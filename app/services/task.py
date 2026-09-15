import math
import os
import re
import socket
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from functools import partial
from os import path
from uuid import uuid4

from loguru import logger

from app.config import config
from app.models import const
from app.models.schema import VideoConcatMode, VideoParams, VideoTransitionMode
from app.services import bgm as bgm_service
from app.services import (
    generation_report,
    llm,
    material_pipeline,
    seedance,
    sonilo,
    subtitle,
    task_store,
    twelvelabs,
    video,
    voice,
)
from app.services import upload_post
from app.services import state as sm
from app.utils import file_security, utils


# 发布请求最长可等待数分钟，不能继续占用视频生成任务的并发名额。
# 固定大小的线程池将发布吞吐限制在可控范围内，同时让视频产物生成后
# 立即进入完成状态。
_cross_post_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="mpt-cross-post",
)
_cross_post_max_pending_tasks = max(
    1,
    int(config.app.get("upload_post_max_pending_tasks", 10)),
)
_cross_post_slots = threading.BoundedSemaphore(_cross_post_max_pending_tasks)
_cross_post_registry_lock = threading.RLock()
_cross_post_futures: dict[str, Future] = {}
_cross_post_process_owner = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
_ACTIVE_CROSS_POST_STATES = {
    const.CROSS_POST_STATE_PENDING,
    const.CROSS_POST_STATE_PROCESSING,
}
_CROSS_POST_STATE_WRITE_ATTEMPTS = 3
_CROSS_POST_STATE_RETRY_DELAY_SECONDS = 0.1
_INTERRUPTED_CROSS_POST_ERROR = (
    "cross-posting was interrupted before the process completed"
)


class _TaskCancellationCheckpoint(RuntimeError):
    """Internal signal used to leave a multi-operation stage at a safe boundary."""


def _is_task_cancel_requested(task_id: str) -> bool:
    task = sm.state.get_task(task_id) or {}
    return bool(
        task.get("cancel_requested")
        or task.get("status") == const.TASK_STATUS_CANCELLATION_REQUESTED
    )


_VISUAL_REFERENCE_PROVIDER = "visual_reference"
_VISUAL_REFERENCE_TRIGGER_PATTERN = re.compile(
    r"(截图|界面|logo|标志|标识|品牌|图标|上传素材|上传图片|上传截图|"
    r"参考图|素材图|插入素材|插入图片|展示素材|展示截图|展示界面|展示logo|"
    r"使用素材|使用截图|使用logo|引用素材|剪入|剪进|放入素材|放进素材)",
    re.IGNORECASE,
)


def _current_model_token_usage() -> dict:
    return llm.get_collected_token_usage()


def is_task_busy(task: dict | None) -> bool:
    """判断任务是否仍在生成或发布，供所有删除入口复用。"""
    if not task:
        return False

    state = task.get("state")
    try:
        state = int(state)
    except (TypeError, ValueError):
        pass

    # 视频生成和跨平台发布都可能继续读取任务目录。统一视为忙碌状态，
    # 可以避免 API 与 WebUI 分别维护规则后出现一个允许删除、另一个禁止
    # 删除的不一致行为。
    task_status = task.get("status")
    generation_busy = (
        task_status in const.TASK_BUSY_STATUSES
        if task_status
        else state == const.TASK_STATE_PROCESSING
    )
    return generation_busy or task.get("cross_post_state") in _ACTIVE_CROSS_POST_STATES


def apply_material_defaults(
    params: VideoParams, *, stop_at: str = "video"
) -> VideoParams:
    """Fill stock sources from workspace config; skip AI clips without a video API key."""
    explicit_sources = material_pipeline.stock_sources_from(params.video_sources)
    params.video_sources = explicit_sources or material_pipeline.configured_stock_sources()
    if not params.video_source or params.video_source == "manual_upload":
        params.video_source = next(
            (source for source in params.video_sources if source != "local"),
            params.video_sources[0] if params.video_sources else "pexels",
        )
    if (
        stop_at in {"video", "materials"}
        and params.material_strategy == "ai_generated"
        and not seedance.is_enabled()
    ):
        logger.info(
            "video generation API key is not configured; "
            "automatic editing will use configured online stock materials"
        )
        params.material_strategy = "local_first"
    return params


def _force_manual_scene_materials(params: VideoParams) -> None:
    visual_reference_materials = _uploaded_visual_reference_materials(params)
    params.video_source = "manual_upload"
    params.video_sources = ["manual_upload"]
    params.material_strategy = "ai_generated"
    params.video_materials = visual_reference_materials or None
    params.match_materials_to_script = True


def _uploaded_visual_reference_materials(params: VideoParams) -> list:
    materials = []
    for material in params.video_materials or []:
        provider = str(getattr(material, "provider", "") or "").strip()
        url = str(getattr(material, "url", "") or "").strip()
        if provider == _VISUAL_REFERENCE_PROVIDER and url:
            materials.append(material)
    return materials


def _should_insert_uploaded_visuals(params: VideoParams, video_script: str) -> bool:
    if not _uploaded_visual_reference_materials(params):
        return False

    request_text = "\n".join(
        [
            params.video_subject or "",
            params.video_script_prompt or "",
            params.video_script or "",
            video_script or "",
        ]
    )
    return bool(_VISUAL_REFERENCE_TRIGGER_PATTERN.search(request_text))


def _prepare_uploaded_visual_reference_paths(params: VideoParams) -> list[str]:
    materials = _uploaded_visual_reference_materials(params)
    if not materials:
        return []

    prepared_materials = video.preprocess_video(
        materials,
        clip_duration=max(1, int(params.video_clip_duration or 5)),
    )
    paths = [
        str(getattr(material, "url", "") or "").strip()
        for material in prepared_materials
        if str(getattr(material, "url", "") or "").strip()
    ]
    if paths:
        logger.info(f"prepared {len(paths)} uploaded visual reference material(s)")
    return paths


