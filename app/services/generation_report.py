from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

from app.services.task_store import get_task_store
from app.utils import utils


REPORT_VERSION = 1
OFFICIAL_PRICING_URL = "https://www.volcengine.com/docs/82379/1544106"
OFFICIAL_PRICING_RETRIEVED_AT = "2026-07-22"
_locks_guard = threading.Lock()
_task_locks: dict[str, threading.RLock] = {}
_REPORT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
_LEGACY_SEEDANCE_BUDGET_FIELDS = {
    "budget_limit_cny",
    "reserved_cost_cny",
    "remaining_budget_cny",
    "budget_status",
    "budget_has_unknown_cost",
    "budget_reservations",
    "next_scene_index",
    "next_scene_estimated_cost_cny",
    "next_scene_reserved_cost_cny",
    "projected_cost_cny",
    "budget_estimate",
}


def _report_now() -> str:
    return datetime.now(_REPORT_TIMEZONE).isoformat(timespec="seconds")


def _lock_for(task_id: str) -> threading.RLock:
    with _locks_guard:
        return _task_locks.setdefault(task_id, threading.RLock())


def _base_report(task_id: str) -> dict[str, Any]:
    now = _report_now()
    return {
        "version": REPORT_VERSION,
        "task_id": task_id,
        "status": "processing",
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
        "total_elapsed_seconds": None,
        "duration_limit_seconds": None,
        "stage_timings": [],
        "paid_operations": [],
        "seedance_billing": {
            "currency": "CNY",
            "pricing_source": {
                "type": "official_document",
                "url": OFFICIAL_PRICING_URL,
                "retrieved_at": OFFICIAL_PRICING_RETRIEVED_AT,
            },
            "official_rule": (
                "仅对成功生成的视频计费；准确用量以 API 返回的 "
                "usage.completion_tokens 为准，缺失时按官方公式估算"
            ),
            "scenes": [],
            "total_tokens": 0,
            "estimated_total_cost_cny": 0.0,
            "has_unknown_cost": False,
            "is_estimate": False,
        },
    }


def _remove_legacy_seedance_budget_fields(report: dict[str, Any]) -> None:
    billing = report.get("seedance_billing")
    if not isinstance(billing, dict):
        return
    for field in _LEGACY_SEEDANCE_BUDGET_FIELDS:
        billing.pop(field, None)


def _get_report(task_id: str) -> dict[str, Any] | None:
    try:
        task = get_task_store().get_task(task_id)
    except Exception as exc:
        logger.warning(
            f"failed to read generation report, task_id: {task_id}, error: {exc}"
        )
        return None
    if not task:
        return None
    report = task.get("generation_report")
    return dict(report) if isinstance(report, dict) else None


def _write_report_file(task_id: str, report: dict[str, Any]) -> None:
    report_path = Path(utils.task_dir(task_id)) / "generation-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = report_path.with_suffix(".json.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2, default=str)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, report_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _persist(task_id: str, report: dict[str, Any]) -> None:
    report["updated_at"] = _report_now()
    try:
        get_task_store().patch_task(task_id, generation_report=report)
        _write_report_file(task_id, report)
    except Exception as exc:
        logger.warning(
            f"failed to persist generation report, task_id: {task_id}, error: {exc}"
        )


