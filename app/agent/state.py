from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    task_id: str
    stop_at: str
    params: dict[str, Any]
    script: str
    terms: Any
    director_plan: dict[str, Any]
    audio_file: str
    audio_duration: float
    subtitle_path: str
    materials: list[str]
    timeline: dict[str, Any]
    warnings: list[Any]
    result: dict[str, Any] | None
    error: str
    failed_stage: str
    halt: bool
