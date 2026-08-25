import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { api } from "../api";
import { AgentSteps } from "../components/AgentSteps";
import type { TaskDetail } from "../types";
import { crossPostStatusLabel, isTaskSettled, statusLabel } from "../types";

export function TaskDetailPage() {
  const { taskId } = useParams({ from: "/tasks/$taskId" });
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
        <button className="ghost" onClick={() => cancel.mutate()}>
          取消
        </button>
        <button className="ghost" onClick={() => retry.mutate()}>
          重试
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
