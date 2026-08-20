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
  stream?: { headline?: string; steps?: StreamStep[] };
};

export type VoiceGroup = { id: string; label: string; voices: string[] };

export type WorkspaceOptions = {
  voice_groups: VoiceGroup[];
  fonts: string[];
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
