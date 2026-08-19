from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

from app.models.schema import VideoAspect
from app.services import generation_report, seedance
from app.services.material_library import get_material_library
from app.services.task_store import TaskStoreError, get_task_store
from app.utils import utils


def _scene_index(scene: Mapping[str, Any], position: int) -> int:
    try:
        return int(scene.get("scene_index", position))
    except (TypeError, ValueError):
        return position


def _find_scene(task: Mapping[str, Any], scene_index: int) -> dict[str, Any]:
    for position, scene in enumerate(task.get("scene_plan") or []):
        if not isinstance(scene, Mapping):
            continue
        if _scene_index(scene, position) == int(scene_index):
            return dict(scene)
    raise TaskStoreError("scene does not exist in this task")


def _scene_prompt(scene: Mapping[str, Any]) -> str:
    return str(
        scene.get("material_prompt")
        or scene.get("seedance_prompt")
        or scene.get("video_prompt")
        or scene.get("text")
        or ""
    ).strip()


def _scene_report(
    scene: Mapping[str, Any], details: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "scene_index": int(scene.get("scene_index", 0)),
        "text": str(scene.get("text") or "")[:500],
        "prompt": _scene_prompt(scene)[:4000],
        **dict(details),
    }


def _archive_generated_scene(
    task_id: str, scene: Mapping[str, Any], file_path: str
) -> None:
    try:
        get_material_library().import_task_scene(
            task_id,
            int(scene.get("scene_index", 0)),
            file_path,
            scene_text=str(scene.get("text") or ""),
            search_query=str(scene.get("search_query") or ""),
            provider="seedance",
        )
    except Exception as exc:
        logger.warning(
            "failed to archive regenerated Seedance scene, "
            f"task_id={task_id}, scene={scene.get('scene_index')}, error={exc}"
        )


def _run_scene_generation(
    task_id: str,
    scene_index: int,
    run_id: str,
    provider_task_id: str = "",
) -> None:
    store = get_task_store()
    task = store.get_task(task_id) or {}
    try:
        scene = _find_scene(task, scene_index)
        prompt = _scene_prompt(scene)
        if not prompt:
            raise TaskStoreError("scene generation prompt is empty")
        duration = max(0.1, float(scene.get("target_duration") or 0.1))
        aspect_ratio = VideoAspect(
            (task.get("params") or {}).get("video_aspect")
            or VideoAspect.portrait.value
        ).value
        output_path = str(
            Path(utils.task_dir(task_id))
            / "materials"
            / f"scene-{int(scene_index):04d}-seedance-retry.mp4"
        )
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        store.update_scene_generation_job(
            task_id,
            scene_index,
            run_id,
            status="processing",
            provider_task_id=provider_task_id or None,
        )
        generation_report.initialize(
            task_id,
            details={"manual_scene_generation_enabled": True},
        )

        def persist_provider_task(created_task_id: str) -> None:
            store.update_scene_generation_job(
                task_id,
                scene_index,
                run_id,
                status="submitted",
                provider_task_id=created_task_id,
            )
            generation_report.record_paid_operation(
                task_id,
                {
                    "type": "seedance_scene",
                    "name": f"Seedance 分镜 {int(scene_index) + 1}",
                    "scene_index": int(scene_index),
                    "provider_task_id": created_task_id,
                    "status": "submitted",
                    "auto_approved": False,
                    "trigger": "user_scene_retry",
                },
            )

        if provider_task_id:
            generation_report.record_paid_operation(
                task_id,
                {
                    "type": "seedance_scene",
                    "name": f"Seedance 分镜 {int(scene_index) + 1}",
                    "scene_index": int(scene_index),
                    "provider_task_id": provider_task_id,
                    "status": "resumed",
                    "auto_approved": False,
                    "trigger": "user_scene_retry",
                },
            )
        with generation_report.stage(
            task_id,
            f"seedance_scene_retry_{int(scene_index)}",
            f"重新生成分镜 {int(scene_index) + 1}",
            details={
                "scene_index": int(scene_index),
                "provider_task_id": provider_task_id or None,
                "trigger": "user_scene_retry",
            },
        ) as stage_details:
            generated, provider_task_id, details = seedance.generate_clip_detailed(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                duration=duration,
                output_path=output_path,
                provider_task_id=provider_task_id,
                task_created_callback=persist_provider_task,
            )
            stage_details.update(
                provider_task_id=provider_task_id,
                tokens=details.get("tokens"),
                estimated_cost_cny=details.get("estimated_cost_cny"),
            )
        store.complete_scene_generation(
            task_id,
            scene_index,
            run_id,
            generated,
            provider_task_id=provider_task_id,
            details=details,
        )
        generation_report.record_seedance_scene(
            task_id, _scene_report(scene, details)
        )
        generation_report.record_paid_operation(
            task_id,
            {
                "type": "seedance_scene",
                "name": f"Seedance 分镜 {int(scene_index) + 1}",
                "scene_index": int(scene_index),
                "provider_task_id": provider_task_id,
                "status": "completed",
                "auto_approved": False,
                "trigger": "user_scene_retry",
                "billable": details.get("billable"),
                "tokens": details.get("tokens"),
                "estimated_cost_cny": details.get("estimated_cost_cny"),
            },
        )
        _archive_generated_scene(task_id, scene, generated)
    except Exception as exc:
        details = dict(getattr(exc, "details", None) or {})
        details.setdefault("provider_task_id", provider_task_id or None)
        details.setdefault("status", "failed")
        details.setdefault("billable", None)
        details.setdefault("tokens", None)
        details.setdefault("estimated_cost_cny", None)
        details.setdefault("calculation_method", "unavailable")
        error = str(exc)[:1000]
        logger.warning(
            "single Seedance scene generation failed, "
            f"task_id={task_id}, scene={scene_index}, error={error}"
        )
        try:
            scene = _find_scene(store.get_task(task_id) or task, scene_index)
            generation_report.record_seedance_scene(
                task_id, _scene_report(scene, details)
            )
            generation_report.record_paid_operation(
                task_id,
                {
                    "type": "seedance_scene",
                    "name": f"Seedance 分镜 {int(scene_index) + 1}",
                    "scene_index": int(scene_index),
                    "provider_task_id": details.get("provider_task_id"),
                    "status": "failed",
                    "auto_approved": False,
                    "trigger": "user_scene_retry",
                    "billable": details.get("billable"),
                    "tokens": details.get("tokens"),
                    "estimated_cost_cny": details.get("estimated_cost_cny"),
                    "error": error,
                },
            )
        finally:
            store.fail_scene_generation(
                task_id,
                scene_index,
                run_id,
                error,
                details=details,
            )


def request_scene_generation(task_id: str, scene_index: int) -> dict[str, Any]:
    """提交一个后台单分镜生成操作；同一分镜同时只允许一个运行。"""
    store = get_task_store()
    job = store.reserve_scene_generation(task_id, int(scene_index))
    thread = threading.Thread(
        target=_run_scene_generation,
        args=(
            task_id,
            int(scene_index),
            str(job["run_id"]),
            str(job.get("provider_task_id") or ""),
        ),
        name=f"seedance-scene-{task_id[:8]}-{int(scene_index)}",
        daemon=True,
    )
    try:
        thread.start()
    except Exception as exc:
        store.fail_scene_generation(
            task_id,
            int(scene_index),
            str(job["run_id"]),
            f"failed to start scene generation worker: {exc}",
        )
        raise
    return job
