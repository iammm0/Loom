export type StreamStep = {
  key: string;
  label: string;
  state: string;
  elapsed_label?: string;
  meta?: string;
};

export type TaskDetail = {
  task_id: string;
  status?: string;
  stage?: string;
  progress?: number;
  error?: string;
  video_subject?: string;
  videos?: string[];
  created_at?: string;
  cross_post_state?: string;
  cross_post_error?: string;
  cross_post_results?: Record<string, unknown>[];
  stream?: { headline?: string; steps?: StreamStep[] };
};

export type VoiceGroup = { id: string; label: string; voices: string[] };

export type ChoiceOption = { id: string; label: string; hint?: string };

export type WorkspaceOptions = {
  voice_groups: VoiceGroup[];
  fonts: string[];
  upload_post_platforms?: ChoiceOption[];
  upload_post_youtube_privacy?: ChoiceOption[];
};

export type Settings = {
  app?: Record<string, unknown>;
  seedance?: Record<string, unknown>;
  azure?: Record<string, unknown>;
  siliconflow?: Record<string, unknown>;
  elevenlabs?: Record<string, unknown>;
  chatterbox?: Record<string, unknown>;
  ui?: Record<string, unknown>;
};

export const STATUS_LABEL: Record<string, string> = {
  queued: "排队",
  processing: "进行中",
  completed: "完成",
  failed: "失败",
  cancelled: "已取消",
  awaiting_approval: "待确认",
  awaiting_material: "待素材",
  cancellation_requested: "取消中",
};

export function statusLabel(status?: string) {
  if (!status) return "";
  return STATUS_LABEL[status] || status;
}

export const CROSS_POST_STATUS_LABEL: Record<string, string> = {
  pending: "待发布",
  processing: "发布中",
  complete: "已发布",
  failed: "发布失败",
};

export function crossPostStatusLabel(status?: string) {
  if (!status) return "";
  return CROSS_POST_STATUS_LABEL[status] || status;
}

export function isTaskSettled(task?: { status?: string; cross_post_state?: string }) {
  const generationDone = ["completed", "failed", "cancelled"].includes(task?.status || "");
  const publishing = ["pending", "processing"].includes(task?.cross_post_state || "");
  return generationDone && !publishing;
}