def _merge_uploaded_visuals_with_generated(
    generated_paths: list[str],
    uploaded_visual_paths: list[str],
) -> list[str]:
    if not uploaded_visual_paths:
        return generated_paths
    if not generated_paths:
        return uploaded_visual_paths

    merged = []
    for index, generated_path in enumerate(generated_paths):
        if index < len(uploaded_visual_paths):
            merged.append(uploaded_visual_paths[index])
        merged.append(generated_path)
    merged.extend(uploaded_visual_paths[len(generated_paths) :])
    return merged


def _resolve_material_durations(
    material_paths: list[str],
    persisted_durations: list | None,
    fallback_duration: float,
) -> list[float]:
    """Return one positive playback duration for every material path."""
    durations = []
    persisted = list(persisted_durations or [])
    fallback = max(0.1, float(fallback_duration or 0.1))
    for index, material_path in enumerate(material_paths):
        value = persisted[index] if index < len(persisted) else None
        try:
            duration = float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            try:
                duration = float(video.get_media_duration(material_path))
            except Exception as exc:
                logger.warning(
                    f"failed to probe inserted material duration: {material_path}: {exc}"
                )
                duration = fallback
        durations.append(max(0.1, duration))
    return durations


def _merge_uploaded_visual_durations(
    generated_durations: list[float],
    uploaded_visual_durations: list[float],
) -> list[float]:
    """Mirror path interleaving so every output clip keeps its own duration."""
    if not uploaded_visual_durations:
        return generated_durations
    if not generated_durations:
        return uploaded_visual_durations

    merged = []
    for index, generated_duration in enumerate(generated_durations):
        if index < len(uploaded_visual_durations):
            merged.append(uploaded_visual_durations[index])
        merged.append(generated_duration)
    merged.extend(uploaded_visual_durations[len(generated_durations) :])
    return merged


def _cancel_if_requested(task_id: str) -> bool:
    """Stop at a pipeline boundary without interrupting active encoders or requests."""
    task = sm.state.get_task(task_id) or {}
    if not _is_task_cancel_requested(task_id):
        return False
    logger.info(f"task cancellation reached a safe checkpoint: {task_id}")
    sm.state.update_task(
        task_id,
        state=const.TASK_STATE_FAILED,
        progress=int(task.get("progress", 0) or 0),
        status=const.TASK_STATUS_CANCELLED,
        stage="cancelled",
        error="task cancelled by user",
        model_token_usage=_current_model_token_usage(),
    )
    generation_report.finalize(
        task_id,
        "cancelled",
        error="task cancelled by user",
    )
    return True


def _register_cross_post_future(task_id: str, future: Future) -> None:
    """登记当前进程持有的发布 Future，供启动恢复和测试判断真实运行状态。"""
    with _cross_post_registry_lock:
        _cross_post_futures[task_id] = future


def _unregister_cross_post_future(task_id: str, future: Future | None = None) -> None:
    """仅移除匹配的 Future，避免旧回调误删同任务后续注册的新工作。"""
    with _cross_post_registry_lock:
        current = _cross_post_futures.get(task_id)
        if current is None or (future is not None and current is not future):
            return
        _cross_post_futures.pop(task_id, None)


def _is_cross_post_active_in_process(task_id: str) -> bool:
    """判断当前进程是否仍持有未结束的发布任务。"""
    with _cross_post_registry_lock:
        future = _cross_post_futures.get(task_id)
        return future is not None and not future.done()


def _is_windows_process_alive(process_id: int) -> bool:
    """通过只读 Win32 API 判断进程状态，避免用 os.kill 误终止进程。"""
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # ctypes 默认把未声明的返回值当作 32 位 int。Windows 64 位进程句柄可能
    # 因此被截断，必须显式声明 Win32 函数签名后再调用。
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        if error_code == error_invalid_parameter:
            return False
        if error_code == error_access_denied:
            # 进程存在但当前用户无查询权限时，必须保守地视为存活，避免错误
            # 回收其它账户正在执行的发布任务。
            return True
        logger.warning(
            "failed to open cross-post owner process on Windows, "
            f"process_id: {process_id}, error_code: {error_code}"
        )
        return True

    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            error_code = ctypes.get_last_error()
            logger.warning(
                "failed to read cross-post owner process state on Windows, "
                f"process_id: {process_id}, error_code: {error_code}"
            )
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _is_cross_post_owner_alive(owner: str | None) -> bool:
    """判断持久化发布任务的本机进程是否仍存在。"""
    if not owner:
        return False

    try:
        hostname, process_id_text, _ = owner.split(":", 2)
        process_id = int(process_id_text)
    except (TypeError, ValueError):
        logger.warning(f"invalid cross-post owner metadata: {owner}")
        return False

    # 无法可靠探测其它主机上的进程。共享 Redis 的多主机部署中必须保守地
    # 视为仍在运行，避免当前节点误删另一节点正在读取的视频文件。
    if hostname != socket.gethostname():
        return True

    # 当前进程内是否仍有真实发布工作，已经由 Future 注册表准确判断。运行到
    # 这里说明注册表中没有对应 Future，即使 owner 与当前进程完全一致，也应
    # 视为已中断；这可以覆盖终态写入持续失败、Future 已结束的场景。
    if process_id == os.getpid():
        return False

    # Windows 的 os.kill(pid, 0) 与 POSIX 语义不同，可能直接终止目标进程。
    # 使用只申请查询权限的 Win32 API，不向目标进程发送任何信号。
    if os.name == "nt":
        return _is_windows_process_alive(process_id)

    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        logger.warning(
            f"failed to inspect cross-post owner process, owner: {owner}, error: {exc}"
        )
        return True
    return True


