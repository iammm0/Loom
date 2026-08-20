import { useMutation, useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { AgentSteps } from "../components/AgentSteps";
import { Icons } from "../icons";
import type { Settings, TaskDetail, WorkspaceOptions } from "../types";

type CreateTaskResponse = { task_id: string };

type Draft = {
  video_language: string;
  paragraph_number: string;
  target_duration: string;
  video_aspect: string;
  material_strategy: string;
  video_count: string;
  voice_name: string;
  voice_volume: string;
  voice_rate: string;
  subtitle_enabled: boolean;
  font_name: string;
  font_size: string;
  text_fore_color: string;
  stroke_color: string;
  stroke_width: string;
  subtitle_position: string;
  custom_position: string;
  subtitle_background_enabled: boolean;
  subtitle_background_color: string;
  rounded_subtitle_background: boolean;
  bgm_type: string;
  bgm_volume: string;
  sonilo_bgm_prompt: string;
};

type Turn = { prompt: string; taskId: string };

const ASPECTS = ["9:16", "16:9", "1:1"];
const DURATIONS = [
  ["auto", "自动"],
  ["15-30", "15-30s"],
  ["30-60", "30-60s"],
  ["60-75", "60-75s"],
];
const STRATEGIES = [
  ["ai_generated", "AI 分镜"],
  ["local_first", "素材库"],
];

function nextValue(values: string[], current: string) {
  const index = values.indexOf(current);
  return values[(index + 1) % values.length];
}

function splitPrompt(text: string) {
  const trimmed = text.trim();
  const lines = trimmed.split(/\n/);
  const first = (lines[0] || "").trim();
  const rest = lines.slice(1).join("\n").trim();
  if (rest && first.length <= 40) {
    return { video_subject: first, video_script: rest };
  }
  if (trimmed.length > 120 && lines.length > 2) {
    return { video_subject: first.slice(0, 40), video_script: trimmed };
  }
  return { video_subject: trimmed, video_script: "" };
}

function defaultsFrom(ui: Record<string, unknown>, fallbackVoice: string, fallbackFont: string): Draft {
  return {
    video_language: "",
    paragraph_number: "1",
    target_duration: "auto",
    video_aspect: "9:16",
    material_strategy: "ai_generated",
    video_count: "1",
    voice_name: String(ui.voice_name || fallbackVoice),
    voice_volume: "1",
    voice_rate: "1",
    subtitle_enabled: true,
    font_name: String(ui.font_name || fallbackFont),
    font_size: String(ui.font_size || 60),
    text_fore_color: String(ui.text_fore_color || "#FFFFFF"),
    stroke_color: "#000000",
    stroke_width: "1.5",
    subtitle_position: String(ui.subtitle_position || "bottom"),
    custom_position: String(ui.custom_position || 70),
    subtitle_background_enabled: Boolean(ui.subtitle_background_enabled),
    subtitle_background_color: String(ui.subtitle_background_color || "#000000"),
    rounded_subtitle_background: Boolean(ui.rounded_subtitle_background),
    bgm_type: "random",
    bgm_volume: "0.2",
    sonilo_bgm_prompt: "",
  };
}

function AgentTurn({
  prompt,
  taskId,
  onUpdate,
}: {
  prompt: string;
  taskId: string;
  onUpdate?: () => void;
}) {
  const task = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.get<TaskDetail>(`/api/v1/tasks/${taskId}`),
    refetchInterval: (current) =>
      ["completed", "failed", "cancelled"].includes(current.state.data?.status || "")
        ? false
        : 2500,
  });
  useEffect(() => {
    onUpdate?.();
  }, [onUpdate, task.data]);
  const video = task.data?.videos?.[0];
  return (
    <>
      <div className="user-bubble">{prompt}</div>
      <div className="agent-block">
        <AgentSteps steps={task.data?.stream?.steps} />
        {task.data?.error ? <div className="error">{task.data.error}</div> : null}
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
    </>
  );
}

