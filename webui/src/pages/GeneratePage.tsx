import { useMutation, useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { AgentSteps } from "../components/AgentSteps";
import { Icons } from "../icons";
import type { Settings, StreamStep, TaskDetail, WorkspaceOptions } from "../types";
import { crossPostStatusLabel, isTaskSettled } from "../types";

type CreateTaskResponse = { task_id: string };

type Draft = {
  video_language: string;
  target_duration: string;
  video_aspect: string;
  material_strategy: string;
  voice_name: string;
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
  sonilo_bgm_prompt: string;
};

type Turn =
  | { kind: "task"; prompt: string; taskId: string }
  | { kind: "demo"; prompt: string; id: string };

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
const DEMO_PROMPT = "制作一条介绍城市夜间书店的 30 秒竖屏短视频";
const DEMO_STEPS = [
  {
    key: "preflight",
    label: "理解创作目标",
    meta: "需求已结构化",
    elapsed: "4.0s",
    duration: 4000,
    detail: "识别主题、受众、画面比例和目标时长，建立本次剪辑的执行约束。",
  },
  {
    key: "script",
    label: "撰写视频文案",
    meta: "92 字 · 3 段",
    elapsed: "5.5s",
    duration: 5500,
    detail: "围绕夜间书店提炼开场钩子、核心叙事和结尾行动句。",
  },
  {
    key: "director",
    label: "生成导演方案",
    meta: "节奏与参数已确定",
    elapsed: "5.0s",
    duration: 5000,
    detail: "根据旁白长度自动决定镜头密度、语速、转场方式和音乐情绪。",
  },
  {
    key: "scenes",
    label: "拆解视频分镜",
    meta: "6 个镜头",
    elapsed: "6.0s",
    duration: 6000,
    detail: "把叙事拆成可执行镜头，并为每个镜头生成景别、动作与画面描述。",
  },
  {
    key: "audio",
    label: "生成旁白与配乐",
    meta: "人声与 BGM 已混合",
    elapsed: "5.0s",
    duration: 5000,
    detail: "生成自然旁白，匹配温暖轻爵士配乐，并自动控制人声与音乐比例。",
  },
  {
    key: "subtitle",
    label: "生成动态字幕",
    meta: "12 条字幕",
    elapsed: "4.0s",
    duration: 4000,
    detail: "依据旁白时间轴切分字幕，处理断句、行宽和竖屏安全区域。",
  },
  {
    key: "materials",
    label: "匹配画面素材",
    meta: "6 / 6 已匹配",
    elapsed: "6.0s",
    duration: 6000,
    detail: "按镜头语义筛选素材，比较构图、情绪和可用时长，选出最佳候选。",
  },
  {
    key: "timeline",
    label: "组装剪辑时间线",
    meta: "画面、声音、字幕已对齐",
    elapsed: "5.5s",
    duration: 5500,
    detail: "完成镜头裁切、转场、旁白、配乐与字幕的时间线编排。",
  },
  {
    key: "export",
    label: "渲染最终成片",
    meta: "1080 × 1920",
    elapsed: "4.5s",
    duration: 4500,
    detail: "执行最终画面合成与质量检查，输出可发布的竖屏视频。",
  },
];
const DEMO_TOTAL_DURATION = DEMO_STEPS.reduce((total, step) => total + step.duration, 0);
const DEMO_SCENES = [
  { number: "01", time: "0:00–0:04", label: "雨夜街景", position: "12%" },
  { number: "02", time: "0:04–0:09", label: "推近书店", position: "30%" },
  { number: "03", time: "0:09–0:14", label: "读者入店", position: "46%" },
  { number: "04", time: "0:14–0:20", label: "书架细节", position: "62%" },
  { number: "05", time: "0:20–0:25", label: "暖光阅读", position: "78%" },
  { number: "06", time: "0:25–0:30", label: "门店收束", position: "92%" },
];
const DEMO_ACTIONS: Record<string, Array<{ tool: string; label: string; detail: string }>> = {
  preflight: [
    { tool: "read_request", label: "读取创作需求", detail: "提取主题：城市夜间书店；识别为短视频生成任务。" },
    { tool: "detect_intent", label: "判断内容意图", detail: "目标是营造温暖氛围，并引导观众产生到店阅读兴趣。" },
    { tool: "infer_format", label: "推导成片规格", detail: "采用 9:16 竖屏、中文旁白、约 30 秒时长。" },
    { tool: "build_plan", label: "建立执行计划", detail: "规划文案、导演、分镜、音频、字幕、素材和导出阶段。" },
  ],
  script: [
    { tool: "extract_theme", label: "提炼主题信息", detail: "保留夜色、暖光、书页气味与慢生活四个核心意象。" },
    { tool: "write_hook", label: "撰写开场钩子", detail: "用“城市入夜后，还有一盏灯”在前 3 秒建立注意力。" },
    { tool: "write_body", label: "展开核心叙事", detail: "通过推门、书架和音乐三个动作建立沉浸感。" },
    { tool: "write_outro", label: "生成结尾行动句", detail: "邀请观众给自己二十分钟，去遇见一本未知的书。" },
    { tool: "estimate_duration", label: "校验旁白时长", detail: "92 字，预计自然旁白 28.6 秒，符合目标区间。" },
  ],
  director: [
    { tool: "analyze_pacing", label: "分析叙事节奏", detail: "开场慢入，中段用细节快切，结尾保留 1.2 秒停顿。" },
    { tool: "set_voice_rate", label: "设定旁白语速", detail: "自动选择 1.05×，兼顾信息完整和自然听感。" },
    { tool: "plan_shots", label: "计算镜头密度", detail: "规划 6 个镜头，平均镜头长度约 5 秒。" },
    { tool: "choose_transitions", label: "选择转场策略", detail: "主体镜头使用硬切，开场和结尾使用轻微淡入。" },
    { tool: "balance_music", label: "规划配乐关系", detail: "轻爵士作为背景层，人声出现时自动压低至 18%。" },
  ],
  scenes: [
    { tool: "scene_01", label: "创建镜头 01 · 雨夜街景", detail: "广角建立环境，湿润路面反射暖色店铺灯光。" },
    { tool: "scene_02", label: "创建镜头 02 · 推近书店", detail: "镜头沿人行道缓慢前移，把观众带向门口。" },
    { tool: "scene_03", label: "创建镜头 03 · 读者入店", detail: "中景捕捉推门动作，完成室外到室内的叙事转换。" },
    { tool: "scene_04", label: "创建镜头 04 · 书架细节", detail: "特写书脊、翻页和手部动作，增加触觉联想。" },
    { tool: "scene_05", label: "创建镜头 05 · 暖光阅读", detail: "静态近景呈现读者停留，配合旁白情绪高点。" },
    { tool: "scene_06", label: "创建镜头 06 · 门店收束", detail: "回到店外全景，留出结尾字幕和品牌安全区域。" },
  ],
  audio: [
    { tool: "select_voice", label: "匹配旁白声音", detail: "选择自然、亲近的女性声线，与生活方式内容一致。" },
    { tool: "synthesize_voice", label: "合成完整旁白", detail: "生成 28.6 秒人声，并保留句间自然呼吸。" },
    { tool: "normalize_voice", label: "标准化人声响度", detail: "目标响度调整至 -14 LUFS，控制峰值避免失真。" },
    { tool: "select_bgm", label: "匹配背景音乐", detail: "选择温暖轻爵士，节拍避开关键旁白重音。" },
    { tool: "mix_audio", label: "执行自动混音", detail: "旁白优先，背景音乐动态衰减，结尾自然淡出。" },
  ],
  subtitle: [
    { tool: "align_timestamps", label: "对齐旁白时间轴", detail: "依据语音边界生成 12 条可编辑字幕区间。" },
    { tool: "split_lines", label: "优化字幕断句", detail: "按语义和阅读速度断行，避免标点单独占行。" },
    { tool: "apply_style", label: "应用字幕样式", detail: "使用白色中黑体和轻描边，保证复杂画面下可读。" },
    { tool: "check_safe_area", label: "检查竖屏安全区", detail: "避开平台按钮区域，字幕固定在底部安全线以内。" },
  ],
  materials: [
    { tool: "search_scene_01", label: "检索雨夜城市素材", detail: "比较 18 个候选，选择暖冷对比清晰的街景。" },
    { tool: "search_scene_02", label: "检索书店外景素材", detail: "优先正面构图和可用于推近运动的高分辨率镜头。" },
    { tool: "search_scene_03", label: "检索入店动作素材", detail: "筛选带自然人物动作且无明显品牌标识的片段。" },
    { tool: "search_scene_04", label: "检索书页细节素材", detail: "匹配翻页、书脊和木质书架三个视觉关键词。" },
    { tool: "search_scene_05", label: "检索阅读氛围素材", detail: "选择暖光、浅景深和静态构图以承接情绪高点。" },
    { tool: "validate_materials", label: "验证素材完整性", detail: "6 个镜头全部匹配，分辨率与可用时长检查通过。" },
  ],
  timeline: [
    { tool: "trim_clips", label: "裁切并排列镜头", detail: "按旁白语义点调整入出点，形成 30 秒主画面轨。" },
    { tool: "apply_transitions", label: "应用镜头转场", detail: "添加 2 处淡入，其余使用节奏明确的直接切换。" },
    { tool: "place_voiceover", label: "铺设旁白轨道", detail: "将旁白起点提前 0.3 秒，增强开场进入感。" },
    { tool: "place_subtitles", label: "铺设字幕轨道", detail: "12 条字幕与人声逐句对齐，并完成安全区复检。" },
    { tool: "place_music", label: "铺设配乐轨道", detail: "音乐覆盖全片，在结尾 1.5 秒执行平滑淡出。" },
  ],
  export: [
    { tool: "render_frames", label: "渲染画面与字幕", detail: "按 1080 × 1920、30 fps 合成全部视频帧。" },
    { tool: "encode_video", label: "编码视频文件", detail: "使用 H.264 编码并优化移动端播放兼容性。" },
    { tool: "mux_audio", label: "封装最终音频", detail: "写入旁白与配乐混音结果，保持音画同步。" },
    { tool: "quality_check", label: "执行发布前检查", detail: "检查时长、黑帧、字幕越界与音频峰值，全部通过。" },
  ],
};

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

const CHINESE_VIDEO_COUNTS: Record<string, number> = {
  "一": 1,
  "二": 2,
  "两": 2,
  "三": 3,
  "四": 4,
  "五": 5,
};

function inferRequestedVideoCount(text: string) {
  const patterns = [
    /([1-5一二两三四五])\s*(?:条|个|份)\s*(?:视频|成片|版本)/i,
    /([1-5一二两三四五])\s*版/i,
    /(?:生成|制作|输出|给我)\s*([1-5一二两三四五])\s*条/i,
    /(?:make|create|generate)\s+(?:me\s+)?([1-5])\s+(?:videos?|versions?)/i,
  ];
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (!match) continue;
    return Number(match[1]) || CHINESE_VIDEO_COUNTS[match[1]] || 1;
  }
  return 1;
}