def _mark_task_failed(task_id: str, stage: str, error: str) -> dict:
    """记录结构化失败信息，并保留任务失败前已经到达的进度。"""
    existing_task = None
    try:
        existing_task = sm.state.get_task(task_id)
    except Exception as exc:
        logger.warning(f"failed to read task state before failure update: {exc}")

    # 具体服务函数通常比编排层拥有更准确的错误原因。后续的空结果检查
    # 不能再用通用文案覆盖它，否则 API 调用方仍然只能看到模糊信息。
    if (
        existing_task
        and existing_task.get("state") == const.TASK_STATE_FAILED
        and existing_task.get("error")
    ):
        return existing_task

    message = str(error or "unknown task error").strip()
    progress = int((existing_task or {}).get("progress", 0) or 0)
    logger.error(f"task failed, task_id: {task_id}, stage: {stage}, error: {message}")
    failure = {
        "task_id": task_id,
        "state": const.TASK_STATE_FAILED,
        "progress": progress,
        "status": const.TASK_STATUS_FAILED,
        "stage": stage,
        "failed_stage": stage,
        "error": message,
        "model_token_usage": _current_model_token_usage(),
    }
    sm.state.update_task(
        task_id,
        state=failure["state"],
        progress=failure["progress"],
        status=failure["status"],
        stage=failure["stage"],
        failed_stage=failure["failed_stage"],
        error=failure["error"],
        model_token_usage=failure["model_token_usage"],
    )
    generation_report.finalize(task_id, "failed", error=message)
    return failure


def generate_script(task_id, params):
    logger.info("\n\n## generating video script")
    video_script = params.video_script.strip()
    if not video_script:
        video_script = llm.generate_script(
            video_subject=params.video_subject,
            language=params.video_language,
            paragraph_number=params.paragraph_number,
            video_script_prompt=params.video_script_prompt,
            custom_system_prompt=params.custom_system_prompt,
            target_duration=params.target_duration,
            ai_director_enabled=params.ai_director_enabled,
        )
    else:
        logger.debug(f"video script: \n{video_script}")

    if not video_script:
        _mark_task_failed(task_id, "script", "failed to generate video script")
        return None

    return video_script


def prepare_director_plan(task_id: str, params: VideoParams, video_script: str) -> dict:
    """Resolve AI creative settings, then apply explicit user overrides."""
    existing_plan = (task_store.get_task_store().get_task(task_id) or {}).get(
        "director_plan"
    )
    if isinstance(existing_plan, dict) and existing_plan:
        plan = dict(existing_plan)
        source = str(plan.get("source") or "recovered")
    elif params.ai_director_enabled:
        plan = llm.generate_director_plan(
            video_subject=params.video_subject,
            video_script=video_script,
            video_request=params.video_script_prompt,
            target_duration=params.target_duration,
        )
        source = "ai"
    else:
        plan = {}
        source = "manual"

    manual_values = {
        "voice_rate": params.voice_rate,
        "video_clip_duration": params.video_clip_duration,
        "video_clip_speed": params.video_clip_speed,
        "video_transition_mode": getattr(
            params.video_transition_mode, "value", params.video_transition_mode
        ),
        "bgm_volume": params.bgm_volume,
        "sonilo_bgm_prompt": params.sonilo_bgm_prompt,
    }
    overrides = set(params.director_overrides or [])
    if not params.ai_director_enabled:
        overrides = set(manual_values)
    for field in overrides.intersection(manual_values):
        plan[field] = manual_values[field]

    params.voice_rate = float(plan.get("voice_rate", params.voice_rate))
    params.video_clip_duration = int(
        plan.get("video_clip_duration", params.video_clip_duration)
    )
    params.video_clip_speed = utils.normalize_clip_speed(
        plan.get("video_clip_speed", params.video_clip_speed)
    )
    transition = plan.get("video_transition_mode", params.video_transition_mode)
    params.video_transition_mode = (
        VideoTransitionMode.none
        if transition is None
        else VideoTransitionMode(transition)
    )
    params.bgm_volume = float(plan.get("bgm_volume", params.bgm_volume))
    if params.bgm_type == "sonilo":
        params.sonilo_bgm_prompt = str(
            plan.get("sonilo_bgm_prompt", params.sonilo_bgm_prompt) or ""
        )

    resolved = {
        **plan,
        "source": source,
        "target_duration": params.target_duration,
        "overrides": sorted(overrides),
    }
    task_store.get_task_store().patch_task(task_id, director_plan=resolved)
    return resolved


def generate_terms(task_id, params, video_script):
    logger.info("\n\n## generating video terms")
    video_terms = params.video_terms
    if not video_terms:
        # 开启素材按文案顺序匹配后，关键词本身也必须按脚本叙事顺序生成；
        # 否则后续即使顺序下载和顺序拼接，也只能复用一组全局主题词，
        # 无法改善“后面内容的画面提前出现”的问题。
        video_terms = llm.generate_terms(
            video_subject=params.video_subject,
            video_script=video_script,
            amount=8 if params.match_materials_to_script else 5,
            match_script_order=params.match_materials_to_script,
        )
    else:
        if isinstance(video_terms, str):
            video_terms = [term.strip() for term in re.split(r"[,，]", video_terms)]
        elif isinstance(video_terms, list):
            video_terms = [term.strip() for term in video_terms]
        else:
            raise ValueError("video_terms must be a string or a list of strings.")

        logger.debug(f"video terms: {utils.to_json(video_terms)}")

    if not video_terms:
        _mark_task_failed(
            task_id,
            "terms",
            "failed to generate video search terms",
        )
        return None

    # 可选的 TwelveLabs Marengo 语义重排：未启用时返回原顺序，无任何副作用。
    # 顺序匹配模式下关键词顺序本身就是脚本叙事顺序，必须保持原样，故跳过。
    if not params.match_materials_to_script:
        video_terms = twelvelabs.rerank_terms_by_subject(
            video_subject=params.video_subject,
            search_terms=video_terms,
        )

    return video_terms


