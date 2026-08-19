from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from app.models import const
from app.models.schema import VideoAspect, VideoParams
from app.services import seedance
from app.services import task as tm
from app.services.task_store import get_task_store
from app.tools.base import FunctionTool, ToolContext, ToolResult
from app.utils import utils


def _params(ctx: ToolContext) -> VideoParams:
    return ctx.params


def generate_script(ctx: ToolContext) -> ToolResult:
    script = tm.generate_script(ctx.task_id, _params(ctx))
    if not script or "Error: " in str(script):
        error = (
            str(script).removeprefix("Error: ").strip()
            if isinstance(script, str) and "Error: " in str(script)
            else "failed to generate video script"
        )
        return ToolResult(ok=False, error=error)
    return ToolResult(ok=True, data={"script": script})


def plan_director(ctx: ToolContext) -> ToolResult:
    script = str(ctx.extras.get("script") or "")
    plan = tm.prepare_director_plan(ctx.task_id, _params(ctx), script)
    return ToolResult(ok=True, data={"director_plan": plan, "params": ctx.params})


def plan_scenes(ctx: ToolContext) -> ToolResult:
    params = _params(ctx)
    script = str(ctx.extras.get("script") or "")
    terms = ctx.extras.get("terms")
    if params.material_strategy == "ai_generated":
        return ToolResult(ok=True, data={"terms": ""})
    terms = tm.generate_terms(ctx.task_id, params, script) if not terms else terms
    if not terms:
        return ToolResult(ok=False, error="failed to generate video search terms")
    tm.save_script_data(ctx.task_id, script, terms, params)
    return ToolResult(ok=True, data={"terms": terms})


def synthesize_speech(ctx: ToolContext) -> ToolResult:
    audio_file, audio_duration, sub_maker = tm.generate_audio(
        ctx.task_id, _params(ctx), str(ctx.extras.get("script") or "")
    )
    if not audio_file:
        return ToolResult(ok=False, error="failed to prepare narration audio")
    return ToolResult(
        ok=True,
        data={
            "audio_file": audio_file,
            "audio_duration": audio_duration,
            "sub_maker": sub_maker,
        },
        artifacts=[audio_file],
    )


def generate_subtitles(ctx: ToolContext) -> ToolResult:
    path = tm.generate_subtitle(
        ctx.task_id,
        _params(ctx),
        str(ctx.extras.get("script") or ""),
        ctx.extras.get("sub_maker"),
        str(ctx.extras.get("audio_file") or ""),
    )
    return ToolResult(
        ok=True,
        data={"subtitle_path": path or ""},
        artifacts=[path] if path else [],
    )


def _scene_prompt(scene: dict[str, Any]) -> str:
    return str(
        scene.get("material_prompt")
        or scene.get("video_prompt")
        or scene.get("text")
        or ""
    ).strip()


def generate_scene_video(ctx: ToolContext) -> ToolResult:
    scene = dict(ctx.extras.get("scene") or {})
    scene_index = int(scene.get("scene_index", ctx.extras.get("scene_index") or 0))
    prompt = _scene_prompt(scene)
    if not prompt:
        return ToolResult(ok=False, error="scene generation prompt is empty")
    if not seedance.is_enabled():
        return ToolResult(ok=False, error="Seedance is not configured")
    duration = max(0.1, float(scene.get("target_duration") or 0.1))
    aspect_ratio = VideoAspect(
        ctx.params.video_aspect or VideoAspect.portrait.value
    ).value
    output_path = str(
        Path(utils.task_dir(ctx.task_id))
        / "materials"
        / f"scene-{scene_index:04d}-seedance.mp4"
    )
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        generated, provider_task_id = seedance.generate_clip(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            duration=duration,
            output_path=output_path,
        )
    except Exception as exc:
        return ToolResult(ok=False, error=str(exc)[:1000])
    return ToolResult(
        ok=True,
        data={
            "path": generated,
            "scene_index": scene_index,
            "provider_task_id": provider_task_id,
        },
        artifacts=[generated],
    )