function defaultsFrom(ui: Record<string, unknown>, fallbackVoice: string, fallbackFont: string): Draft {
  return {
    video_language: "",
    target_duration: "auto",
    video_aspect: "9:16",
    material_strategy: "ai_generated",
    voice_name: String(ui.voice_name || fallbackVoice),
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
    refetchInterval: (current) => (isTaskSettled(current.state.data) ? false : 2500),
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
        {task.data?.cross_post_state ? (
          <div className={`publish-status ${task.data.cross_post_state === "complete" ? "ready" : ""}`}>
            自动发布：{crossPostStatusLabel(task.data.cross_post_state)}
            {task.data.cross_post_error ? ` · ${task.data.cross_post_error}` : ""}
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
    </>
  );
}

function DemoActionLog({
  actions,
  activeAction,
  complete,
}: {
  actions: Array<{ tool: string; label: string; detail: string }>;
  activeAction: number;
  complete: boolean;
}) {
  return (
    <div className="demo-action-log">
      <div className="demo-action-log-head">
        <span>Agent 执行记录</span>
        <strong>{complete ? `${actions.length} / ${actions.length}` : `${activeAction + 1} / ${actions.length}`}</strong>
      </div>
      {actions.map((action, index) => {
        const state = complete || index < activeAction ? "completed" : index === activeAction ? "running" : "pending";
        return (
          <div className={`demo-action-row ${state}`} key={action.tool}>
            <div className="demo-action-state">
              {state === "completed" ? <Icons.check /> : state === "running" ? <span className="tool-spinner" /> : <span className="tool-dot" />}
            </div>
            <div className="demo-action-copy">
              <div><strong>{action.label}</strong><code>{action.tool}</code></div>
              <p>{action.detail}</p>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function DemoStageVisual({ stageKey, progress }: { stageKey: string; progress: number }) {
  if (stageKey === "preflight") {
    return (
      <div className="demo-spec-grid">
        <div><span>内容类型</span><strong>城市生活方式</strong></div>
        <div><span>目标受众</span><strong>18–35 岁读者</strong></div>
        <div><span>成片规格</span><strong>9:16 · 约 30 秒</strong></div>
        <div><span>叙事情绪</span><strong>温暖 · 松弛 · 好奇</strong></div>
      </div>
    );
  }
  if (stageKey === "script") {
    return (
      <div className="demo-script-preview">
        <div><span>01</span><p>城市入夜后，还有一盏灯，专门为不舍得睡的人亮着。</p></div>
        <div><span>02</span><p>推开门，旧书页的气味和轻爵士一起，把街上的匆忙留在身后。</p></div>
        <div><span>03</span><p>今晚，给自己二十分钟，去遇见一本还不知道名字的书。</p></div>
      </div>
    );
  }
  if (stageKey === "director") {
    return (
      <div className="demo-metric-grid">
        <div><span>叙事节奏</span><strong>慢入 · 快切 · 留白</strong></div>
        <div><span>旁白语速</span><strong>1.05×</strong></div>
        <div><span>镜头规划</span><strong>6 镜 · 平均 5 秒</strong></div>
        <div><span>转场策略</span><strong>硬切 + 淡入</strong></div>
        <div><span>音乐方向</span><strong>温暖轻爵士</strong></div>
        <div><span>配乐音量</span><strong>自动压低至 18%</strong></div>
      </div>
    );
  }
  if (stageKey === "scenes" || stageKey === "materials") {
    return (
      <div className="demo-scene-grid">
        {DEMO_SCENES.map((scene, index) => (
          <figure key={scene.number}>
            <img
              src="/demo/bookstore-night.jpg"
              alt=""
              style={{ objectPosition: `${scene.position} center` }}
            />
            <figcaption>
              <span>{scene.number} · {scene.time}</span>
              <strong>{scene.label}</strong>
              {stageKey === "materials" ? <em>{96 - index * 2}% 匹配</em> : null}
            </figcaption>
          </figure>
        ))}
      </div>
    );
  }
  if (stageKey === "audio") {
    return (
      <div className="demo-audio-preview">
        <div className="demo-audio-head"><span>旁白 · mimo female</span><strong>00:28.6</strong></div>
        <div className="demo-waveform" aria-hidden="true">
          {[22, 46, 68, 34, 78, 55, 88, 42, 64, 30, 74, 92, 48, 66, 38, 82, 58, 72, 28, 52, 84, 44, 62, 36, 76, 50, 70, 26].map((height, index) => (
            <i key={index} style={{ height: `${height}%` }} />
          ))}
        </div>
        <div className="demo-mix-row">
          <span>人声增强</span><div><i style={{ width: "82%" }} /></div><strong>-14 LUFS</strong>
        </div>
        <div className="demo-mix-row music">
          <span>背景音乐</span><div><i style={{ width: "44%" }} /></div><strong>18%</strong>
        </div>
      </div>
    );
  }
  if (stageKey === "subtitle") {
    return (
      <div className="demo-subtitle-preview">
        <img src="/demo/bookstore-night.jpg" alt="夜间书店演示画面" />
        <div className="demo-subtitle-safe-area" />
        <p>城市入夜后，还有一盏灯<br />专门为不舍得睡的人亮着</p>
        <span>字幕安全区 · 自动断句 · 描边增强</span>
      </div>
    );
  }
  if (stageKey === "timeline") {
    return (
      <div className="demo-timeline-preview">
        <div className="demo-time-ruler"><span>00:00</span><span>00:10</span><span>00:20</span><span>00:30</span></div>
        <div className="demo-track"><b>视频</b><div className="demo-video-clips">{DEMO_SCENES.map((scene) => <i key={scene.number}>{scene.number}</i>)}</div></div>
        <div className="demo-track"><b>旁白</b><div className="demo-track-line voice"><i /></div></div>
        <div className="demo-track"><b>字幕</b><div className="demo-caption-clips">{[1, 2, 3, 4, 5, 6].map((item) => <i key={item} />)}</div></div>
        <div className="demo-track"><b>音乐</b><div className="demo-track-line music"><i /></div></div>
      </div>
    );
  }
  return (
    <div className="demo-export-preview">
      <div className="demo-phone-frame">
        <img src="/demo/bookstore-night.jpg" alt="夜间书店竖屏成片预览" />
        <div className="demo-phone-caption">今晚，去遇见一本<br />还不知道名字的书</div>
        <span>00:30</span>
      </div>
      <div className="demo-export-stats">
        <div><span>渲染进度</span><strong>{Math.round(progress)}%</strong></div>
        <div className="demo-export-progress"><i style={{ width: `${progress}%` }} /></div>
        <dl>
          <div><dt>画面</dt><dd>1080 × 1920</dd></div>
          <div><dt>帧率</dt><dd>30 fps</dd></div>
          <div><dt>编码</dt><dd>H.264</dd></div>
          <div><dt>质量检查</dt><dd>{progress >= 100 ? "通过" : "进行中"}</dd></div>
        </dl>
      </div>
    </div>
  );
}

function DemoAgentTurn({
  prompt,
  onComplete,
}: {
  prompt: string;
  onComplete: () => void;
}) {
  const [elapsedMs, setElapsedMs] = useState(0);
  const [paused, setPaused] = useState(false);
  const turnRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLElement>(null);
  const complete = elapsedMs >= DEMO_TOTAL_DURATION;
  let consumedMs = 0;
  let activeStep = DEMO_STEPS.length - 1;
  if (!complete) {
    activeStep = DEMO_STEPS.findIndex((step) => {
      consumedMs += step.duration;
      return elapsedMs < consumedMs;
    });
    consumedMs -= DEMO_STEPS[activeStep].duration;
  } else {
    consumedMs = DEMO_TOTAL_DURATION - DEMO_STEPS[activeStep].duration;
  }
  const currentStep = DEMO_STEPS[activeStep];
  const stageProgress = complete
    ? 100
    : Math.min(100, ((elapsedMs - consumedMs) / currentStep.duration) * 100);
  const overallProgress = Math.min(100, (elapsedMs / DEMO_TOTAL_DURATION) * 100);
  const currentActions = DEMO_ACTIONS[currentStep.key] || [];
  const activeAction = complete
    ? Math.max(0, currentActions.length - 1)
    : Math.min(currentActions.length - 1, Math.floor((stageProgress / 100) * currentActions.length));
  const steps: StreamStep[] = DEMO_STEPS.map((step, index) => ({
    key: step.key,
    label: step.label,
    state: index < activeStep || complete ? "completed" : index === activeStep ? "running" : "pending",
    meta: index <= activeStep || complete ? step.meta : "",
    elapsed_label: index < activeStep || complete ? step.elapsed : "",
  }));

  useEffect(() => {
    if (paused || complete) return;
    const interval = window.setInterval(() => {
      setElapsedMs((current) => Math.min(DEMO_TOTAL_DURATION, current + 80));
    }, 80);
    return () => window.clearInterval(interval);
  }, [complete, paused]);

  useEffect(() => {
    if (complete) onComplete();
  }, [complete, onComplete]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      turnRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      stageRef.current?.scrollTo({ behavior: "smooth", top: 0 });
    }, 80);
    return () => window.clearTimeout(timeout);
  }, [activeStep]);

  return (
    <div className="demo-turn" ref={turnRef}>
      <div className="user-bubble">{prompt}</div>
      <div className="demo-console">
        <div className="demo-console-head">
          <div><i className={complete ? "complete" : ""} /><strong>自动剪辑 Agent</strong></div>
          <div className="demo-console-status">
            <span>{complete ? "演示完成" : paused ? "演示已暂停" : `正在执行 ${activeStep + 1} / ${DEMO_STEPS.length}`}</span>
            {!complete ? (
              <button type="button" onClick={() => setPaused((value) => !value)}>
                {paused ? "继续" : "暂停"}
              </button>
            ) : null}
          </div>
        </div>
        <div className="demo-overall-progress"><i style={{ width: `${overallProgress}%` }} /></div>
        <div className="demo-workbench">
          <aside className="demo-step-rail"><AgentSteps steps={steps} /></aside>
          <section className="demo-stage" aria-live="polite" ref={stageRef}>
            <div className="demo-stage-head">
              <div><span>{complete ? "最终产出" : "当前任务"}</span><h3>{complete ? "成片已准备就绪" : currentStep.label}</h3></div>
              <strong>{complete ? "100%" : `${Math.round(stageProgress)}%`}</strong>
            </div>
            <p className="demo-stage-detail">
              {complete ? "全部创作与剪辑步骤已完成，成片规格和发布前检查均已通过。" : currentStep.detail}
            </p>
            <DemoActionLog actions={currentActions} activeAction={activeAction} complete={complete} />
            <div className="demo-stage-output">
              <span>阶段产出</span>
              <DemoStageVisual key={currentStep.key} stageKey={currentStep.key} progress={stageProgress} />
            </div>
          </section>
        </div>
        <div className="demo-console-foot">
          <span>{complete ? "9 个阶段全部完成" : `已完成 ${activeStep} 个阶段`}</span>
          <span>演示模式 · 未调用真实生成服务</span>
        </div>
      </div>
    </div>
  );
}

function GenerateSession() {
  const [prompt, setPrompt] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [demoRunning, setDemoRunning] = useState(false);
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

  const finishDemo = useCallback(() => setDemoRunning(false), []);

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
    const requestedVideoCount = inferRequestedVideoCount(text);
    try {
      const data = await mutation.mutateAsync({
        ...parts,
        video_script_prompt: "",
        video_language: draft.video_language,
        target_duration: draft.target_duration,
        video_aspect: draft.video_aspect,
        material_strategy: draft.material_strategy,
        ...(requestedVideoCount > 1 ? { video_count: requestedVideoCount } : {}),
        voice_name: draft.voice_name,
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
        sonilo_bgm_prompt: draft.sonilo_bgm_prompt,
        ai_director_enabled: true,
      });
      setTurns((current) => [...current, { kind: "task", prompt: text, taskId: data.task_id }]);
      setPrompt("");
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    }
  };

  const startDemo = () => {
    if (demoRunning) return;
    const text = prompt.trim() || DEMO_PROMPT;
    setError("");
    setDemoRunning(true);
    setTurns((current) => [
      ...current,
      { kind: "demo", prompt: text, id: `demo-${Date.now()}` },
    ]);
    setPrompt("");
    setOpen(false);
  };

  const durationLabel = DURATIONS.find((item) => item[0] === draft?.target_duration)?.[1] || "自动";
  const strategyLabel = STRATEGIES.find((item) => item[0] === draft?.material_strategy)?.[1] || "AI 分镜";

  const composer = (
    <div className="composer-dock">
      <div className="composer">
        {open && draft ? (
          <div className="sheet">
            <div className="agent-settings-grid">
              <label className="agent-setting-field">
                <span>语言</span>
                <select
                  value={draft.video_language}
                  onChange={(event) => patch("video_language", event.target.value)}
                >
                  <option value="">自动识别</option>
                  <option value="zh-CN">中文</option>
                  <option value="en-US">英语</option>
                  <option value="ja-JP">日语</option>
                </select>
              </label>
              <label className="agent-setting-field">
                <span>配音</span>
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
              </label>
              <label className="agent-setting-field">
                <span>配乐</span>
                <select value={draft.bgm_type} onChange={(event) => patch("bgm_type", event.target.value)}>
                  <option value="random">随机配乐</option>
                  <option value="">无配乐</option>
                  <option value="sonilo">Sonilo</option>
                </select>
              </label>
              {draft.subtitle_enabled ? (
                <>
                  <label className="agent-setting-field">
                    <span>字幕字体</span>
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
                  </label>
                  <label className="agent-setting-field">
                    <span>字幕位置</span>
                    <select
                      value={draft.subtitle_position}
                      onChange={(event) => patch("subtitle_position", event.target.value)}
                    >
                      <option value="top">顶部</option>
                      <option value="center">居中</option>
                      <option value="bottom">底部</option>
                      <option value="custom">自定义</option>
                    </select>
                  </label>
                </>
              ) : null}
            </div>
            {draft.bgm_type === "sonilo" ? (
              <label className="agent-setting-field">
                <span>配乐描述</span>
                <input
                  value={draft.sonilo_bgm_prompt}
                  onChange={(event) => patch("sonilo_bgm_prompt", event.target.value)}
                  placeholder="例如：轻快、温暖、有节奏感"
                />
              </label>
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
        <div className="composer-input-row">
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
          <button
            className="demo-button"
            type="button"
            disabled={demoRunning || mutation.isPending}
            onClick={startDemo}
          >
            <Icons.sparkle />
            <span>{demoRunning ? "演示中" : "演示"}</span>
          </button>
        </div>
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
          <button
            className="icon-btn"
            type="button"
            aria-label="高级设置"
            title="高级设置"
            onClick={() => setOpen((value) => !value)}
          >
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
          {turns.map((turn) =>
            turn.kind === "demo" ? (
              <DemoAgentTurn
                key={turn.id}
                prompt={turn.prompt}
                onComplete={finishDemo}
              />
            ) : (
              <AgentTurn key={turn.taskId} prompt={turn.prompt} taskId={turn.taskId} onUpdate={scrollToEnd} />
            ),
          )}
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
