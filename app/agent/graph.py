from __future__ import annotations

import math
from os import path
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from loguru import logger

from app.agent.runtime import clear_runtime_bag, runtime_bag
from app.agent.state import AgentState
from app.config import config
from app.models import const
from app.models.schema import VideoParams
from app.services import bgm as bgm_service
from app.services import generation_report
from app.services import material_pipeline
from app.services import sonilo
from app.services import state as sm
from app.services import task as tm
from app.services import video
from app.timeline.compile import build_timeline, save_timeline
from app.tools import run_tool
from app.tools.base import ToolContext
from app.utils import utils


def _params(state: AgentState) -> VideoParams:
    return VideoParams.model_validate(state.get("params") or {})


def _dump_params(params: VideoParams) -> dict[str, Any]:
    return params.model_dump(mode="json", warnings=False)


def _ctx(state: AgentState, **extras: Any) -> ToolContext:
    extras = dict(extras)
    bag = runtime_bag(str(state["task_id"]))
    if "sub_maker" not in extras and "sub_maker" in bag:
        extras["sub_maker"] = bag.get("sub_maker")
    return ToolContext(task_id=str(state["task_id"]), params=_params(state), extras=extras)


def _fail(state: AgentState, stage: str, error: str) -> AgentState:
    result = tm._mark_task_failed(str(state["task_id"]), stage, error)
    return {**state, "halt": True, "failed_stage": stage, "error": error, "result": result}


def _stop(state: AgentState, result: dict[str, Any]) -> AgentState:
    return {**state, "halt": True, "result": result}


def _cancelled(state: AgentState) -> bool:
    return tm._cancel_if_requested(str(state["task_id"]))


def preflight_node(state: AgentState) -> AgentState:
    task_id = str(state["task_id"])
    params = _params(state)
    stop_at = str(state.get("stop_at") or "video")
    checkpoint = sm.state.get_task(task_id) or {}
    recovered = {
        "script": str(checkpoint.get("script") or state.get("script") or ""),
        "terms": checkpoint.get("terms") if checkpoint.get("terms") is not None else state.get("terms"),
        "audio_file": str(checkpoint.get("audio_file") or state.get("audio_file") or ""),
        "audio_duration": checkpoint.get("audio_duration") or state.get("audio_duration"),
        "subtitle_path": str(checkpoint.get("subtitle_path") or state.get("subtitle_path") or ""),
        "materials": checkpoint.get("materials") or state.get("materials") or [],
        "director_plan": checkpoint.get("director_plan") or state.get("director_plan") or {},
        "timeline": checkpoint.get("timeline") or state.get("timeline"),
    }
    if params.material_strategy == "ai_generated":
        tm._force_manual_scene_materials(params)
        state = {**state, "params": _dump_params(params)}
    logger.info(f"start task: {task_id}, stop_at: {stop_at}")
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=5,
        status=const.TASK_STATUS_PROCESSING,
        stage="preflight",
        video_subject=params.video_subject,
        **{key: value for key, value in recovered.items() if value not in (None, "", [])},
    )
    if _cancelled(state):
        return _stop(state, None)
    with generation_report.stage(task_id, "preflight", "任务预检") as details:
        details.update(
            {
                "duration_limit_seconds": None,
                "manual_scene_materials": params.material_strategy == "ai_generated",
                "sonilo_requested": bool(
                    stop_at == "video"
                    and params.bgm_type == "sonilo"
                    and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
                ),
                "video_generation_api_used": False,
                "auto_fill_materials": True,
            }
        )
    if (
        stop_at == "video"
        and params.bgm_type == "sonilo"
        and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume)
        and not sonilo.is_enabled()
    ):
        return _fail(state, "preflight", "Sonilo background music requires an API key")
    return {**state, **recovered, "halt": False}


def script_node(state: AgentState) -> AgentState:
    task_id = str(state["task_id"])
    params = _params(state)
    checkpoint = sm.state.get_task(task_id) or {}
    with generation_report.stage(task_id, "script", "生成文案") as report_details:
        video_script = str(state.get("script") or checkpoint.get("script") or "").strip()
        if video_script:
            report_details["reused_checkpoint"] = True
        else:
            result = run_tool("generate_script", _ctx(state))
            if not result.ok:
                return _fail(state, "script", result.error or "failed to generate video script")
            video_script = str(result.data.get("script") or "")
            report_details["reused_checkpoint"] = False
        report_details["characters"] = len(video_script or "")
    if not video_script:
        return _fail(state, "script", "failed to generate video script")
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=10,
        stage="script",
        script=video_script,
        estimated_narration_duration=tm.llm.estimate_narration_duration(video_script),
    )
    generation_report.initialize(
        task_id,
        details={
            "script_paragraph_number": params.paragraph_number,
            "estimated_narration_duration": tm.llm.estimate_narration_duration(video_script),
        },
    )
    if _cancelled(state):
        return _stop(state, None)
    next_state = {**state, "script": video_script}
    if str(state.get("stop_at") or "video") == "script":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            status=const.TASK_STATUS_COMPLETED,
            stage="completed",
            script=video_script,
        )
        generation_report.finalize(task_id, "completed")
        return _stop(next_state, {"script": video_script})
    return next_state