def save_script_data(task_id, video_script, video_terms, params):
    script_file = path.join(utils.task_dir(task_id), "script.json")
    script_data = {
        "script": video_script,
        "search_terms": video_terms,
        "params": params,
    }

    with open(script_file, "w", encoding="utf-8") as f:
        f.write(utils.to_json(script_data))


def resolve_custom_audio_file(task_id: str, custom_audio_file: str | None) -> str:
    requested_file = (custom_audio_file or "").strip()
    if not requested_file:
        return ""

    task_dir = utils.task_dir(task_id)
    try:
        return file_security.resolve_path_within_directory(
            task_dir,
            requested_file,
        )
    except ValueError as exc:
        task_dir_error = exc

    server_audio_file = path.realpath(
        requested_file
        if path.isabs(requested_file)
        else path.join(utils.root_dir(), requested_file)
    )
    if not path.isabs(requested_file):
        project_root = path.realpath(utils.root_dir())
        try:
            if path.commonpath([project_root, server_audio_file]) != project_root:
                raise ValueError(
                    "relative custom audio paths must stay within the project directory"
                )
        except ValueError as exc:
            raise ValueError(
                "custom audio file must be task-local or an existing server-side file"
            ) from exc

    if not path.isfile(server_audio_file):
        raise ValueError(
            "custom audio file does not exist or is not a file"
        ) from task_dir_error

    return server_audio_file


def generate_audio(task_id, params, video_script):
    """
    Generate audio for the video script.
    Audio is always produced by the system TTS path; uploaded/custom narration
    files are no longer accepted for video generation tasks.
    Returns:
        - audio_file: path to the generated audio file
        - audio_duration: duration of the audio in seconds
        - sub_maker: subtitle maker object
    """
    logger.info("\n\n## generating audio")
    if getattr(params, "custom_audio_file", None):
        logger.warning(
            "custom_audio_file was provided but is ignored; using system TTS"
        )
        params.custom_audio_file = None

    audio_file = path.join(utils.task_dir(task_id), "audio.mp3")
    sub_maker = voice.tts(
        text=video_script,
        voice_name=voice.parse_voice_name(params.voice_name),
        voice_rate=params.voice_rate,
        voice_file=audio_file,
    )
    if sub_maker is None:
        _mark_task_failed(
            task_id,
            "audio",
            "failed to synthesize audio; verify the selected voice and TTS connectivity",
        )
        return None, None, None
    audio_duration = 0.0
    if path.isfile(audio_file):
        try:
            audio_duration = float(video.get_audio_duration(audio_file))
        except Exception as exc:
            logger.warning(
                "failed to probe generated audio duration, "
                f"task_id={task_id}, error={exc}"
            )
    if audio_duration <= 0:
        audio_duration = float(voice.get_audio_duration(sub_maker))
    if audio_duration == 0:
        _mark_task_failed(task_id, "audio", "generated audio duration is zero")
        return None, None, None
    return audio_file, audio_duration, sub_maker


def generate_subtitle(task_id, params, video_script, sub_maker, audio_file):
    """
    Generate subtitle for the video script.
    If subtitle generation is disabled or no subtitle maker is provided, it will return an empty string.
    Otherwise, it will generate the subtitle using the specified provider.
    Returns:
        - subtitle_path: path to the generated subtitle file
    """
    logger.info("\n\n## generating subtitle")
    if not params.subtitle_enabled:
        return ""

    subtitle_path = path.join(utils.task_dir(task_id), "subtitle.srt")
    subtitle_provider = config.app.get("subtitle_provider", "edge").strip().lower()
    logger.info(f"\n\n## generating subtitle, provider: {subtitle_provider}")

    if not subtitle_provider:
        logger.info("subtitle provider is empty, skip subtitle generation")
        return ""

    if sub_maker is None and subtitle_provider != "whisper":
        # 自定义音频不会经过 TTS，因此没有 Edge/Azure 等 TTS 返回的
        # sub_maker 时间轴。只有 Whisper 可以直接从音频文件转写字幕；
        # 其他字幕提供方继续保持原有行为，避免生成错误的空时间轴。
        logger.warning(
            "subtitle maker is missing, skip subtitle generation for provider: "
            f"{subtitle_provider}"
        )
        return ""

    if subtitle_provider == "edge":
        voice.create_subtitle(
            text=video_script, sub_maker=sub_maker, subtitle_file=subtitle_path
        )
        if not os.path.exists(subtitle_path):
            # Edge 字幕偶尔会因为时间轴与文案无法匹配而没有产出文件。这里不能
            # 自动切换到 Whisper，否则首次失败会在用户不知情的情况下下载数 GB
            # 的模型。只有显式配置 Whisper 时才允许加载模型，Edge 失败则保留
            # 无字幕视频并记录原因，避免意外的网络和磁盘开销。
            logger.warning(
                "edge subtitle generation did not produce a subtitle file; "
                "skip subtitles without falling back to whisper"
            )
            return ""

    if subtitle_provider == "whisper":
        subtitle.create(audio_file=audio_file, subtitle_file=subtitle_path)
        logger.info("\n\n## correcting subtitle")
        subtitle.correct(subtitle_file=subtitle_path, video_script=video_script)

    subtitle_lines = subtitle.file_to_subtitles(subtitle_path)
    if not subtitle_lines:
        logger.warning(f"subtitle file is invalid: {subtitle_path}")
        return ""

    return subtitle_path


