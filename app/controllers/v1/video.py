import glob
import os
import pathlib
import shutil
import zipfile
from typing import Union
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, Path, Query, Request, UploadFile
from fastapi.params import File
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger

from app.config import config
from app.controllers import base
from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.v1.base import new_router
from app.models import const
from app.models.exception import HttpException
from app.models.schema import (
    AudioRequest,
    BatchTaskCreateRequest,
    BgmRetrieveResponse,
    BgmUploadResponse,
    SupplementalSceneApprovalRequest,
    SubtitleRequest,
    TaskDeletionResponse,
    TaskListResponse,
    TaskIdsRequest,
    TaskReplacementRequest,
    TaskQueryRequest,
    TaskQueryResponse,
    TaskResponse,
    TaskBulkActionRequest,
    TaskVideoRequest,
    VideoMaterialUploadResponse,
    VideoMaterialRetrieveResponse,
)
from app.services import bgm as bgm_service
from app.services import state as sm
from app.services import task as tm
from app.services import task_store
from app.services import video
from app.services.task_stream import build_task_stream

from app.utils import file_security, utils

# 认证依赖项
# router = new_router(dependencies=[Depends(base.verify_token)])
router = new_router()


class _PersistentTaskManager:
    """Keep the existing controller seam while persisting every queued job."""

    def add_task(self, func, *args, **kwargs):
        del func, args
        return task_store.get_task_store().enqueue(
            kwargs["params"],
            task_id=kwargs.get("task_id"),
            stop_at=kwargs.get("stop_at", "video"),
        )


task_manager = _PersistentTaskManager()

_DIRECTOR_OVERRIDE_FIELDS = {
    "paragraph_number",
    "voice_rate",
    "video_clip_duration",
    "video_clip_speed",
    "video_transition_mode",
    "bgm_volume",
    "sonilo_bgm_prompt",
}


def _record_explicit_director_overrides(params):
    """Preserve creative fields explicitly supplied by existing API clients."""
    if not params.ai_director_enabled or params.director_overrides:
        return params
    params.director_overrides = sorted(
        _DIRECTOR_OVERRIDE_FIELDS.intersection(params.model_fields_set)
    )
    return params


def _sanitize_upload_filename(filename: str, request_id: str) -> str:
    # 浏览器或客户端有时会附带目录信息，甚至可能夹带 ../ 这类穿越片段。
    # 这里只保留纯文件名，避免上传接口把文件写到目标目录之外。
    normalized_name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if not normalized_name or normalized_name in {".", ".."}:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: invalid filename",
        )
    return normalized_name


def _resolve_path_within_directory(
    base_dir: str, unsafe_path: str, request_id: str
) -> str:
    try:
        return file_security.resolve_path_within_directory(base_dir, unsafe_path)
    except ValueError as exc:
        logger.warning(
            f"reject unsafe file path, request_id: {request_id}, path: {unsafe_path}, "
            f"error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=404 if str(exc) == "file does not exist" else 403,
            message=f"{request_id}: invalid file path",
        )


def _public_task_data(task: dict, *, include_stream: bool = False) -> dict:
    """复制任务状态并移除仅用于服务端进程协调的内部字段。"""
    public_task = dict(task)
    public_task.pop("cross_post_owner", None)
    public_task.pop("resolved_scene_paths", None)
    uploaded_paths = public_task.pop("uploaded_scene_paths", {}) or {}
    if uploaded_paths:
        public_task["uploaded_scene_indexes"] = sorted(
            int(key) for key in uploaded_paths if str(key).isdigit()
        )
    if include_stream:
        public_task["stream"] = build_task_stream(public_task)
    return public_task


def _task_file_to_uri(file: str, endpoint: str, task_dir: str, request_id: str) -> str:
    if not isinstance(file, str):
        return file

    if file.startswith(("http://", "https://")):
        return file

    try:
        resolved_path = file_security.resolve_path_within_directory(task_dir, file)
    except ValueError as exc:
        # 任务状态理论上只应保存任务目录内的产物路径。这里不再继续拼接 URL，
        # 避免把异常路径包装成可访问链接；同时保留原值，便于排查历史脏数据。
        logger.warning(
            f"skip unsafe task output path, request_id: {request_id}, path: {file}, "
            f"error: {str(exc)}"
        )
        return file

    relative_path = os.path.relpath(resolved_path, task_dir).replace("\\", "/")
    uri_path = f"tasks/{relative_path}"
    if endpoint:
        return f"{endpoint.rstrip('/')}/{uri_path}"
    return f"/{uri_path}"