def director_node(state: AgentState) -> AgentState:
    task_id = str(state["task_id"])
    with generation_report.stage(task_id, "director_plan", "AI 导演与分镜参数规划") as report_details:
        result = run_tool("plan_director", _ctx(state, script=state.get("script")))
        if not result.ok:
            return _fail(state, "director_plan", result.error or "director plan failed")
        plan = result.data.get("director_plan") or {}
        report_details["source"] = plan.get("source")
        report_details["settings"] = plan
    params = result.data.get("params") or _params(state)
    dumped = _dump_params(params) if isinstance(params, VideoParams) else _dump_params(_params(state))
    return {**state, "director_plan": plan, "params": dumped}


def scenes_node(state: AgentState) -> AgentState:
    task_id = str(state["task_id"])
    params = _params(state)
    result = run_tool(
        "plan_scenes",
        _ctx(state, script=state.get("script"), terms=state.get("terms")),
    )
    if not result.ok:
        return _fail(state, "terms", result.error or "failed to generate video search terms")
    terms = result.data.get("terms")
    tm.save_script_data(task_id, str(state.get("script") or ""), terms, params)
    if _cancelled(state):
        return _stop(state, None)
    next_state = {**state, "terms": terms}
    if str(state.get("stop_at") or "video") == "terms":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            status=const.TASK_STATUS_COMPLETED,
            stage="completed",
            terms=terms,
        )
        generation_report.finalize(task_id, "completed")
        return _stop(next_state, {"script": state.get("script"), "terms": terms})
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=20,
        stage="terms",
        terms=terms,
    )
    return next_state


def audio_node(state: AgentState) -> AgentState:
    task_id = str(state["task_id"])
    params = _params(state)
    checkpoint = sm.state.get_task(task_id) or {}
    bag = runtime_bag(task_id)
    with generation_report.stage(task_id, "audio", "生成旁白") as report_details:
        audio_file = str(checkpoint.get("audio_file") or "").strip()
        if not audio_file:
            candidate = path.join(utils.task_dir(task_id), "audio.mp3")
            audio_file = candidate if path.isfile(candidate) else ""
        audio_duration = checkpoint.get("audio_duration")
        if audio_file and path.isfile(audio_file):
            try:
                measured_duration = float(video.get_audio_duration(audio_file))
                if measured_duration <= 0:
                    raise ValueError("audio checkpoint duration is zero")
                audio_duration = measured_duration
                report_details["reused_checkpoint"] = True
            except Exception as exc:
                logger.warning(
                    f"discard invalid audio checkpoint, task_id={task_id}, "
                    f"path={audio_file}, error={exc}"
                )
                report_details["invalid_checkpoint"] = str(exc)[:500]
                audio_file = ""
                audio_duration = None
        if not audio_file:
            result = run_tool("synthesize_speech", _ctx(state, script=state.get("script")))
            if not result.ok:
                return _fail(state, "audio", result.error or "failed to prepare narration audio")
            audio_file = str(result.data.get("audio_file") or "")
            audio_duration = result.data.get("audio_duration")
            bag["sub_maker"] = result.data.get("sub_maker")
            report_details["reused_checkpoint"] = False
        report_details["duration_seconds"] = audio_duration
    if not audio_file:
        return _fail(state, "audio", "failed to prepare narration audio")
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=30,
        stage="audio",
        audio_file=audio_file,
        audio_duration=audio_duration,
        required_visual_duration=video.get_required_video_duration(audio_duration),
        required_scene_count=max(
            1,
            math.ceil(
                video.get_required_video_duration(audio_duration)
                / max(1, int(params.video_clip_duration or 1))
            ),
        ),
    )
    generation_report.initialize(
        task_id,
        details={
            "required_visual_duration": video.get_required_video_duration(audio_duration),
            "required_scene_count": max(
                1,
                math.ceil(
                    video.get_required_video_duration(audio_duration)
                    / max(1, int(params.video_clip_duration or 1))
                ),
            ),
        },
    )
    if _cancelled(state):
        return _stop(state, None)
    next_state = {**state, "audio_file": audio_file, "audio_duration": audio_duration}
    if str(state.get("stop_at") or "video") == "audio":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            status=const.TASK_STATUS_COMPLETED,
            stage="completed",
            audio_file=audio_file,
        )
        generation_report.finalize(task_id, "completed")
        return _stop(next_state, {"audio_file": audio_file, "audio_duration": audio_duration})
    return next_state


