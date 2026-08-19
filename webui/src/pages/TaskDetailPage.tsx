import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { api } from "../api";

type StreamStep = {
  key: string;
  label: string;
  state: string;
  elapsed_label?: string;
  meta?: string;
};

type TaskDetail = {
  task_id: string;
  status?: string;
  stage?: string;
  progress?: number;
  error?: string;
  video_subject?: string;
  videos?: string[];
  stream?: {
    headline?: string;
    steps?: StreamStep[];
  };
};

export function TaskDetailPage() {
  const { taskId } = useParams({ from: "/tasks/$taskId" });
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.get<TaskDetail>(`/api/v1/tasks/${taskId}`),
    refetchInterval: (current) =>
      ["completed", "failed", "cancelled"].includes(current.state.data?.status || "")
        ? false
        : 2500,
  });
  const retry = useMutation({
    mutationFn: () => api.post(`/api/v1/tasks/${taskId}/retry`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["task", taskId] }),
  });
  const cancel = useMutation({
    mutationFn: () => api.post(`/api/v1/tasks/${taskId}/cancel`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["task", taskId] }),
  });
  const task = query.data;
  const video = task?.videos?.[0];

  return (
    <section className="page">
      <div className="card">
        <Link to="/tasks">返回任务</Link>
        <p>{task?.stream?.headline || task?.video_subject || taskId}</p>
        <span className="status">{task?.status || ""}</span>
        <div className="toolbar">
          <button className="ghost" onClick={() => cancel.mutate()}>
            取消
          </button>
          <button className="ghost" onClick={() => retry.mutate()}>
            重试
          </button>
        </div>
        {task?.error ? <div className="error">{task.error}</div> : null}
      </div>
      <div className="card steps">
        {(task?.stream?.steps || []).map((step) => (
          <div className={`step ${step.state}`} key={step.key}>
            <span>
              {step.label}
              {step.meta ? ` · ${step.meta}` : ""}
            </span>
            <span className="muted">{step.elapsed_label || step.state}</span>
          </div>
        ))}
      </div>
      {video ? (
        <div className="card">
          <video src={video} controls />
          <a className="ghost" href={video} download>
            下载
          </a>
        </div>
      ) : null}
    </section>
  );
}