def _material_preparation_error(prepared: dict | None, fallback: str) -> str:
    if not isinstance(prepared, dict):
        return fallback

    errors = prepared.get("errors") or prepared.get("seedance_errors") or []
    if isinstance(errors, list) and errors:
        messages = []
        for item in errors[:3]:
            if isinstance(item, dict):
                message = str(item.get("error") or item.get("message") or "").strip()
                scene_index = item.get("scene_index")
                if message:
                    prefix = f"scene {scene_index}: " if scene_index is not None else ""
                    messages.append(f"{prefix}{message}")
            else:
                message = str(item).strip()
                if message:
                    messages.append(message)
        if messages:
            if len(errors) > len(messages):
                messages.append(f"... and {len(errors) - len(messages)} more")
            return "; ".join(messages)

    missing = prepared.get("missing") or []
    if isinstance(missing, list) and missing:
        return f"missing video materials for {len(missing)} scene(s)"

    return fallback


def get_video_materials(
    task_id,
    params,
    video_terms,
    audio_duration,
    video_script="",
    sub_maker=None,
):
    if params.material_strategy == "ai_generated":
        _force_manual_scene_materials(params)
    insert_uploaded_visuals = _should_insert_uploaded_visuals(params, video_script)
    uploaded_visual_paths = []
    if insert_uploaded_visuals:
        uploaded_visual_paths = _prepare_uploaded_visual_reference_paths(params)
    elif _uploaded_visual_reference_materials(params):
        logger.info(
            "uploaded visual references are available but not requested by "
            "the subject or script"
        )

    current_task = task_store.get_task_store().get_task(task_id) or {}
    director_plan = current_task.get("director_plan") or {}
    narration_units = current_task.get(
        "narration_units"
    ) or material_pipeline.build_narration_units(
        video_script or params.video_script or params.video_subject,
        audio_duration,
        director_plan.get("video_clip_duration") or params.video_clip_duration,
        sub_maker=sub_maker,
    )
    if params.material_strategy == "ai_generated":
        prepared = material_pipeline.prepare_ai_generated_materials(
            task_id=task_id,
            params=params,
            video_script=video_script or params.video_script or params.video_subject,
            audio_duration=audio_duration,
            narration_units=narration_units,
            director_plan=director_plan,
        )
    else:
        terms = (
            [str(item).strip() for item in video_terms if str(item).strip()]
            if isinstance(video_terms, (list, tuple))
            else [
                item.strip()
                for item in str(video_terms or "").split(",")
                if item.strip()
            ]
        )
        prepared = material_pipeline.prepare_ordered_materials(
            task_id=task_id,
            params=params,
            video_script=video_script or params.video_script or params.video_subject,
            video_terms=terms,
            audio_duration=audio_duration,
        )
    if prepared.get("status") == const.TASK_STATUS_FAILED:
        _mark_task_failed(
            task_id,
            "materials",
            _material_preparation_error(
                prepared, "failed to generate AI video materials"
            ),
        )
        return None
    generated_paths = prepared["paths"] or []
    material_paths = _merge_uploaded_visuals_with_generated(
        generated_paths,
        uploaded_visual_paths,
    )
    if uploaded_visual_paths and material_paths:
        latest_task = task_store.get_task_store().get_task(task_id) or {}
        generated_durations = _resolve_material_durations(
            generated_paths,
            latest_task.get("selected_material_durations"),
            params.video_clip_duration,
        )
        uploaded_visual_durations = _resolve_material_durations(
            uploaded_visual_paths,
            None,
            params.video_clip_duration,
        )
        merged_durations = _merge_uploaded_visual_durations(
            generated_durations,
            uploaded_visual_durations,
        )
        task_store.get_task_store().patch_task(
            task_id,
            selected_materials=material_paths,
            selected_material_durations=merged_durations,
        )
    return material_paths or None