def subtitle_node(state: AgentState) -> AgentState:
    task_id = str(state["task_id"])
    params = _params(state)
    checkpoint = sm.state.get_task(task_id) or {}
    bag = runtime_bag(task_id)
    with generation_report.stage(task_id, "subtitle", "生成字幕") as report_details:
        subtitle_path = ""
        if bag.get("sub_maker") is None:
            subtitle_path = str(checkpoint.get("subtitle_path") or "").strip()
            if not subtitle_path:
                candidate = path.join(utils.task_dir(task_id), "subtitle.srt")
                subtitle_path = candidate if path.isfile(candidate) else ""
        if subtitle_path and path.isfile(subtitle_path):
            report_details["reused_checkpoint"] = True
        else:
            result = run_tool(
                "generate_subtitles",
                _ctx(
                    state,
                    script=state.get("script"),
                    audio_file=state.get("audio_file"),
                    sub_maker=bag.get("sub_maker"),
                ),
            )
            subtitle_path = str((result.data or {}).get("subtitle_path") or "")
            report_details["reused_checkpoint"] = False
        report_details["subtitle_enabled"] = bool(params.subtitle_enabled)
        report_details["output_path"] = subtitle_path or None
    if _cancelled(state):
        return _stop(state, None)
    next_state = {**state, "subtitle_path": subtitle_path}
    if str(state.get("stop_at") or "video") == "subtitle":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            status=const.TASK_STATUS_COMPLETED,
            stage="completed",
            subtitle_path=subtitle_path,
        )
        generation_report.finalize(task_id, "completed")
        return _stop(next_state, {"subtitle_path": subtitle_path})
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        progress=40,
        stage="materials",
        subtitle_path=subtitle_path,
    )
    return next_state


def materials_node(state: AgentState) -> AgentState:
    task_id = str(state["task_id"])
    params = _params(state)
    bag = runtime_bag(task_id)
    with generation_report.stage(
        task_id,
        "materials",
        "自动匹配或生成分镜素材",
        details={"video_generation_api_used": False, "auto_fill": True},
    ) as report_details:
        result = run_tool(
            "match_library_materials",
            _ctx(
                state,
                script=state.get("script"),
                terms=state.get("terms"),
                audio_duration=state.get("audio_duration"),
                sub_maker=bag.get("sub_maker"),
            ),
        )
        if not result.ok:
            return _fail(
                state,
                "materials",
                result.error or "failed to prepare video materials",
            )
        materials = list(result.data.get("materials") or [])
        report_details["material_count"] = len(materials)
        material_task = sm.state.get_task(task_id) or {}
        report_details["required_scene_count"] = material_task.get("required_scene_count")
        report_details["planned_scene_count"] = material_task.get("planned_scene_count")
    if not materials:
        return _fail(state, "materials", "failed to prepare video materials")
    if _cancelled(state):
        return _stop(state, None)

    stop_at = str(state.get("stop_at") or "video")
    if stop_at == "video" and all(path.isfile(item) for item in materials):
        latest_task = sm.state.get_task(task_id) or {}
        selected_durations = latest_task.get("selected_material_durations") or None
        visual_duration, effective_durations = video.get_effective_visual_duration(
            materials,
            max_clip_duration=params.video_clip_duration,
            clip_speed=params.video_clip_speed,
            clip_durations=selected_durations,
            concat_mode=tm.VideoConcatMode.sequential,
        )
        required_visual_duration = video.get_required_video_duration(
            float(state.get("audio_duration") or 0)
        )
        sm.state.patch_task(
            task_id,
            visual_duration=visual_duration,
            required_visual_duration=required_visual_duration,
            effective_material_durations=effective_durations,
            shortfall_duration=max(0.0, round(required_visual_duration - visual_duration, 3)),
        )
        if visual_duration + 0.001 < required_visual_duration:
            shortfall = material_pipeline.build_visual_duration_shortfall_scenes(
                params,
                str(state.get("script") or ""),
                scene_plan=list((sm.state.get_task(task_id) or {}).get("scene_plan") or []),
                visual_duration=visual_duration,
                required_visual_duration=required_visual_duration,
            )
            from app.tools.production import _auto_generate_missing

            try:
                _auto_generate_missing(
                    _ctx(state),
                    list(shortfall.get("new_supplemental_scenes") or []),
                )
            except Exception as exc:
                return _fail(state, "materials", str(exc)[:1000])
            refill = run_tool(
                "match_library_materials",
                _ctx(
                    state,
                    script=state.get("script"),
                    terms=state.get("terms"),
                    audio_duration=state.get("audio_duration"),
                    sub_maker=bag.get("sub_maker"),
                ),
            )
            if not refill.ok:
                return _fail(state, "materials", refill.error or "visual duration shortfall")
            materials = list(refill.data.get("materials") or materials)

    if stop_at == "materials":
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            status=const.TASK_STATUS_COMPLETED,
            stage="completed",
            materials=materials,
            audio_duration=state.get("audio_duration"),
        )
        generation_report.finalize(task_id, "completed")
        return _stop({**state, "materials": materials}, {"materials": materials})
    sm.state.update_task(
        task_id, state=const.TASK_STATE_PROCESSING, progress=50, stage="materials"
    )
    return {**state, "materials": materials}