def _parse_byte_range(
    range_header: str | None, file_size: int, request_id: str
) -> tuple[int, int]:
    """解析单段 HTTP Range，并把无效或越界请求稳定转换成 416。"""
    if file_size <= 0:
        raise HttpException(
            task_id=request_id,
            status_code=416,
            message=f"{request_id}: requested range is not satisfiable",
        )

    if not range_header:
        return 0, file_size - 1

    try:
        # 视频播放器这里只需要单段 bytes range。拒绝多段请求可以避免返回体
        # 与 Content-Range 不一致，也避免异常字符串落入 int() 产生 500。
        if not range_header.startswith("bytes=") or "," in range_header:
            raise ValueError("unsupported range format")
        start_text, end_text = range_header[6:].split("-", 1)
        if not start_text and not end_text:
            raise ValueError("empty range")

        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError("invalid suffix length")
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
            if start < 0 or start >= file_size or end < start:
                raise ValueError("range outside file")
            end = min(end, file_size - 1)
    except (TypeError, ValueError) as exc:
        logger.warning(
            f"reject invalid video range, request_id: {request_id}, "
            f"range: {range_header}, file_size: {file_size}, error: {str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=416,
            message=f"{request_id}: requested range is not satisfiable",
        ) from exc

    return start, end


@router.post("/videos", response_model=TaskResponse, summary="Generate a short video")
def create_video(
    background_tasks: BackgroundTasks, request: Request, body: TaskVideoRequest
):
    return create_task(request, body, stop_at="video")


@router.post("/subtitle", response_model=TaskResponse, summary="Generate subtitle only")
def create_subtitle(
    background_tasks: BackgroundTasks, request: Request, body: SubtitleRequest
):
    return create_task(request, body, stop_at="subtitle")


@router.post("/audio", response_model=TaskResponse, summary="Generate audio only")
def create_audio(
    background_tasks: BackgroundTasks, request: Request, body: AudioRequest
):
    return create_task(request, body, stop_at="audio")


def create_task(
    request: Request,
    body: Union[TaskVideoRequest, SubtitleRequest, AudioRequest],
    stop_at: str,
):
    _record_explicit_director_overrides(body)
    task_id = utils.get_uuid()
    request_id = base.get_task_id(request)
    try:
        task = {
            "task_id": task_id,
            "request_id": request_id,
            "params": body.model_dump(),
        }
        sm.state.update_task(task_id)
        task_manager.add_task(tm.start, task_id=task_id, params=body, stop_at=stop_at)
        logger.success(f"Task created: {utils.to_json(task)}")
        return utils.get_response(200, task)
    except TaskQueueFullError as e:
        sm.state.delete_task(task_id)
        logger.warning(
            f"reject task because queue is full, request_id: {request_id}, task_id: {task_id}"
        )
        raise HttpException(
            task_id=task_id, status_code=429, message=f"{request_id}: {str(e)}"
        )
    except ValueError as e:
        raise HttpException(
            task_id=task_id, status_code=400, message=f"{request_id}: {str(e)}"
        )


@router.post("/tasks/batch", summary="Create a batch of video tasks")
def create_task_batch(request: Request, body: BatchTaskCreateRequest):
    request_id = base.get_task_id(request)
    try:
        _record_explicit_director_overrides(body.params)
        batch_id, tasks = task_store.get_task_store().enqueue_batch(
            body.subjects,
            body.params,
            batch_name=body.batch_name,
            request_id=request_id,
        )
    except TaskQueueFullError as exc:
        raise HttpException(
            task_id=request_id,
            status_code=429,
            message=f"{request_id}: {str(exc)}",
        ) from exc
    except ValueError as exc:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        ) from exc
    task_store.ensure_task_workers_started()
    return utils.get_response(
        200,
        {
            "batch_id": batch_id,
            "tasks": [
                {"task_id": task["task_id"], "status": task["status"]} for task in tasks
            ],
        },
    )


