import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { api } from "../api";
import { crossPostStatusLabel, statusLabel } from "../types";

type Task = {
  task_id: string;
  status?: string;
  video_subject?: string;
  created_at?: string;
  cross_post_state?: string;
};

type TaskList = {
  tasks: Task[];
};

function when(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export function TasksPage() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const query = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api.get<TaskList>("/api/v1/tasks?page=1&page_size=50"),
    refetchInterval: 4000,
  });
  const action = useMutation({
    mutationFn: (payload: { action: string; task_ids: string[] }) =>
      api.post("/api/v1/tasks/actions", payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      setSelected({});
    },
  });
  const selectedIds = Object.entries(selected)
    .filter(([, checked]) => checked)
    .map(([id]) => id);

  return (
    <section className="page">
      {selectedIds.length ? (
        <div className="toolbar">
          <button className="ghost" onClick={() => action.mutate({ action: "cancel", task_ids: selectedIds })}>
            取消
          </button>
          <button className="ghost" onClick={() => action.mutate({ action: "retry", task_ids: selectedIds })}>
            重试
          </button>
          <button className="danger" onClick={() => action.mutate({ action: "delete", task_ids: selectedIds })}>
            删除
          </button>
        </div>
      ) : null}
      <div className="row-list">
        {(query.data?.tasks || []).map((task) => (
          <div className="row-link" key={task.task_id}>
            <input
              type="checkbox"
              checked={Boolean(selected[task.task_id])}
              onChange={(event) =>
                setSelected((current) => ({
                  ...current,
                  [task.task_id]: event.target.checked,
                }))
              }
            />
            <Link
              className="row-title"
              to="/tasks/$taskId"
              params={{ taskId: task.task_id }}
            >
              {task.video_subject || task.task_id}
            </Link>
            <span className="status">
              {statusLabel(task.status)}
              {task.cross_post_state ? ` · ${crossPostStatusLabel(task.cross_post_state)}` : ""}
            </span>
            <span className="muted">{when(task.created_at)}</span>
          </div>
        ))}
        {!query.data?.tasks?.length ? <div className="muted">暂无任务</div> : null}
      </div>
      {query.error ? <div className="error">{query.error.message}</div> : null}
    </section>
  );
}