def generate_final_videos(
    task_id, params, downloaded_videos, audio_file, subtitle_path, audio_duration
):
    final_video_paths = []
    combined_video_paths = []
    warnings = []
    sonilo_bgm_requested = params.bgm_type == "sonilo" and bgm_service.should_use_bgm(
        params.bgm_type, params.bgm_volume
    )
    # AI 分镜素材已经按脚本顺序生成，最终合成必须保持同一时间线。
    if params.match_materials_to_script or params.material_strategy == "ai_generated":
        video_concat_mode = VideoConcatMode.sequential
    elif params.video_count == 1:
        video_concat_mode = params.video_concat_mode
    else:
        video_concat_mode = VideoConcatMode.random
    video_transition_mode = params.video_transition_mode
    current_task = sm.state.get_task(task_id) or {}
    selected_durations = current_task.get("selected_material_durations") or None

    _progress = 50
    for i in range(params.video_count):
        if _is_task_cancel_requested(task_id):
            raise _TaskCancellationCheckpoint()
        index = i + 1
        combined_video_path = path.join(
            utils.task_dir(task_id), f"combined-{index}.mp4"
        )
        logger.info(f"\n\n## combining video: {index} => {combined_video_path}")
        video.combine_videos(
            combined_video_path=combined_video_path,
            video_paths=downloaded_videos,
            audio_file=audio_file,
            video_aspect=params.video_aspect,
            video_concat_mode=video_concat_mode,
            video_transition_mode=video_transition_mode,
            max_clip_duration=params.video_clip_duration,
            threads=params.n_threads,
            clip_speed=params.video_clip_speed,
            clip_durations=selected_durations,
        )

        if _is_task_cancel_requested(task_id):
            raise _TaskCancellationCheckpoint()

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_path = path.join(utils.task_dir(task_id), f"final-{index}.mp4")

        # Sonilo 模式下先明确禁用默认 BGM 解析，避免恢复旧任务时残留的
        # bgm_file 被误当成当前配乐。只有音量大于 0 才生成代理并调用付费 API；
        # 0 音量表示完整禁用背景音乐，不产生生成、下载或混音开销。
        bgm_file_override = "" if params.bgm_type == "sonilo" else None
        if sonilo_bgm_requested:
            sonilo_bgm_path = path.join(
                utils.task_dir(task_id), f"sonilo-bgm-{index}.m4a"
            )
            try:
                with generation_report.stage(
                    task_id,
                    "sonilo_bgm",
                    f"生成作品 {index} 的配乐",
                    details={
                        "video_index": index,
                        "duration_seconds": audio_duration,
                        "paid_operation": True,
                    },
                ):
                    sonilo.generate_bgm(
                        video_path=combined_video_path,
                        output_path=sonilo_bgm_path,
                        video_duration=audio_duration,
                        prompt=params.sonilo_bgm_prompt,
                    )
                bgm_file_override = sonilo_bgm_path
                generation_report.record_paid_operation(
                    task_id,
                    {
                        "type": "sonilo_bgm",
                        "name": f"作品 {index} 的 AI 配乐",
                        "scene_index": index,
                        "status": "completed",
                        "auto_approved": True,
                        "duration_seconds": audio_duration,
                    },
                )
            except sonilo.SoniloError as exc:
                # 视频、旁白和字幕都已生成时，第三方配乐临时失败不应浪费整条
                # 任务。当前视频明确禁用 BGM，并把降级结果返回 WebUI 提醒用户。
                logger.warning(
                    f"Sonilo BGM generation failed: task_id={task_id}, "
                    f"video_index={index}, error={exc}"
                )
                bgm_file_override = ""
                warnings.append({"code": "sonilo_bgm_failed", "video_index": index})
                generation_report.record_paid_operation(
                    task_id,
                    {
                        "type": "sonilo_bgm",
                        "name": f"作品 {index} 的 AI 配乐",
                        "scene_index": index,
                        "status": "failed",
                        "auto_approved": True,
                        "duration_seconds": audio_duration,
                        "error": str(exc)[:500],
                    },
                )

        if _is_task_cancel_requested(task_id):
            raise _TaskCancellationCheckpoint()

        logger.info(f"\n\n## generating video: {index} => {final_video_path}")
        bgm_mix_succeeded = video.generate_video(
            video_path=combined_video_path,
            audio_path=audio_file,
            subtitle_path=subtitle_path,
            output_file=final_video_path,
            params=params,
            bgm_file_override=bgm_file_override,
        )
        if _is_task_cancel_requested(task_id):
            raise _TaskCancellationCheckpoint()
        if params.bgm_type == "sonilo" and bgm_file_override and not bgm_mix_succeeded:
            # Sonilo 已成功返回并通过 FFmpeg 校验，但 MoviePy 最终混音仍可能
            # 因运行环境失败。视频服务会保留无 BGM 作品，任务层复用同一结构化
            # 警告通知 WebUI；API 生成失败时 override 为空，不会重复追加警告。
            warnings.append({"code": "sonilo_bgm_failed", "video_index": index})

        _progress += 50 / params.video_count / 2
        sm.state.update_task(task_id, progress=_progress)

        final_video_paths.append(final_video_path)
        combined_video_paths.append(combined_video_path)

    return final_video_paths, combined_video_paths, warnings


def _patch_cross_post_state(task_id: str, **kwargs) -> bool | None:
    """安全更新发布字段；短暂状态后端故障时有限重试。"""
    for attempt in range(1, _CROSS_POST_STATE_WRITE_ATTEMPTS + 1):
        try:
            return sm.state.patch_task(task_id, **kwargs)
        except Exception as exc:
            # Redis 短暂断连不应让任务永久停留在 pending/processing。发布状态
            # 写入频率很低，这里使用固定次数和短等待即可覆盖瞬时故障，同时
            # 避免后台线程无限阻塞。最后一次失败保留完整堆栈便于定位。
            if attempt >= _CROSS_POST_STATE_WRITE_ATTEMPTS:
                logger.exception(
                    f"failed to update cross-post state after retries, "
                    f"task_id: {task_id}, fields: {', '.join(kwargs)}, "
                    f"attempts: {attempt}, error: {exc}"
                )
                return None

            logger.warning(
                f"retry cross-post state update, task_id: {task_id}, "
                f"fields: {', '.join(kwargs)}, attempt: {attempt}, error: {exc}"
            )
            time.sleep(_CROSS_POST_STATE_RETRY_DELAY_SECONDS)

    return None


def _record_cross_post_failure(
    task_id: str,
    error: Exception,
    results: list[dict] | None = None,
) -> None:
    """尽最大努力保存发布失败；状态后端不可用时由日志保留诊断信息。"""
    updated = _patch_cross_post_state(
        task_id,
        cross_post_state=const.CROSS_POST_STATE_FAILED,
        cross_post_results=results or None,
        cross_post_error=str(error),
        cross_post_owner=None,
    )
    if updated is False:
        logger.warning(f"discard cross-post failure for missing task: {task_id}")