@router.post("/tasks/actions", summary="Apply an action to multiple tasks")
def apply_task_action(request: Request, body: TaskBulkActionRequest):
    store = task_store.get_task_store()
    results = []
    for task_id in dict.fromkeys(body.task_ids):
        try:
            task = store.get_task(task_id)
            if not task:
                results.append({"task_id": task_id, "ok": False, "error": "not found"})
                continue
            if body.action == "cancel":
                status = store.request_cancel(task_id)
                results.append({"task_id": task_id, "ok": True, "status": status})
            elif body.action == "retry":
                retried = store.retry_task(task_id)
                results.append(
                    {"task_id": task_id, "ok": True, "status": retried["status"]}
                )
            else:
                if tm.is_task_busy(task):
                    results.append(
                        {"task_id": task_id, "ok": False, "error": "task is busy"}
                    )
                    continue
                shutil.rmtree(utils.task_dir(task_id), ignore_errors=True)
                store.delete_task(task_id)
                results.append({"task_id": task_id, "ok": True})
        except task_store.TaskStoreError as exc:
            results.append({"task_id": task_id, "ok": False, "error": str(exc)})
    return utils.get_response(200, {"action": body.action, "results": results})


@router.post("/tasks/download", summary="Download final videos from selected tasks")
def download_task_results(request: Request, body: TaskIdsRequest):
    request_id = base.get_task_id(request)
    store = task_store.get_task_store()
    archive_dir = utils.storage_dir("temp", create=True)
    archive_path = os.path.join(archive_dir, f"tasks-{utils.get_uuid()}.zip")
    added = 0
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for task_id in dict.fromkeys(body.task_ids):
            task = store.get_task(task_id) or {}
            if task.get("status") != const.TASK_STATUS_COMPLETED:
                continue
            for index, video_path in enumerate(task.get("videos") or [], start=1):
                try:
                    safe_path = file_security.resolve_path_within_directory(
                        utils.task_dir(task_id), video_path
                    )
                except ValueError:
                    continue
                archive.write(
                    safe_path,
                    arcname=f"{task_id}/final-{index}{pathlib.Path(safe_path).suffix}",
                )
                added += 1
    if not added:
        os.remove(archive_path)
        raise HttpException(
            task_id=request_id,
            status_code=404,
            message=f"{request_id}: no completed videos were selected",
        )
    return FileResponse(
        archive_path,
        filename="video-tasks.zip",
        media_type="application/zip",
    )


@router.post("/tasks/{task_id}/cancel", summary="Cancel a queued or running task")
def cancel_task(request: Request, task_id: str = Path(...)):
    request_id = base.get_task_id(request)
    status = task_store.get_task_store().request_cancel(task_id)
    if status is None:
        raise HttpException(
            task_id=task_id,
            status_code=404,
            message=f"{request_id}: task not found",
        )
    return utils.get_response(200, {"task_id": task_id, "status": status})


@router.post("/tasks/{task_id}/retry", summary="Retry a failed task")
def retry_task(request: Request, task_id: str = Path(...)):
    request_id = base.get_task_id(request)
    try:
        task = task_store.get_task_store().retry_task(task_id)
    except task_store.TaskStoreError as exc:
        raise HttpException(
            task_id=task_id,
            status_code=409,
            message=f"{request_id}: {str(exc)}",
        ) from exc
    if task is None:
        raise HttpException(
            task_id=task_id,
            status_code=404,
            message=f"{request_id}: task not found",
        )
    task_store.ensure_task_workers_started()
    return utils.get_response(200, task)


