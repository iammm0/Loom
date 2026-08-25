import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api";
import {
  crossPostStatusLabel,
  type ChoiceOption,
  type Settings,
  type WorkspaceOptions,
} from "../types";

type PublishTask = {
  task_id: string;
  video_subject?: string;
  created_at?: string;
  cross_post_state?: string;
  cross_post_error?: string;
};

const FALLBACK_PLATFORMS: ChoiceOption[] = [
  { id: "tiktok", label: "TikTok", hint: "短视频" },
  { id: "instagram", label: "Instagram", hint: "Reels" },
  { id: "youtube", label: "YouTube", hint: "Shorts" },
];

const FALLBACK_PRIVACY: ChoiceOption[] = [
  { id: "public", label: "公开" },
  { id: "unlisted", label: "不列出" },
  { id: "private", label: "私密" },
];

function asBool(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  const text = String(value || "").trim().toLowerCase();
  return text === "true" || text === "1" || text === "yes" || text === "on";
}

function asPlatforms(value: unknown): string[] {
  const allowed = new Set(FALLBACK_PLATFORMS.map((item) => item.id));
  if (value == null || value === "") return ["tiktok", "instagram"];
  const items = Array.isArray(value)
    ? value.map(String)
    : String(value)
        .split(/[\s,]+/)
        .filter(Boolean);
  return items.map((item) => item.trim().toLowerCase()).filter((item) => allowed.has(item));
}

function when(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="settings-field">
      <span className="settings-label">{label}</span>
      {children}
      {hint ? <span className="settings-hint">{hint}</span> : null}
    </label>
  );
}