function GenerateSession() {
  const [prompt, setPrompt] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const options = useQuery({
    queryKey: ["workspace-options"],
    queryFn: () => api.get<WorkspaceOptions>("/api/v1/workspace/options"),
  });
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<Settings>("/api/v1/settings"),
  });
  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post<CreateTaskResponse>("/api/v1/videos", body),
  });

  const ui = settings.data?.ui || {};
  const fallbackVoice = String(ui.voice_name || "mimo:mimo_default-Female");
  const fallbackFont = String(ui.font_name || "STHeitiMedium.ttc");

  useEffect(() => {
    if (draft || !settings.data) return;
    const nextUi = settings.data.ui || {};
    setDraft(
      defaultsFrom(
        nextUi,
        String(nextUi.voice_name || fallbackVoice),
        String(nextUi.font_name || fallbackFont),
      ),
    );
  }, [draft, fallbackFont, fallbackVoice, settings.data]);

  useEffect(() => {
    const node = threadRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [turns]);

  const scrollToEnd = useCallback(() => {
    const node = threadRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, []);

  useEffect(() => {
    const node = areaRef.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 200)}px`;
  }, [prompt]);

  const voices = options.data?.voice_groups || [];
  const fonts = options.data?.fonts?.length ? options.data.fonts : [fallbackFont];
  const empty = turns.length === 0;
  const patch = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => (current ? { ...current, [key]: value } : current));

  const submit = async () => {
    const text = prompt.trim();
    if (!text || !draft || mutation.isPending) return;
    setError("");
    const parts = splitPrompt(text);
    try {
      const data = await mutation.mutateAsync({
        ...parts,
        video_script_prompt: "",
        video_language: draft.video_language,
        paragraph_number: Number(draft.paragraph_number),
        target_duration: draft.target_duration,
        video_aspect: draft.video_aspect,
        material_strategy: draft.material_strategy,
        video_count: Number(draft.video_count),
        voice_name: draft.voice_name,
        voice_volume: Number(draft.voice_volume),
        voice_rate: Number(draft.voice_rate),
        subtitle_enabled: draft.subtitle_enabled,
        font_name: draft.font_name,
        font_size: Number(draft.font_size),
        text_fore_color: draft.text_fore_color,
        stroke_color: draft.stroke_color,
        stroke_width: Number(draft.stroke_width),
        subtitle_position: draft.subtitle_position,
        custom_position: Number(draft.custom_position),
        text_background_color: draft.subtitle_background_enabled
          ? draft.subtitle_background_color
          : false,
        rounded_subtitle_background: draft.rounded_subtitle_background,
        bgm_type: draft.bgm_type,
        bgm_volume: Number(draft.bgm_volume),
        sonilo_bgm_prompt: draft.sonilo_bgm_prompt,
        ai_director_enabled: true,
      });
      setTurns((current) => [...current, { prompt: text, taskId: data.task_id }]);
      setPrompt("");
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    }
  };

  const durationLabel = DURATIONS.find((item) => item[0] === draft?.target_duration)?.[1] || "自动";
  const strategyLabel = STRATEGIES.find((item) => item[0] === draft?.material_strategy)?.[1] || "AI 分镜";

  const composer = (
    <div className="composer-dock">
      <div className="composer">
        {open && draft ? (
          <div className="sheet">
            <div className="stack two">
              <select
                value={draft.video_language}
                onChange={(event) => patch("video_language", event.target.value)}
              >
                <option value="">语言</option>
                <option value="zh-CN">中文</option>
                <option value="en-US">英语</option>
                <option value="ja-JP">日语</option>
              </select>
              <input
                type="number"
                min={1}
                max={10}
                value={draft.paragraph_number}
                onChange={(event) => patch("paragraph_number", event.target.value)}
                placeholder="段落"
              />
              <input
                type="number"
                min={1}
                max={5}
                value={draft.video_count}
                onChange={(event) => patch("video_count", event.target.value)}
                placeholder="数量"
              />
              <select
                value={draft.voice_name}
                onChange={(event) => patch("voice_name", event.target.value)}
              >
                {voices.map((group) => (
                  <optgroup key={group.id} label={group.label}>
                    {group.voices.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
              <input
                type="number"
                min={0.5}
                max={2}
                step={0.1}
                value={draft.voice_rate}
                onChange={(event) => patch("voice_rate", event.target.value)}
                placeholder="语速"
              />
              <input
                type="number"
                min={0}
                max={1}
                step={0.1}
                value={draft.voice_volume}
                onChange={(event) => patch("voice_volume", event.target.value)}
                placeholder="音量"
              />
              <select
                value={draft.font_name}
                onChange={(event) => patch("font_name", event.target.value)}
              >
                {fonts.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
              <select
                value={draft.subtitle_position}
                onChange={(event) => patch("subtitle_position", event.target.value)}
              >
                <option value="top">顶部</option>
                <option value="center">居中</option>
                <option value="bottom">底部</option>
                <option value="custom">自定义</option>
              </select>
              <select value={draft.bgm_type} onChange={(event) => patch("bgm_type", event.target.value)}>
                <option value="random">随机配乐</option>
                <option value="">无配乐</option>
                <option value="sonilo">Sonilo</option>
              </select>
              <input
                type="number"
                min={0}
                max={1}
                step={0.05}
                value={draft.bgm_volume}
                onChange={(event) => patch("bgm_volume", event.target.value)}
                placeholder="配乐音量"
              />
            </div>
            {draft.bgm_type === "sonilo" ? (
              <input
                value={draft.sonilo_bgm_prompt}
                onChange={(event) => patch("sonilo_bgm_prompt", event.target.value)}
                placeholder="Sonilo"
              />
            ) : null}
            <label className="toggle">
              字幕
              <input
                type="checkbox"
                checked={draft.subtitle_enabled}
                onChange={(event) => patch("subtitle_enabled", event.target.checked)}
              />
            </label>
          </div>
        ) : null}
        <textarea
          ref={areaRef}
          value={prompt}
          placeholder="主题、文案或修改意见"
          rows={1}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void submit();
            }
          }}
        />
        <div className="composer-bar">
          <button
            className="chip"
            type="button"
            onClick={() => draft && patch("video_aspect", nextValue(ASPECTS, draft.video_aspect))}
          >
            {draft?.video_aspect || "9:16"}
          </button>
          <button
            className="chip"
            type="button"
            onClick={() =>
              draft && patch("target_duration", nextValue(DURATIONS.map((item) => item[0]), draft.target_duration))
            }
          >
            {durationLabel}
          </button>
          <button
            className="chip"
            type="button"
            onClick={() =>
              draft &&
              patch("material_strategy", nextValue(STRATEGIES.map((item) => item[0]), draft.material_strategy))
            }
          >
            {strategyLabel}
          </button>
          <span className="spacer" />
          <button className="icon-btn" type="button" onClick={() => setOpen((value) => !value)}>
            <Icons.sliders />
          </button>
          <button className="send-btn" type="button" disabled={!prompt.trim() || mutation.isPending} onClick={() => void submit()}>
            <Icons.send />
          </button>
        </div>
      </div>
      {error ? <div className="error">{error}</div> : null}
    </div>
  );

  if (empty) {
    return (
      <section className="agent-page empty">
        <p className="agent-hello">今天剪什么</p>
        {composer}
      </section>
    );
  }

  return (
    <section className="agent-page">
      <div className="agent-thread" ref={threadRef}>
        <div className="agent-inner">
          {turns.map((turn) => (
            <AgentTurn key={turn.taskId} prompt={turn.prompt} taskId={turn.taskId} onUpdate={scrollToEnd} />
          ))}
        </div>
      </div>
      {composer}
    </section>
  );
}

export function GeneratePage() {
  const [session, setSession] = useState(0);
  useEffect(() => {
    const reset = () => setSession((value) => value + 1);
    window.addEventListener("loom-new-session", reset);
    return () => window.removeEventListener("loom-new-session", reset);
  }, []);
  return <GenerateSession key={session} />;
}