def _ensure_cross_post_terminal_state(task_id: str) -> None:
    """Future 结束后把仍处于活动态的任务收敛为失败。"""
    try:
        task = sm.state.get_task(task_id)
    except Exception as exc:
        # 此处已经是 Future 的最终回调，没有后续同步调用方可以处理异常。
        # 状态后端恢复后，下一次进程启动仍会通过恢复逻辑处理遗留状态。
        logger.exception(
            f"failed to verify final cross-post state, task_id: {task_id}, error: {exc}"
        )
        return

    if not task or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES:
        return

    logger.warning(
        f"cross-post worker ended without terminal state, task_id: {task_id}, "
        f"state: {task.get('cross_post_state')}"
    )
    _record_cross_post_failure(
        task_id,
        RuntimeError("cross-post worker ended without persisting a terminal state"),
        task.get("cross_post_results"),
    )


def recover_interrupted_cross_posts(page_size: int = 100) -> int | None:
    """
    将进程重启后无法恢复的发布任务标记为失败。

    跨平台发布使用当前进程内的线程池，不是持久化任务队列。进程启动时，
    Redis 中残留的 pending/processing 不会自动继续执行；如果继续把它们视为
    运行中，用户将永久无法删除任务。这里分页扫描状态，只处理当前进程没有
    对应 Future 的活动记录，并保留已经生成的视频结果。
    """
    recovered = 0
    page = 1

    while True:
        try:
            tasks, total = sm.state.get_all_tasks(page, page_size)
        except Exception as exc:
            logger.exception(f"failed to recover interrupted cross-post tasks: {exc}")
            return None

        for task in tasks:
            task_id = str(task.get("task_id") or "")
            if (
                not task_id
                or task.get("cross_post_state") not in _ACTIVE_CROSS_POST_STATES
                or _is_cross_post_active_in_process(task_id)
                or _is_cross_post_owner_alive(task.get("cross_post_owner"))
            ):
                continue

            updated = _patch_cross_post_state(
                task_id,
                cross_post_state=const.CROSS_POST_STATE_FAILED,
                cross_post_error=_INTERRUPTED_CROSS_POST_ERROR,
                cross_post_owner=None,
            )
            if updated is True:
                recovered += 1

        if page * page_size >= total or not tasks:
            break
        page += 1

    if recovered:
        logger.warning(f"recovered interrupted cross-post tasks: {recovered}")
    return recovered


def _run_cross_post(
    task_id: str,
    video_paths: tuple[str, ...],
    video_subject: str,
    video_script: str,
    video_language: str,
    platforms: tuple[str, ...],
    youtube_privacy_status: str,
) -> None:
    """后台执行跨平台发布，并只补充发布相关的任务字段。"""
    results = []
    try:
        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_PROCESSING,
            cross_post_error=None,
            cross_post_owner=_cross_post_process_owner,
        )
        if state_updated is not True:
            # False 表示任务已删除，None 表示状态后端暂时不可用。两种情况都
            # 不应继续调用第三方接口，否则用户无法查询或控制这次发布。
            if state_updated is False:
                logger.warning(f"skip cross-post for missing task: {task_id}")
            else:
                _record_cross_post_failure(
                    task_id,
                    RuntimeError("failed to persist cross-post processing state"),
                )
            return

        logger.info(
            f"cross-post started, task_id: {task_id}, platforms: {', '.join(platforms)}"
        )
        youtube_extra = None
        if any(platform.startswith("youtube") for platform in platforms):
            task_snapshot = sm.state.get_task(task_id) or {}
            with llm.token_usage_context(
                task_snapshot.get("model_token_usage")
            ) as token_usage:
                metadata = llm.generate_social_metadata(
                    video_subject=video_subject,
                    video_script=video_script,
                    language=video_language or "",
                    platform="youtube_shorts",
                )
                _patch_cross_post_state(
                    task_id,
                    model_token_usage=llm.get_collected_token_usage(token_usage),
                )
            youtube_extra = {
                "youtube_title": metadata.get("title", video_subject),
                "youtube_description": metadata.get("caption", ""),
                "tags": metadata.get("hashtags", []),
                "privacyStatus": youtube_privacy_status,
                "containsSyntheticMedia": True,
            }

        for video_path in video_paths:
            result = upload_post.cross_post_video(
                video_path=video_path,
                title=video_subject or "Check out this video! #shorts #viral",
                platforms=list(platforms),
                youtube_extra=youtube_extra,
            )
            if not isinstance(result, dict):
                result = {
                    "success": False,
                    "error": "Upload-Post returned an invalid response",
                }
            results.append(result)

        failures = [result for result in results if not result.get("success")]
        if failures:
            error_messages = [
                str(
                    result.get("error")
                    or result.get("message")
                    or "unknown upload error"
                )
                for result in failures
            ]
            cross_post_state = const.CROSS_POST_STATE_FAILED
            cross_post_error = "; ".join(error_messages)
            logger.warning(
                f"cross-post completed with failures, task_id: {task_id}, "
                f"failed: {len(failures)}, total: {len(results)}"
            )
        else:
            cross_post_state = const.CROSS_POST_STATE_COMPLETE
            cross_post_error = None
            logger.success(
                f"cross-post completed, task_id: {task_id}, videos: {len(results)}"
            )

        state_updated = _patch_cross_post_state(
            task_id,
            cross_post_state=cross_post_state,
            cross_post_results=results,
            cross_post_error=cross_post_error,
            cross_post_owner=None,
        )
        if state_updated is False:
            logger.warning(f"discard cross-post result for missing task: {task_id}")
        elif state_updated is None:
            # 上传已经结束但结果没有持久化时，不能继续保留 processing。
            # 失败状态写入会再次经过有限重试，至少让调用方得到明确终态。
            _record_cross_post_failure(
                task_id,
                RuntimeError("failed to persist final cross-post result"),
                results,
            )
    except Exception as exc:
        # 发布失败只影响发布状态，不能反向覆盖已经完成的视频任务。
        # 异常原文写入任务状态，API 调用方无需访问服务端日志也能定位问题。
        logger.exception(f"cross-post failed, task_id: {task_id}, error: {exc}")
        _record_cross_post_failure(task_id, exc, results)