export function PublishPage() {
  const queryClient = useQueryClient();
  const [enabled, setEnabled] = useState(false);
  const [autoUpload, setAutoUpload] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [username, setUsername] = useState("");
  const [platforms, setPlatforms] = useState<string[]>(["tiktok", "instagram"]);
  const [privacy, setPrivacy] = useState("public");
  const [savedAt, setSavedAt] = useState("");

  const options = useQuery({
    queryKey: ["workspace-options"],
    queryFn: () => api.get<WorkspaceOptions>("/api/v1/workspace/options"),
  });
  const query = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<Settings>("/api/v1/settings"),
  });
  const tasks = useQuery({
    queryKey: ["tasks"],
    queryFn: () => api.get<{ tasks: PublishTask[] }>("/api/v1/tasks?page=1&page_size=50"),
    refetchInterval: 4000,
  });
  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.put<Settings>("/api/v1/settings", body),
    onSuccess: () => {
      setSavedAt(new Date().toLocaleTimeString());
      void queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
  });

  const platformOptions = options.data?.upload_post_platforms?.length
    ? options.data.upload_post_platforms
    : FALLBACK_PLATFORMS;
  const privacyOptions = options.data?.upload_post_youtube_privacy?.length
    ? options.data.upload_post_youtube_privacy
    : FALLBACK_PRIVACY;

  useEffect(() => {
    if (!query.data) return;
    const app = query.data.app || {};
    setEnabled(asBool(app.upload_post_enabled));
    setAutoUpload(asBool(app.upload_post_auto_upload));
    setApiKey(String(app.upload_post_api_key || ""));
    setUsername(String(app.upload_post_username || ""));
    setPlatforms(asPlatforms(app.upload_post_platforms));
    const nextPrivacy = String(app.upload_post_youtube_privacy_status || "public");
    setPrivacy(["public", "unlisted", "private"].includes(nextPrivacy) ? nextPrivacy : "public");
  }, [query.data]);

  const configured = Boolean(enabled && apiKey.trim() && username.trim());
  const ready = configured && autoUpload && platforms.length > 0;
  const statusText = ready
    ? "成片后会自动发布到已选平台。"
    : configured
      ? "账号已连接。打开「生成后自动发布」才会真正发帖。"
      : enabled
        ? "还需要填写 Upload-Post API Key 和用户名。"
        : "当前未启用跨平台发布。";

  const recent = useMemo(
    () => (tasks.data?.tasks || []).filter((task) => task.cross_post_state),
    [tasks.data],
  );

  const togglePlatform = (id: string) => {
    setPlatforms((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  };

  if (query.isLoading) {
    return (
      <section className="page">
        <div className="muted">加载配置中…</div>
      </section>
    );
  }

  if (!query.data) {
    return (
      <section className="page">
        <div className="error">{query.error?.message || "无法读取配置"}</div>
      </section>
    );
  }

  return (
    <section className="page">
      <div className="page-header">
        <div>
          <h1 className="page-title">自动发布</h1>
          <p className="muted">通过 Upload-Post 把成片同步到 TikTok、Instagram 和 YouTube。</p>
        </div>
      </div>

      <div className={`publish-status ${ready ? "ready" : ""}`}>{statusText}</div>

      <form
        className="settings-panel"
        onSubmit={(event) => {
          event.preventDefault();
          void mutation.mutateAsync({
            app: {
              ...(query.data.app || {}),
              upload_post_enabled: enabled,
              upload_post_auto_upload: autoUpload,
              upload_post_api_key: apiKey,
              upload_post_username: username,
              upload_post_platforms: platforms,
              upload_post_youtube_privacy_status: privacy,
            },
          });
        }}
      >
        <div className="settings-section stack">
          <div className="settings-section-head">
            <h2>Upload-Post</h2>
            <p className="muted">
              在{" "}
              <a className="inline-link" href="https://upload-post.com/" target="_blank" rel="noreferrer">
                upload-post.com
              </a>{" "}
              创建账号并申请 API Key，再把平台账号绑定到同一用户名。
            </p>
          </div>
          <label className="toggle">
            启用跨平台发布
            <input
              type="checkbox"
              checked={enabled}
              onChange={(event) => setEnabled(event.target.checked)}
            />
          </label>
          <label className="toggle">
            生成后自动发布
            <input
              type="checkbox"
              checked={autoUpload}
              onChange={(event) => setAutoUpload(event.target.checked)}
            />
          </label>
          <Field label="API Key" hint="保存后立即生效，不会出现在任务列表里。">
            <input
              type="password"
              autoComplete="off"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder="Upload-Post API Key"
            />
          </Field>
          <Field label="用户名" hint="必须与 Upload-Post 后台已绑定的用户名一致。">
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="upload-post username"
            />
          </Field>
        </div>

        <div className="settings-section stack">
          <div className="settings-section-head">
            <h2>发布平台</h2>
            <p className="muted">成片会按这里勾选的平台一次性分发。</p>
          </div>
          <div className="choice-grid">
            {platformOptions.map((item) => {
              const active = platforms.includes(item.id);
              return (
                <button
                  key={item.id}
                  type="button"
                  className={`choice-card ${active ? "active" : ""}`}
                  aria-pressed={active}
                  onClick={() => togglePlatform(item.id)}
                >
                  <span className="choice-card-title">{item.label}</span>
                  {item.hint ? <span className="choice-card-hint">{item.hint}</span> : null}
                </button>
              );
            })}
          </div>
          {!platforms.length ? <div className="error">至少选择一个平台。</div> : null}
          {platforms.includes("youtube") ? (
            <Field label="YouTube 可见范围">
              <select value={privacy} onChange={(event) => setPrivacy(event.target.value)}>
                {privacyOptions.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
        </div>

        <div className="settings-footer">
          <button className="primary" type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? "保存中…" : "保存发布配置"}
          </button>
          {savedAt && !mutation.error ? <span className="muted">已保存 {savedAt}</span> : null}
          {mutation.error ? <span className="error">{mutation.error.message}</span> : null}
        </div>
      </form>

      <div className="settings-section stack">
        <div className="settings-section-head">
          <h2>最近发布</h2>
          <p className="muted">只显示已经进入自动发布流程的任务。</p>
        </div>
        <div className="row-list">
          {recent.map((task) => (
            <div className="row-link plain" key={task.task_id}>
              <Link className="row-title" to="/tasks/$taskId" params={{ taskId: task.task_id }}>
                {task.video_subject || task.task_id}
              </Link>
              <span className="status">{crossPostStatusLabel(task.cross_post_state)}</span>
              <span className="muted">{task.cross_post_error || when(task.created_at)}</span>
            </div>
          ))}
          {!recent.length ? <div className="muted">还没有自动发布记录</div> : null}
        </div>
      </div>
    </section>
  );
}