@router.post(
    "/tasks/{task_id}/scene-materials/{scene_index}",
    summary="Upload a user-generated video clip for one scene",
)
def upload_task_scene_material(
    request: Request,
    file: UploadFile = File(...),
    task_id: str = Path(...),
    scene_index: int = Path(..., ge=0),
):
    request_id = base.get_task_id(request)
    safe_name = _sanitize_upload_filename(file.filename, request_id)
    extension = pathlib.Path(safe_name).suffix.lower()
    allowed_extensions = {f".{suffix}" for suffix in const.FILE_TYPE_VIDEOS}
    if extension not in allowed_extensions:
        raise HttpException(
            task_id=task_id,
            status_code=400,
            message=f"{request_id}: unsupported scene material file type",
        )

    task = task_store.get_task_store().get_task(task_id)
    if task is None:
        raise HttpException(
            task_id=task_id,
            status_code=404,
            message=f"{request_id}: task not found",
        )
    upload_dir = os.path.join(utils.task_dir(task_id), "material-uploads")
    os.makedirs(upload_dir, exist_ok=True)
    save_path = os.path.join(
        upload_dir, f"scene-{scene_index:04d}-{uuid4().hex}{extension}"
    )
    size = 0
    try:
        file.file.seek(0)
        with open(save_path, "wb") as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > 4 * 1024 * 1024 * 1024:
                    raise ValueError("scene material file exceeds 4 GB")
                output.write(chunk)
        if size <= 0:
            raise ValueError("scene material file is empty")
        if float(video.get_media_duration(save_path) or 0) <= 0:
            raise ValueError("scene material duration is invalid")
        updated, old_path = task_store.get_task_store().attach_scene_material(
            task_id,
            scene_index,
            save_path,
            original_name=safe_name,
        )
        if updated is None:
            raise HttpException(
                task_id=task_id,
                status_code=404,
                message=f"{request_id}: task not found",
            )
        if old_path and old_path != save_path and os.path.isfile(old_path):
            os.remove(old_path)
        return utils.get_response(200, _public_task_data(updated))
    except HttpException:
        if os.path.isfile(save_path):
            os.remove(save_path)
        raise
    except (OSError, ValueError, task_store.TaskStoreError) as exc:
        if os.path.isfile(save_path):
            os.remove(save_path)
        raise HttpException(
            task_id=task_id,
            status_code=409 if isinstance(exc, task_store.TaskStoreError) else 400,
            message=f"{request_id}: {str(exc)}",
        ) from exc


@router.post(
    "/tasks/{task_id}/scene-materials-confirmation",
    summary="Confirm all uploaded scene materials and continue the workflow",
)
def confirm_task_scene_materials(request: Request, task_id: str = Path(...)):
    request_id = base.get_task_id(request)
    try:
        task = task_store.get_task_store().confirm_scene_materials(task_id)
    except task_store.TaskStoreError as exc:
        raise HttpException(
            task_id=task_id,
            status_code=409,
            message=f"{request_id}: {str(exc)}",
        ) from exc
    if task is None:
        raise HttpException(
            task_id=task_id,
            status_code=404,
            message=f"{request_id}: task not found",
        )
    task_store.ensure_task_workers_started()
    return utils.get_response(200, _public_task_data(task))


@router.post(
    "/tasks/{task_id}/supplemental-scenes/approve",
    summary="Approve billable supplemental scene generation",
)
def approve_supplemental_scenes(
    request: Request,
    body: SupplementalSceneApprovalRequest,
    task_id: str = Path(...),
):
    request_id = base.get_task_id(request)
    try:
        task = task_store.get_task_store().approve_supplemental_scenes(
            task_id, body.prompts
        )
    except task_store.TaskStoreError as exc:
        raise HttpException(
            task_id=task_id,
            status_code=409,
            message=f"{request_id}: {str(exc)}",
        ) from exc
    if task is None:
        raise HttpException(
            task_id=task_id,
            status_code=404,
            message=f"{request_id}: task not found",
        )
    task_store.ensure_task_workers_started()
    return utils.get_response(200, task)


@router.post(
    "/tasks/{task_id}/seedance/reject",
    summary="Reject Seedance and wait for replacement materials",
)
def reject_seedance_generation(request: Request, task_id: str = Path(...)):
    request_id = base.get_task_id(request)
    try:
        task = task_store.get_task_store().reject_seedance(task_id)
    except task_store.TaskStoreError as exc:
        raise HttpException(
            task_id=task_id,
            status_code=409,
            message=f"{request_id}: {str(exc)}",
        ) from exc
    if task is None:
        raise HttpException(
            task_id=task_id,
            status_code=404,
            message=f"{request_id}: task not found",
        )
    return utils.get_response(200, task)


@router.post(
    "/tasks/{task_id}/replacement-materials",
    summary="Attach library segments to missing scenes",
)
def replace_task_materials(
    request: Request,
    body: TaskReplacementRequest,
    task_id: str = Path(...),
):
    request_id = base.get_task_id(request)
    try:
        task = task_store.get_task_store().add_replacement_segments(
            task_id, body.replacements
        )
    except task_store.TaskStoreError as exc:
        raise HttpException(
            task_id=task_id,
            status_code=409,
            message=f"{request_id}: {str(exc)}",
        ) from exc
    if task is None:
        raise HttpException(
            task_id=task_id,
            status_code=404,
            message=f"{request_id}: task not found",
        )
    task_store.ensure_task_workers_started()
    return utils.get_response(200, task)