def _run_cross_post_with_slot(*args) -> None:
    """执行发布任务，并确保成功、失败或异常时都会归还队列容量。"""
    try:
        _run_cross_post(*args)
    except Exception as exc:
        # _run_cross_post 已处理预期异常；这里是最后一道保护，避免未来新增
        # 逻辑抛出的异常只保存在无人读取的 Future 中。
        task_id = str(args[0]) if args else "unknown"
        logger.exception(f"cross-post worker crashed, task_id: {task_id}, error: {exc}")
        if args:
            _record_cross_post_failure(task_id, exc)
    finally:
        _cross_post_slots.release()


def _finalize_cross_post_future(task_id: str, future: Future) -> None:
    """清理 Future 注册，并确保取消、异常和状态写入失败都能收敛。"""
    _unregister_cross_post_future(task_id, future)

    try:
        error = future.exception()
    except CancelledError:
        logger.warning(f"cross-post future was cancelled, task_id: {task_id}")
        # Future 在开始执行前被取消时，worker 的 finally 不会运行，因此需要
        # 在回调中归还队列容量，并把持久化状态改为失败。
        _cross_post_slots.release()
        _record_cross_post_failure(
            task_id,
            RuntimeError("cross-post job was cancelled before execution"),
        )
        return
    except Exception as exc:
        logger.exception(
            f"failed to inspect cross-post future, task_id: {task_id}, error: {exc}"
        )
        _ensure_cross_post_terminal_state(task_id)
        return

    if error is not None:
        logger.error(
            f"cross-post future failed, task_id: {task_id}, "
            f"error: {type(error).__name__}: {error}"
        )

    _ensure_cross_post_terminal_state(task_id)


def _schedule_cross_post(
    task_id: str,
    video_paths: list[str],
    params: VideoParams,
    video_script: str,
    platforms: list[str],
    youtube_privacy_status: str,
) -> str | None:
    """提交后台发布任务；成功返回 None，调度失败返回可查询的错误原因。"""
    if not _cross_post_slots.acquire(blocking=False):
        error = "cross-post queue is full; publishing was skipped"
        logger.warning(
            f"skip cross-post because queue is full, task_id: {task_id}, "
            f"capacity: {_cross_post_max_pending_tasks}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=error,
            cross_post_owner=None,
        )
        return error

    try:
        future = _cross_post_executor.submit(
            _run_cross_post_with_slot,
            task_id,
            tuple(video_paths),
            params.video_subject or "",
            video_script,
            params.video_language or "",
            tuple(platforms),
            youtube_privacy_status,
        )
        _register_cross_post_future(task_id, future)
        future.add_done_callback(partial(_finalize_cross_post_future, task_id))
    except RuntimeError as exc:
        _unregister_cross_post_future(task_id)
        _cross_post_slots.release()
        logger.exception(
            f"failed to schedule cross-post, task_id: {task_id}, error: {exc}"
        )
        _patch_cross_post_state(
            task_id,
            cross_post_state=const.CROSS_POST_STATE_FAILED,
            cross_post_error=f"failed to schedule cross-post: {exc}",
            cross_post_owner=None,
        )
        return f"failed to schedule cross-post: {exc}"

    return None


def _run_pipeline(task_id, params: VideoParams, stop_at: str = "video"):
    from app.agent.runner import run_agent

    return run_agent(task_id, params, stop_at=stop_at)



def start(task_id, params: VideoParams, stop_at: str = "video"):
    """执行任务流水线，并确保未预期异常也会转换成可查询的失败状态。"""
    apply_material_defaults(params, stop_at=stop_at)
    generation_report.initialize(
        task_id,
        details={
            "subject": params.video_subject or "",
            "stop_at": stop_at,
            "target_duration": params.target_duration,
            "video_count": params.video_count,
            "material_workflow": (
                "auto_fill_seedance"
                if params.material_strategy == "ai_generated"
                else "library_online_then_auto_fill"
            ),
            "video_generation_api_used": False,
        },
    )
    initial_task = task_store.get_task_store().get_task(task_id) or {}
    preflight_usage = initial_task.get("preflight_model_token_usage")
    if preflight_usage:
        generation_report.initialize(
            task_id,
            details={
                "preflight_completed": True,
                "preflight_analysis": initial_task.get("preflight_analysis") or {},
            },
        )
    with llm.token_usage_context(preflight_usage):
        try:
            result = _run_pipeline(task_id, params, stop_at=stop_at)
        except Exception as exc:
            logger.exception(
                f"unexpected task pipeline failure, task_id: {task_id}, error: {exc}"
            )
            result = _mark_task_failed(
                task_id,
                "pipeline",
                f"{type(exc).__name__}: {exc}",
            )

        # 完整视频任务如果中途进入待处理状态，`_run_pipeline()` 可能还没走到
        # 统一的终态写入。这里补一次汇总，确保查询任务时仍能看到已经消耗的
        # 文案模型 token。
        if stop_at == "video" and isinstance(result, dict):
            result.setdefault("model_token_usage", _current_model_token_usage())
            try:
                sm.state.patch_task(
                    task_id,
                    model_token_usage=result["model_token_usage"],
                )
            except Exception as exc:
                logger.warning(
                    f"failed to persist model token usage, task_id: {task_id}, "
                    f"error: {exc}"
                )
        return result


if __name__ == "__main__":
    task_id = "task_id"
    params = VideoParams(
        video_subject="金钱的作用",
        voice_name="zh-CN-XiaoyiNeural-Female",
        voice_rate=1.0,
    )
    start(task_id, params, stop_at="video")