def timeline_node(state: AgentState) -> AgentState:
    params = _params(state)
    task = sm.state.get_task(str(state["task_id"])) or {}
    timeline = build_timeline(
        params=params,
        materials=list(state.get("materials") or []),
        durations=task.get("selected_material_durations") or task.get("effective_material_durations"),
        audio_file=str(state.get("audio_file") or ""),
        audio_duration=float(state.get("audio_duration") or 0),
        subtitle_path=str(state.get("subtitle_path") or ""),
    )
    save_timeline(str(state["task_id"]), timeline)
    sm.state.patch_task(
        str(state["task_id"]),
        timeline=timeline.to_dict(),
        selected_material_durations=timeline.clip_durations(),
    )
    return {**state, "timeline": timeline.to_dict()}


def refine_node(state: AgentState) -> AgentState:
    materials = list(state.get("materials") or [])
    steps = 0
    for source in materials:
        if steps >= 8:
            break
        run_tool("probe_media", _ctx(state, source=source))
        steps += 1
    if config.app.get("rough_cut_enabled") and materials and steps < 8:
        rough = run_tool(
            "rough_cut_silence",
            _ctx(state, source=materials[0], output=path.join(utils.task_dir(str(state["task_id"])), "edits", "rough-cut.mp4")),
        )
        if rough.ok and rough.data.get("path") and not rough.data.get("skipped"):
            materials = [str(rough.data["path"]), *materials[1:]]
            state = {**state, "materials": materials}
    return state


def export_node(state: AgentState) -> AgentState:
    task_id = str(state["task_id"])
    params = _params(state)
    with generation_report.stage(
        task_id,
        "video",
        "拼接画面并合成作品",
        details={"video_count": params.video_count},
    ) as report_details:
        try:
            result = run_tool(
                "export_timeline",
                _ctx(
                    state,
                    timeline=state.get("timeline"),
                    audio_file=state.get("audio_file"),
                    subtitle_path=state.get("subtitle_path"),
                    audio_duration=state.get("audio_duration"),
                ),
            )
        except tm._TaskCancellationCheckpoint:
            report_details["cancelled"] = True
            if _cancelled(state):
                return _stop(state, None)
            raise
        if not result.ok:
            return _fail(state, "video", result.error or "failed to generate final video")
        final_video_paths = list(result.data.get("videos") or [])
        combined_video_paths = list(result.data.get("combined_videos") or [])
        generation_warnings = list(result.data.get("warnings") or [])
        report_details["combined_videos"] = len(combined_video_paths)
        report_details["final_videos"] = len(final_video_paths)
        report_details["warnings"] = generation_warnings or None
    if not final_video_paths:
        return _fail(state, "video", "failed to generate final video")
    if _cancelled(state):
        return _stop(state, None)
    logger.success(f"task {task_id} finished, generated {len(final_video_paths)} videos.")
    kwargs = _complete_video_task(
        task_id,
        params,
        script=str(state.get("script") or ""),
        terms=state.get("terms"),
        audio_file=str(state.get("audio_file") or ""),
        audio_duration=state.get("audio_duration"),
        subtitle_path=str(state.get("subtitle_path") or ""),
        materials=list(state.get("materials") or []),
        final_video_paths=final_video_paths,
        combined_video_paths=combined_video_paths,
        generation_warnings=generation_warnings,
        timeline=state.get("timeline"),
    )
    return _stop({**state, "warnings": generation_warnings}, kwargs)