@router.get("/tasks", response_model=TaskListResponse, summary="Get all tasks")
def get_all_tasks(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1),
):
    tasks, total = sm.state.get_all_tasks(page, page_size)

    response = {
        "tasks": [_public_task_data(task) for task in tasks],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    return utils.get_response(200, response)


@router.get(
    "/tasks/{task_id}", response_model=TaskQueryResponse, summary="Query task status"
)
def get_task(
    request: Request,
    task_id: str = Path(..., description="Task ID"),
    query: TaskQueryRequest = Depends(),
):
    request_id = base.get_task_id(request)
    endpoint = config.app.get("endpoint", "").rstrip("/")
    task = sm.state.get_task(task_id)
    if task:
        task_dir = utils.task_dir()
        response_task = _public_task_data(task, include_stream=True)

        if "videos" in task:
            response_task["videos"] = [
                _task_file_to_uri(v, endpoint, task_dir, request_id)
                for v in task["videos"]
            ]
        if "original_videos" in task:
            response_task["original_videos"] = [
                _task_file_to_uri(v, endpoint, task_dir, request_id)
                for v in task["original_videos"]
            ]
        if "combined_videos" in task:
            response_task["combined_videos"] = [
                _task_file_to_uri(v, endpoint, task_dir, request_id)
                for v in task["combined_videos"]
            ]
        return utils.get_response(200, response_task)

    raise HttpException(
        task_id=task_id, status_code=404, message=f"{request_id}: task not found"
    )


@router.delete(
    "/tasks/{task_id}",
    response_model=TaskDeletionResponse,
    summary="Delete a generated short video task",
)
def delete_video(request: Request, task_id: str = Path(..., description="Task ID")):
    request_id = base.get_task_id(request)
    task = sm.state.get_task(task_id)
    if task:
        if tm.is_task_busy(task):
            logger.warning(
                f"refuse to delete busy task, request_id: {request_id}, "
                f"task_id: {task_id}, state: {task.get('state')}, "
                f"cross_post_state: {task.get('cross_post_state')}"
            )
            raise HttpException(
                task_id=task_id,
                status_code=409,
                message=f"{request_id}: task is still running",
            )

        tasks_dir = utils.task_dir()
        current_task_dir = os.path.join(tasks_dir, task_id)
        if os.path.exists(current_task_dir):
            shutil.rmtree(current_task_dir)

        sm.state.delete_task(task_id)
        logger.success(f"video deleted: {utils.to_json(task)}")
        return utils.get_response(200)

    raise HttpException(
        task_id=task_id, status_code=404, message=f"{request_id}: task not found"
    )


@router.get(
    "/musics", response_model=BgmRetrieveResponse, summary="Retrieve local BGM files"
)
def get_bgm_list(request: Request):
    bgm_list = []
    for file in bgm_service.list_bgm_files():
        filename = os.path.basename(file)
        bgm_list.append(
            {
                "name": filename,
                "size": os.path.getsize(file),
                # 只返回文件名，避免把服务器绝对路径暴露给调用方。服务端会
                # 在 storage/bgm 和 resource/songs 两个白名单目录中重新解析。
                "file": filename,
            }
        )
    response = {"files": bgm_list}
    return utils.get_response(200, response)


@router.post(
    "/musics",
    response_model=BgmUploadResponse,
    summary="Upload a background music file",
    description=(
        "Validate an MP3, M4A, AAC, WAV, FLAC, OGG, OPUS, or WMA file up to "
        "30 MB and store it under an immutable UUID filename in storage/bgm."
    ),
    responses={
        400: {"description": "The filename, format, size, or audio stream is invalid"},
        500: {"description": "FFmpeg validation or persistent storage is unavailable"},
    },
)
def upload_bgm_file(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    try:
        safe_filename = bgm_service.save_bgm_upload(file.filename, file.file)
    except bgm_service.BgmUploadError as exc:
        # 上传失败通常可以由用户更换文件后恢复，因此记录 request_id 和明确原因，
        # 但不输出文件内容或绝对路径，避免日志泄露用户数据。
        logger.warning(
            f"background music upload rejected: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        )
    except bgm_service.BgmServiceError as exc:
        # 工具链或存储故障属于服务端问题，不能伪装成用户文件错误。日志保留
        # request_id 和内部原因，HTTP 响应只返回稳定文案，避免暴露服务器路径。
        logger.error(
            f"background music upload failed: request_id={request_id}, error={str(exc)}"
        )
        raise HttpException(
            task_id=request_id,
            status_code=500,
            message=f"{request_id}: background music validation is unavailable",
        )

    response = {"file": safe_filename}
    return utils.get_response(200, response)


@router.get(
    "/video_materials",
    response_model=VideoMaterialRetrieveResponse,
    summary="Retrieve local video materials",
)
def get_video_materials_list(request: Request):
    allowed_suffixes = ("mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png")
    local_videos_dir = utils.storage_dir("local_videos", create=True)
    files = []
    for suffix in allowed_suffixes:
        files.extend(glob.glob(os.path.join(local_videos_dir, f"*.{suffix}")))
    # 文件系统枚举顺序不稳定，直接返回会导致“顺序拼接”在不同机器或不同
    # 时刻表现不一致。这里统一按文件名排序，至少保证服务端返回顺序可预测。
    files.sort(key=lambda file_path: os.path.basename(file_path).lower())
    video_materials_list = []
    for file in files:
        filename = os.path.basename(file)
        video_materials_list.append(
            {
                "name": filename,
                "size": os.path.getsize(file),
                # 与 BGM 一样，只返回文件名；创建任务时再在 local_videos
                # 白名单目录内解析，避免 API 泄露宿主机绝对路径。
                "file": filename,
            }
        )
    response = {"files": video_materials_list}
    return utils.get_response(200, response)


@router.post(
    "/video_materials",
    response_model=VideoMaterialUploadResponse,
    summary="Upload the video material file to the local videos directory",
)
def upload_video_material_file(request: Request, file: UploadFile = File(...)):
    request_id = base.get_task_id(request)
    safe_filename = _sanitize_upload_filename(file.filename, request_id)
    # check file ext
    allowed_suffixes = ("mp4", "mov", "avi", "flv", "mkv", "jpg", "jpeg", "png")
    suffix = pathlib.Path(safe_filename).suffix.lower().lstrip(".")
    # 按完整扩展名校验，既兼容 .MOV 这类大写后缀，也避免 photojpg 这种没有
    # 点号的文件名因为 endswith("jpg") 被误当成合法图片。
    if suffix in allowed_suffixes:
        local_videos_dir = utils.storage_dir("local_videos", create=True)
        save_path = os.path.join(local_videos_dir, safe_filename)
        # save file
        with open(save_path, "wb+") as buffer:
            # If the file already exists, it will be overwritten
            file.file.seek(0)
            buffer.write(file.file.read())
        response = {"file": safe_filename}
        return utils.get_response(200, response)

    raise HttpException(
        "",
        status_code=400,
        message=f"{request_id}: Only files with extensions {', '.join(allowed_suffixes)} can be uploaded",
    )


@router.get("/stream/{file_path:path}")
async def stream_video(request: Request, file_path: str):
    request_id = base.get_task_id(request)
    tasks_dir = utils.task_dir()
    video_path = _resolve_path_within_directory(tasks_dir, file_path, request_id)
    range_header = request.headers.get("Range")
    video_size = os.path.getsize(video_path)
    start, end = _parse_byte_range(range_header, video_size, request_id)
    length = end - start + 1

    def file_iterator(file_path, offset=0, bytes_to_read=None):
        with open(file_path, "rb") as f:
            f.seek(offset, os.SEEK_SET)
            remaining = bytes_to_read or video_size
            while remaining > 0:
                bytes_to_read = min(4096, remaining)
                data = f.read(bytes_to_read)
                if not data:
                    break
                remaining -= len(data)
                yield data

    response = StreamingResponse(
        file_iterator(video_path, start, length), media_type="video/mp4"
    )
    response.headers["Content-Range"] = f"bytes {start}-{end}/{video_size}"
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    response.status_code = 206  # Partial Content

    return response


@router.get("/download/{file_path:path}")
async def download_video(request: Request, file_path: str):
    """
    download video
    :param request: Request request
    :param file_path: video file path, eg: /cd1727ed-3473-42a2-a7da-4faafafec72b/final-1.mp4
    :return: video file
    """
    request_id = base.get_task_id(request)
    tasks_dir = utils.task_dir()
    video_path = _resolve_path_within_directory(tasks_dir, file_path, request_id)
    file_path = pathlib.Path(video_path)
    filename = file_path.stem
    extension = file_path.suffix
    headers = {"Content-Disposition": f"attachment; filename={filename}{extension}"}
    return FileResponse(
        path=video_path,
        headers=headers,
        filename=f"{filename}{extension}",
        media_type=f"video/{extension[1:]}",
    )