def _auto_generate_missing(ctx: ToolContext, missing: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    generated_paths = {}
    store = get_task_store()
    task = store.get_task(ctx.task_id) or {}
    generated_paths.update(task.get("generated_scene_paths") or {})
    for scene in missing:
        result = generate_scene_video(
            ToolContext(
                task_id=ctx.task_id,
                params=ctx.params,
                extras={"scene": scene},
            )
        )
        if not result.ok:
            errors.append(result.error or "scene generation failed")
            continue
        generated_paths[str(result.data["scene_index"])] = result.data["path"]
    store.patch_task(
        ctx.task_id,
        generated_scene_paths=generated_paths,
        material_upload_confirmed=True,
        missing_scenes=[],
        status=const.TASK_STATUS_PROCESSING,
        stage="materials",
    )
    if errors and len(generated_paths) < len(missing):
        raise RuntimeError("; ".join(errors[:5]))
    return list(generated_paths.values())


def match_library_materials(ctx: ToolContext) -> ToolResult:
    params = _params(ctx)
    script = str(ctx.extras.get("script") or "")
    materials = tm.get_video_materials(
        ctx.task_id,
        params,
        ctx.extras.get("terms") or "",
        float(ctx.extras.get("audio_duration") or 0),
        video_script=script,
        sub_maker=ctx.extras.get("sub_maker"),
    )
    if materials:
        return ToolResult(ok=True, data={"materials": materials, "missing": []})

    from app.services import state as sm

    task = sm.state.get_task(ctx.task_id) or get_task_store().get_task(ctx.task_id) or {}
    missing = list(task.get("missing_scenes") or [])
    if not missing:
        return ToolResult(
            ok=False,
            error=tm._material_preparation_error(
                None, "failed to prepare video materials"
            ),
        )
    try:
        _auto_generate_missing(ctx, missing)
    except Exception as exc:
        return ToolResult(ok=False, error=str(exc)[:1000])

    materials = tm.get_video_materials(
        ctx.task_id,
        params,
        ctx.extras.get("terms") or "",
        float(ctx.extras.get("audio_duration") or 0),
        video_script=script,
        sub_maker=ctx.extras.get("sub_maker"),
    )
    if not materials:
        return ToolResult(
            ok=False,
            error="failed to auto-fill missing scene materials",
        )
    return ToolResult(ok=True, data={"materials": materials, "missing": []})


def generate_bgm(ctx: ToolContext) -> ToolResult:
    from app.services import bgm as bgm_service
    from app.services import sonilo

    params = _params(ctx)
    if params.bgm_type != "sonilo" or not bgm_service.should_use_bgm(
        params.bgm_type, params.bgm_volume
    ):
        return ToolResult(ok=True, data={"bgm_file": params.bgm_file or ""})
    output = str(
        Path(utils.task_dir(ctx.task_id))
        / f"sonilo-bgm-{int(ctx.extras.get('video_index') or 1)}.m4a"
    )
    try:
        sonilo.generate_bgm(
            video_path=str(ctx.extras.get("video_path") or ""),
            output_path=output,
            video_duration=float(ctx.extras.get("audio_duration") or 0),
            prompt=params.sonilo_bgm_prompt,
        )
    except Exception as exc:
        logger.warning(f"Sonilo BGM generation failed: {exc}")
        return ToolResult(ok=True, data={"bgm_file": "", "warning": str(exc)[:500]})
    return ToolResult(ok=True, data={"bgm_file": output}, artifacts=[output])


PRODUCTION_TOOLS = [
    FunctionTool("generate_script", "根据主题生成视频文案", generate_script),
    FunctionTool("plan_director", "规划导演参数与分镜节奏", plan_director),
    FunctionTool("plan_scenes", "生成分镜检索词或分镜规划", plan_scenes),
    FunctionTool("synthesize_speech", "合成旁白音频", synthesize_speech),
    FunctionTool("generate_subtitles", "生成字幕文件", generate_subtitles),
    FunctionTool("match_library_materials", "匹配素材库或自动补齐分镜素材", match_library_materials),
    FunctionTool("search_online_materials", "在线检索分镜素材，缺镜时自动补齐", match_library_materials),
    FunctionTool("generate_scene_video", "用 Seedance 生成单个分镜视频", generate_scene_video),
    FunctionTool("generate_bgm", "生成或解析背景音乐", generate_bgm),
]
