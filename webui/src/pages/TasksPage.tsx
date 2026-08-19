import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createColumnHelper,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { api } from "../api";
import { DataTable } from "../components/DataTable";

type Task = {
  task_id: string;
  status?: string;
  stage?: string;
  progress?: number;
  video_subject?: string;
  created_at?: string;
};

type TaskList = {
  tasks: Task[];
  total: number;
  page: number;
  page_size: number;
};

const columnHelper = createColumnHelper<Task>();

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

  const columns = useMemo(
    () => [
      columnHelper.display({
        id: "select",
        header: "",
        cell: ({ row }) => (
          <input
            type="checkbox"
            checked={Boolean(selected[row.original.task_id])}
            onChange={(event) =>
              setSelected((current) => ({
                ...current,
                [row.original.task_id]: event.target.checked,
              }))
            }
          />
        ),
      }),
      columnHelper.accessor("video_subject", {
        header: "主题",
        cell: (info) => info.getValue() || info.row.original.task_id,
      }),
      columnHelper.accessor("status", { header: "状态" }),
      columnHelper.accessor("stage", { header: "阶段" }),
      columnHelper.accessor("progress", {
        header: "进度",
        cell: (info) => `${info.getValue() || 0}%`,
      }),
      columnHelper.accessor("created_at", { header: "时间" }),
      columnHelper.display({
        id: "open",
        header: "",
        cell: ({ row }) => (
          <Link to="/tasks/$taskId" params={{ taskId: row.original.task_id }}>
            详情
          </Link>
        ),
      }),
    ],
    [selected],
  );

  const table = useReactTable({
    data: query.data?.tasks || [],
    columns,
    getCoreRowModel: getCoreRowModel(),
  });
  const selectedIds = Object.entries(selected)
    .filter(([, checked]) => checked)
    .map(([id]) => id);

  return (
    <section className="page">
      <div className="card">
        <div className="toolbar">
          <button
            className="ghost"
            disabled={!selectedIds.length}
            onClick={() => action.mutate({ action: "cancel", task_ids: selectedIds })}
          >
            取消
          </button>
          <button
            className="ghost"
            disabled={!selectedIds.length}
            onClick={() => action.mutate({ action: "retry", task_ids: selectedIds })}
          >
            重试
          </button>
          <button
            className="danger"
            disabled={!selectedIds.length}
            onClick={() => action.mutate({ action: "delete", task_ids: selectedIds })}
          >
            删除
          </button>
        </div>
        <DataTable table={table} empty="暂无任务" />
        {query.error ? <div className="error">{query.error.message}</div> : null}
      </div>
    </section>
  );
}