def initialize(
    task_id: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    with _lock_for(task_id):
        report = _get_report(task_id) or _base_report(task_id)
        _remove_legacy_seedance_budget_fields(report)
        report["status"] = "processing"
        report["completed_at"] = None
        if details:
            report.setdefault("task_details", {}).update(details)
        _persist(task_id, report)
        return report


@contextmanager
def stage(
    task_id: str,
    key: str,
    name: str,
    *,
    details: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    started_at = _report_now()
    started_monotonic = time.monotonic()
    entry = {
        "stage": key,
        "name": name,
        "started_at": started_at,
        "finished_at": None,
        "elapsed_seconds": None,
        "status": "processing",
        "details": dict(details or {}),
        "error": None,
    }
    with _lock_for(task_id):
        report = _get_report(task_id)
        if report:
            report.setdefault("stage_timings", []).append(entry)
            _persist(task_id, report)
    try:
        yield entry["details"]
    except Exception as exc:
        entry["status"] = "failed"
        entry["error"] = f"{type(exc).__name__}: {exc}"[:1000]
        raise
    else:
        entry["status"] = "completed"
    finally:
        entry["finished_at"] = _report_now()
        entry["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 3)
        with _lock_for(task_id):
            report = _get_report(task_id)
            if report:
                stages = report.setdefault("stage_timings", [])
                for index in range(len(stages) - 1, -1, -1):
                    candidate = stages[index]
                    if (
                        candidate.get("stage") == key
                        and candidate.get("started_at") == started_at
                    ):
                        stages[index] = entry
                        break
                _persist(task_id, report)


def record_paid_operation(task_id: str, operation: dict[str, Any]) -> None:
    with _lock_for(task_id):
        report = _get_report(task_id)
        if not report:
            return
        item = {"recorded_at": _report_now(), **operation}
        operations = report.setdefault("paid_operations", [])
        identity = (item.get("type"), item.get("scene_index"), item.get("round"))
        for index, existing in enumerate(operations):
            if (
                existing.get("type"),
                existing.get("scene_index"),
                existing.get("round"),
            ) == identity:
                operations[index] = item
                break
        else:
            operations.append(item)
        _persist(task_id, report)


def _refresh_seedance_totals(billing: dict[str, Any]) -> None:
    scenes = billing.get("scenes") or []
    token_values = [
        int(scene["tokens"])
        for scene in scenes
        if isinstance(scene.get("tokens"), (int, float))
    ]
    cost_values = [
        float(scene["estimated_cost_cny"])
        for scene in scenes
        if isinstance(scene.get("estimated_cost_cny"), (int, float))
    ]
    billing["total_tokens"] = sum(token_values)
    billing["estimated_total_cost_cny"] = round(sum(cost_values), 4)
    billing["has_unknown_cost"] = any(
        scene.get("billable") is None
        or (scene.get("billable") and scene.get("estimated_cost_cny") is None)
        for scene in scenes
    )
    billing["is_estimate"] = any(
        scene.get("calculation_method") != "api_usage"
        for scene in scenes
        if scene.get("billable")
    )


def record_seedance_scene(task_id: str, scene: dict[str, Any]) -> None:
    with _lock_for(task_id):
        report = _get_report(task_id)
        if not report:
            return
        billing = report.setdefault("seedance_billing", _base_report(task_id)["seedance_billing"])
        scenes = billing.setdefault("scenes", [])
        identity = (scene.get("scene_index"), scene.get("provider_task_id"))
        for index, existing in enumerate(scenes):
            if (existing.get("scene_index"), existing.get("provider_task_id")) == identity:
                scenes[index] = scene
                break
        else:
            scenes.append(scene)
        scenes.sort(key=lambda item: int(item.get("scene_index", 10**9)))
        _refresh_seedance_totals(billing)
        _persist(task_id, report)


def finalize(
    task_id: str,
    status: str,
    *,
    error: str | None = None,
) -> dict[str, Any] | None:
    with _lock_for(task_id):
        report = _get_report(task_id)
        if not report:
            return None
        now = datetime.now(_REPORT_TIMEZONE)
        report["status"] = status
        report["completed_at"] = now.isoformat(timespec="seconds")
        if error:
            report["error"] = str(error)[:2000]
        try:
            started = datetime.fromisoformat(str(report["started_at"]))
            report["total_elapsed_seconds"] = round((now - started).total_seconds(), 3)
        except (KeyError, TypeError, ValueError):
            report["total_elapsed_seconds"] = None
        _persist(task_id, report)
        return report
