from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from app.models import const


PIPELINE_STEPS = (
    ("queued", "提交任务"),
    ("preflight", "检查生成配置"),
    ("script", "生成文案"),
    ("director_plan", "规划分镜"),
    ("audio", "生成旁白"),
    ("subtitle", "生成字幕"),
    ("materials", "生成视频分镜"),
    ("video", "合成作品"),
    ("completed", "交付作品"),
)

_STAGE_ALIASES = {
    "starting": "preflight",
    "terms": "director_plan",
    "awaiting_approval": "materials",
    "awaiting_material": "materials",
    "awaiting_supplemental_scenes": "materials",
    "cancellation_requested": "materials",
}

_WAITING_STATUSES = {
    const.TASK_STATUS_AWAITING_APPROVAL,
    const.TASK_STATUS_AWAITING_MATERIAL,
}


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_stage(stage: Any) -> str:
    normalized = str(stage or "").strip()
    return _STAGE_ALIASES.get(normalized, normalized)


def _elapsed_seconds(entry: Mapping[str, Any], now: datetime) -> float | None:
    value = entry.get("elapsed_seconds")
    if value is not None:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
    started_at = _parse_datetime(entry.get("started_at"))
    if not started_at:
        return None
    return max(0.0, (now - started_at).total_seconds())


def format_elapsed(value: Any) -> str:
    try:
        seconds = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return ""
    if seconds < 1:
        return "<1 秒"
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


def _step_meta(stage: str, entry: Mapping[str, Any]) -> str:
    details = entry.get("details")
    if not isinstance(details, Mapping):
        return ""

    if stage == "script" and details.get("characters") is not None:
        return f"{int(details['characters'])} 字"
    if stage == "audio" and details.get("duration_seconds") is not None:
        return f"约 {float(details['duration_seconds']):.1f} 秒"
    if stage == "subtitle":
        if details.get("subtitle_enabled") is False:
            return "未启用字幕"
        if details.get("output_path"):
            return "字幕已生成"
    if stage == "materials" and details.get("material_count") is not None:
        return f"{int(details['material_count'])} 个分镜"
    if stage == "video" and details.get("final_videos") is not None:
        return f"{int(details['final_videos'])} 个作品"
    return ""


def build_task_stream(
    task: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """将任务快照转换为可直接渲染的稳定流水线视图。"""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    status = str(task.get("status") or const.TASK_STATUS_QUEUED)
    report = task.get("generation_report")
    report = report if isinstance(report, Mapping) else {}
    raw_entries = report.get("stage_timings") or []

    entries_by_stage: dict[str, list[Mapping[str, Any]]] = {}
    active_stage = ""
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            continue
        stage = _canonical_stage(raw_entry.get("stage"))
        if not stage:
            continue
        entries_by_stage.setdefault(stage, []).append(raw_entry)
        if raw_entry.get("status") == "processing":
            active_stage = stage

    task_stage = _canonical_stage(task.get("stage"))
    if not active_stage and status in {
        const.TASK_STATUS_PROCESSING,
        const.TASK_STATUS_CANCELLATION_REQUESTED,
    }:
        active_stage = task_stage or "preflight"

    failed_stage = _canonical_stage(
        task.get("failed_stage") or task.get("stage") or active_stage
    )
    waiting_stage = "materials" if status in _WAITING_STATUSES else ""
    cancelled = status == const.TASK_STATUS_CANCELLED
    completed = status == const.TASK_STATUS_COMPLETED

    steps = []
    for stage, label in PIPELINE_STEPS:
        stage_entries = entries_by_stage.get(stage, [])
        latest_entry = stage_entries[-1] if stage_entries else {}
        step_state = "pending"

        if stage == "queued":
            step_state = "running" if status == const.TASK_STATUS_QUEUED else "completed"
        elif stage == "completed":
            if completed:
                step_state = "completed"
            elif cancelled:
                step_state = "cancelled"
        elif completed and not raw_entries:
            # 兼容升级前已完成、但没有阶段报告的历史任务。既然作品已经存在，
            # 中间步骤不应在界面上继续显示为待执行。
            step_state = "completed"
        elif stage_entries:
            latest_status = str(latest_entry.get("status") or "")
            if latest_status == "processing":
                step_state = "running"
            elif latest_status == "failed":
                step_state = "failed"
            elif latest_status == "completed":
                step_state = "completed"

        if stage == active_stage and step_state == "pending":
            step_state = "running"
        if stage == waiting_stage:
            step_state = "waiting"
        if status == const.TASK_STATUS_FAILED and stage == failed_stage:
            step_state = "failed"
        if cancelled and stage == active_stage:
            step_state = "cancelled"

        elapsed_values = [
            _elapsed_seconds(entry, now)
            for entry in stage_entries
            if isinstance(entry, Mapping)
        ]
        elapsed = sum(value for value in elapsed_values if value is not None)
        if stage == "queued" and step_state == "running":
            created_at = _parse_datetime(task.get("created_at"))
            elapsed = max(0.0, (now - created_at).total_seconds()) if created_at else 0.0

        steps.append(
            {
                "key": stage,
                "label": label,
                "state": step_state,
                "elapsed_seconds": elapsed if elapsed_values or stage == "queued" else None,
                "elapsed_label": format_elapsed(elapsed)
                if elapsed_values or stage == "queued"
                else "",
                "meta": _step_meta(stage, latest_entry),
            }
        )

    active_step = next(
        (
            step
            for step in steps
            if step["state"] in {"running", "waiting", "failed", "cancelled"}
        ),
        None,
    )
    if completed:
        headline = "作品已生成"
    elif status == const.TASK_STATUS_FAILED:
        headline = "作品生成失败"
    elif cancelled:
        headline = "任务已取消"
    elif status == const.TASK_STATUS_AWAITING_MATERIAL:
        headline = "等待上传分镜素材"
    elif status == const.TASK_STATUS_AWAITING_APPROVAL:
        headline = "等待确认后继续"
    elif status == const.TASK_STATUS_CANCELLATION_REQUESTED:
        headline = "正在停止任务"
    elif status == const.TASK_STATUS_QUEUED:
        headline = "任务正在排队"
    else:
        headline = "正在生成作品"

    return {
        "headline": headline,
        "status": status,
        "progress": max(0, min(100, int(task.get("progress", 0) or 0))),
        "active_label": active_step["label"] if active_step else "",
        "is_live": status
        in {
            const.TASK_STATUS_QUEUED,
            const.TASK_STATUS_PROCESSING,
            const.TASK_STATUS_CANCELLATION_REQUESTED,
        },
        "steps": steps,
    }