def _complete_video_task(
    task_id: str,
    params: VideoParams,
    *,
    script: str,
    terms: Any,
    audio_file: str,
    audio_duration: Any,
    subtitle_path: str,
    materials: list[str],
    final_video_paths: list[str],
    combined_video_paths: list[str],
    generation_warnings: list[Any],
    timeline: dict[str, Any] | None,
) -> dict[str, Any]:
    from app.services import upload_post

    cross_post_enabled = (
        upload_post.upload_post_service.is_configured()
        and upload_post.upload_post_service.auto_upload
    )
    platforms = (
        list(upload_post.upload_post_service.platforms) if cross_post_enabled else []
    )
    should_cross_post = cross_post_enabled and bool(platforms)
    if cross_post_enabled and not platforms:
        logger.warning(
            f"skip cross-post because no platforms are configured, task_id: {task_id}"
        )
    cross_post_state = const.CROSS_POST_STATE_PENDING if should_cross_post else None
    kwargs = {
        "status": const.TASK_STATUS_COMPLETED,
        "stage": "completed",
        "videos": final_video_paths,
        "original_videos": final_video_paths,
        "combined_videos": combined_video_paths,
        "script": script,
        "terms": terms,
        "audio_file": audio_file,
        "audio_duration": audio_duration,
        "subtitle_path": subtitle_path,
        "materials": materials,
        "timeline": timeline,
        "cross_post_state": cross_post_state,
        "cross_post_results": None,
        "cross_post_error": None,
        "cross_post_owner": tm._cross_post_process_owner if should_cross_post else None,
        "warnings": generation_warnings or None,
        "model_token_usage": tm._current_model_token_usage(),
    }
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_COMPLETE,
        progress=100,
        **kwargs,
    )
    generation_report.finalize(task_id, "completed")
    if should_cross_post:
        scheduling_error = tm._schedule_cross_post(
            task_id=task_id,
            video_paths=final_video_paths,
            params=params,
            video_script=script,
            platforms=platforms,
            youtube_privacy_status=(
                upload_post.upload_post_service.youtube_privacy_status
            ),
        )
        if scheduling_error:
            kwargs["cross_post_state"] = const.CROSS_POST_STATE_FAILED
            kwargs["cross_post_error"] = scheduling_error
            kwargs["cross_post_owner"] = None
    return kwargs


def _route(state: AgentState, current: str) -> str:
    if state.get("halt"):
        return "end"
    stop_at = str(state.get("stop_at") or "video")
    order = [
        "preflight",
        "script",
        "director",
        "scenes",
        "audio",
        "subtitle",
        "materials",
        "timeline",
        "refine",
        "export",
    ]
    stop_map = {
        "script": "script",
        "terms": "scenes",
        "audio": "audio",
        "subtitle": "subtitle",
        "materials": "materials",
        "video": "export",
    }
    next_index = order.index(current) + 1
    if next_index >= len(order):
        return "end"
    return order[next_index]


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("preflight", preflight_node)
    graph.add_node("script", script_node)
    graph.add_node("director", director_node)
    graph.add_node("scenes", scenes_node)
    graph.add_node("audio", audio_node)
    graph.add_node("subtitle", subtitle_node)
    graph.add_node("materials", materials_node)
    graph.add_node("timeline", timeline_node)
    graph.add_node("refine", refine_node)
    graph.add_node("export", export_node)
    graph.add_edge(START, "preflight")

    def continue_or_end(node_name: str):
        def _inner(state: AgentState) -> Literal["script", "director", "scenes", "audio", "subtitle", "materials", "timeline", "refine", "export", "end"]:
            target = _route(state, node_name)
            return target  # type: ignore[return-value]

        return _inner

    mapping = {
        "script": "script",
        "director": "director",
        "scenes": "scenes",
        "audio": "audio",
        "subtitle": "subtitle",
        "materials": "materials",
        "timeline": "timeline",
        "refine": "refine",
        "export": "export",
        "end": END,
    }
    for node_name in [
        "preflight",
        "script",
        "director",
        "scenes",
        "audio",
        "subtitle",
        "materials",
        "timeline",
        "refine",
    ]:
        graph.add_conditional_edges(node_name, continue_or_end(node_name), mapping)
    graph.add_edge("export", END)
    return graph.compile()


_GRAPH = None


def get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH
