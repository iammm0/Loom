import { useForm } from "@tanstack/react-form";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { api } from "../api";

type CreateTaskResponse = { task_id: string };

type VoiceGroup = { id: string; label: string; voices: string[] };

type Options = {
  voice_groups: VoiceGroup[];
  fonts: string[];
};

type Settings = {
  ui?: Record<string, unknown>;
};

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
  stream?: { headline?: string; steps?: StreamStep[] };
};

export function GeneratePage() {
  const [taskId, setTaskId] = useState("");
  const [formError, setFormError] = useState("");
  const options = useQuery({
    queryKey: ["workspace-options"],
    queryFn: () => api.get<Options>("/api/v1/workspace/options"),
  });
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<Settings>("/api/v1/settings"),
  });
  const task = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => api.get<TaskDetail>(`/api/v1/tasks/${taskId}`),
    enabled: Boolean(taskId),
    refetchInterval: (current) =>
      ["completed", "failed", "cancelled"].includes(current.state.data?.status || "")
        ? false
        : 2500,
  });
  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post<CreateTaskResponse>("/api/v1/videos", body),
    onSuccess: (data) => setTaskId(data.task_id),
  });
  const ui = settings.data?.ui || {};
  const defaultVoice = String(ui.voice_name || "mimo:mimo_default-Female");
  const defaultFont = String(ui.font_name || "STHeitiMedium.ttc");
  const form = useForm({
    defaultValues: {
      video_subject: "",
      video_script: "",
      video_script_prompt: "",
      video_language: "",
      paragraph_number: "1",
      target_duration: "auto",
      video_aspect: "9:16",
      material_strategy: "ai_generated",
      video_count: "1",
      voice_name: defaultVoice,
      voice_volume: "1",
      voice_rate: "1",
      subtitle_enabled: true,
      font_name: defaultFont,
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
    },
    onSubmit: async ({ value }) => {
      if (!value.video_subject.trim() && !value.video_script.trim()) {
        setFormError("请填写主题或文案");
        return;
      }
      setFormError("");
      await mutation.mutateAsync({
        video_subject: value.video_subject,
        video_script: value.video_script,
        video_script_prompt: value.video_script_prompt,
        video_language: value.video_language,
        paragraph_number: Number(value.paragraph_number),
        target_duration: value.target_duration,
        video_aspect: value.video_aspect,
        material_strategy: value.material_strategy,
        video_count: Number(value.video_count),
        voice_name: value.voice_name,
        voice_volume: Number(value.voice_volume),
        voice_rate: Number(value.voice_rate),
        subtitle_enabled: value.subtitle_enabled,
        font_name: value.font_name,
        font_size: Number(value.font_size),
        text_fore_color: value.text_fore_color,
        stroke_color: value.stroke_color,
        stroke_width: Number(value.stroke_width),
        subtitle_position: value.subtitle_position,
        custom_position: Number(value.custom_position),
        text_background_color: value.subtitle_background_enabled
          ? value.subtitle_background_color
          : false,
        rounded_subtitle_background: value.rounded_subtitle_background,
        bgm_type: value.bgm_type,
        bgm_volume: Number(value.bgm_volume),
        sonilo_bgm_prompt: value.sonilo_bgm_prompt,
        ai_director_enabled: true,
      });
    },
  });
  const voices = useMemo(
    () => options.data?.voice_groups.flatMap((group) => group.voices) || [defaultVoice],
    [defaultVoice, options.data],
  );
  const fonts = options.data?.fonts?.length ? options.data.fonts : [defaultFont];
  const video = task.data?.videos?.[0];

  return (
    <section className="page">
      <form
        className="card grid"
        onSubmit={(event) => {
          event.preventDefault();
          void form.handleSubmit();
        }}
      >
        <form.Field
          name="video_subject"
          children={(field) => (
            <label>
              视频主题
              <input
                value={field.state.value}
                onChange={(event) => field.handleChange(event.target.value)}
                placeholder="例如：城市咖啡店探店"
              />
            </label>
          )}
        />
        <form.Field
          name="video_script"
          children={(field) => (
            <label>
              视频文案
              <textarea
                value={field.state.value}
                onChange={(event) => field.handleChange(event.target.value)}
                placeholder="留空则根据主题生成"
              />
            </label>
          )}
        />
        <form.Field
          name="video_script_prompt"
          children={(field) => (
            <label>
              附加要求
              <textarea
                value={field.state.value}
                onChange={(event) => field.handleChange(event.target.value)}
              />
            </label>
          )}
        />
        <details open>
          <summary>视频设置</summary>
          <div className="grid two">
            <form.Field
              name="video_language"
              children={(field) => (
                <label>
                  语言
                  <select
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  >
                    <option value="">自动</option>
                    <option value="zh-CN">中文</option>
                    <option value="en-US">英语</option>
                    <option value="ja-JP">日语</option>
                  </select>
                </label>
              )}
            />
            <form.Field
              name="paragraph_number"
              children={(field) => (
                <label>
                  段落数
                  <input
                    type="number"
                    min={1}
                    max={10}
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  />
                </label>
              )}
            />
            <form.Field
              name="video_aspect"
              children={(field) => (
                <label>
                  画幅
                  <select
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  >
                    <option value="9:16">竖屏 9:16</option>
                    <option value="16:9">横屏 16:9</option>
                    <option value="1:1">方形 1:1</option>
                  </select>
                </label>
              )}
            />
            <form.Field
              name="target_duration"
              children={(field) => (
                <label>
                  时长
                  <select
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  >
                    <option value="auto">按文案</option>
                    <option value="15-30">15-30 秒</option>
                    <option value="30-60">30-60 秒</option>
                    <option value="60-75">60-75 秒</option>
                  </select>
                </label>
              )}
            />
            <form.Field
              name="material_strategy"
              children={(field) => (
                <label>
                  素材方式
                  <select
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  >
                    <option value="ai_generated">分镜自动生成</option>
                    <option value="local_first">素材库与在线优先</option>
                  </select>
                </label>
              )}
            />
            <form.Field
              name="video_count"
              children={(field) => (
                <label>
                  生成数量
                  <input
                    type="number"
                    min={1}
                    max={5}
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  />
                </label>
              )}
            />
          </div>
        </details>
        <details>
          <summary>配音</summary>
          <div className="grid two">
            <form.Field
              name="voice_name"
              children={(field) => (
                <label>
                  音色
                  <select
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  >
                    {(options.data?.voice_groups || []).map((group) => (
                      <optgroup key={group.id} label={group.label}>
                        {group.voices.map((item) => (
                          <option key={item} value={item}>
                            {item}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                    {!options.data ? <option value={voices[0]}>{voices[0]}</option> : null}
                  </select>
                </label>
              )}
            />
            <form.Field
              name="voice_rate"
              children={(field) => (
                <label>
                  语速
                  <input
                    type="number"
                    min={0.5}
                    max={2}
                    step={0.1}
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  />
                </label>
              )}
            />
            <form.Field
              name="voice_volume"
              children={(field) => (
                <label>
                  音量
                  <input
                    type="number"
                    min={0}
                    max={1}
                    step={0.1}
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  />
                </label>
              )}
            />
          </div>
        </details>
        <details>
          <summary>字幕</summary>
          <div className="grid two">
            <form.Field
              name="subtitle_enabled"
              children={(field) => (
                <label className="check">
                  <input
                    type="checkbox"
                    checked={field.state.value}
                    onChange={(event) => field.handleChange(event.target.checked)}
                  />
                  启用字幕
                </label>
              )}
            />
            <form.Field
              name="font_name"
              children={(field) => (
                <label>
                  字体
                  <select
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  >
                    {fonts.map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            />
            <form.Field
              name="font_size"
              children={(field) => (
                <label>
                  字号
                  <input
                    type="number"
                    min={20}
                    max={120}
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  />
                </label>
              )}
            />
            <form.Field
              name="subtitle_position"
              children={(field) => (
                <label>
                  位置
                  <select
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  >
                    <option value="top">顶部</option>
                    <option value="center">居中</option>
                    <option value="bottom">底部</option>
                    <option value="custom">自定义</option>
                  </select>
                </label>
              )}
            />
            <form.Field
              name="text_fore_color"
              children={(field) => (
                <label>
                  文字颜色
                  <input
                    type="color"
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  />
                </label>
              )}
            />
            <form.Field
              name="stroke_color"
              children={(field) => (
                <label>
                  描边颜色
                  <input
                    type="color"
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  />
                </label>
              )}
            />
            <form.Field
              name="subtitle_background_enabled"
              children={(field) => (
                <label className="check">
                  <input
                    type="checkbox"
                    checked={field.state.value}
                    onChange={(event) => field.handleChange(event.target.checked)}
                  />
                  字幕背景
                </label>
              )}
            />
            <form.Field
              name="rounded_subtitle_background"
              children={(field) => (
                <label className="check">
                  <input
                    type="checkbox"
                    checked={field.state.value}
                    onChange={(event) => field.handleChange(event.target.checked)}
                  />
                  圆角背景
                </label>
              )}
            />
          </div>
        </details>
        <details>
          <summary>配乐</summary>
          <div className="grid two">
            <form.Field
              name="bgm_type"
              children={(field) => (
                <label>
                  配乐
                  <select
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  >
                    <option value="random">随机</option>
                    <option value="">不用</option>
                    <option value="sonilo">Sonilo</option>
                  </select>
                </label>
              )}
            />
            <form.Field
              name="bgm_volume"
              children={(field) => (
                <label>
                  配乐音量
                  <input
                    type="number"
                    min={0}
                    max={1}
                    step={0.05}
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  />
                </label>
              )}
            />
            <form.Field
              name="sonilo_bgm_prompt"
              children={(field) => (
                <label>
                  Sonilo 要求
                  <input
                    value={field.state.value}
                    onChange={(event) => field.handleChange(event.target.value)}
                  />
                </label>
              )}
            />
          </div>
        </details>
        <div className="toolbar">
          <button className="primary" type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? "正在创建" : "生成视频"}
          </button>
        </div>
        {formError ? <div className="error">{formError}</div> : null}
        {mutation.error ? <div className="error">{mutation.error.message}</div> : null}
      </form>

      {taskId ? (
        <div className="card grid">
          <div className="toolbar">
            <span className="status">{task.data?.status || "排队中"}</span>
            <Link to="/tasks/$taskId" params={{ taskId }}>
              打开任务
            </Link>
          </div>
          <p>{task.data?.stream?.headline || task.data?.video_subject || taskId}</p>
          <div className="steps">
            {(task.data?.stream?.steps || []).map((step) => (
              <div className={`step ${step.state}`} key={step.key}>
                <span>
                  {step.label}
                  {step.meta ? ` · ${step.meta}` : ""}
                </span>
                <span className="muted">{step.elapsed_label || step.state}</span>
              </div>
            ))}
          </div>
          {task.data?.error ? <div className="error">{task.data.error}</div> : null}
          {video ? (
            <>
              <video src={video} controls />
              <a className="ghost" href={video} download>
                下载
              </a>
            </>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
