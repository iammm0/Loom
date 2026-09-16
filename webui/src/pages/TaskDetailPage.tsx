import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { api } from "../api";
import { AgentSteps } from "../components/AgentSteps";
import type { TaskDetail } from "../types";
import { crossPostStatusLabel, isTaskSettled, statusLabel } from "../types";

export function TaskDetailPage() {
  const { taskId } = useParams({ from: "/tasks/$taskId" });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.get<TaskDetail>(`/api/v1/tasks/${taskId}`),
    refetchInterval: (current) => (isTaskSettled(current.state.data) ? false : 2500),
  });
  const retry = useMutation({
    mutationFn: () => api.post(`/api/v1/tasks/${taskId}/retry`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["task", taskId] }),
  });
  const cancel = useMutation({
    mutationFn: () => api.post(`/api/v1/tasks/${taskId}/cancel`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["task", taskId] }),
  });
  const rename = useMutation({
    mutationFn: (title: string) => api.patch(`/api/v1/tasks/${taskId}`, { title }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
  const remove = useMutation({
    mutationFn: () => api.delete(`/api/v1/tasks/${taskId}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void navigate({ to: "/tasks" });
    },
  });
  const task = query.data;
  const video = task?.videos?.[0];
  const prompt = task?.stream?.headline || task?.video_subject || taskId;

  return (
    <section className="page">
      <div className="top-actions">
        <Link className="linkish" to="/tasks">
          返回
        </Link>
        <span className="status">{statusLabel(task?.status)}</span>
        <span className="spacer" />
        <button
          className="ghost"
          onClick={() => {
            const title = window.prompt("任务名称", task?.video_subject || "");
            if (title?.trim()) rename.mutate(title.trim());
          }}
        >
          重命名
        </button>
        <button className="ghost" onClick={() => cancel.mutate()}>
          取消
        </button>
        <button className="ghost" onClick={() => retry.mutate()}>
          重试
        </button>
        <button
          className="danger"
          onClick={() => {
            if (window.confirm("删除该任务？")) remove.mutate();
          }}
        >
          删除
        </button>
      </div>
      <div className="agent-inner fill">
        <div className="user-bubble">{prompt}</div>
        <div className="agent-block">
          <AgentSteps steps={task?.stream?.steps} />
          {task?.error ? <div className="error">{task.error}</div> : null}
          {task?.cross_post_state ? (
            <div className={`publish-status ${task.cross_post_state === "complete" ? "ready" : ""}`}>
              自动发布：{crossPostStatusLabel(task.cross_post_state)}
              {task.cross_post_error ? ` · ${task.cross_post_error}` : ""}
            </div>
          ) : null}
          {video ? (
            <div className="artifact">
              <video src={video} controls />
              <div className="artifact-bar">
                <a className="ghost" href={video} download>
                  下载
                </a>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
