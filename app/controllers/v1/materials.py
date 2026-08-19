import mimetypes
import os

from fastapi import Form, Path, Query, Request, UploadFile
from fastapi.params import File
from fastapi.responses import FileResponse, StreamingResponse

from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import (
    MaterialSegmentUpdateRequest,
    MaterialTagMaterialsUpdateRequest,
    MaterialTagUpdateRequest,
    MaterialUpdateRequest,
)
from app.services import material_library
from app.utils import file_security, utils


router = new_router()


def _library():
    return material_library.get_material_library()


def _require_material(material_id: str, request_id: str):
    material = _library().get(material_id)
    if material:
        return material
    raise HttpException(
        task_id=request_id,
        status_code=404,
        message=f"{request_id}: material not found",
    )


def _require_segment(segment_id: str, request_id: str):
    with _library().store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM material_segments WHERE segment_id = ?", (segment_id,)
        ).fetchone()
    if row:
        return _library()._segment_dict(row)
    raise HttpException(
        task_id=request_id,
        status_code=404,
        message=f"{request_id}: material segment not found",
    )


@router.post("/materials", summary="Upload files into the local material library")
def upload_materials(
    request: Request,
    files: list[UploadFile] = File(...),
    manual_tags: str = Form(default=""),
    ai_tagging: bool = Form(default=False),
):
    request_id = base.get_task_id(request)
    if not files or len(files) > 50:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: upload between 1 and 50 files",
        )
    results = []
    for upload in files:
        try:
            upload.file.seek(0)
            results.append(
                _library().save_upload(
                    upload.filename,
                    upload.file,
                    manual_tags=manual_tags,
                    ai_tagging=ai_tagging,
                )
            )
        except material_library.MaterialLibraryError as exc:
            raise HttpException(
                task_id=request_id,
                status_code=400,
                message=f"{request_id}: {str(exc)}",
            ) from exc
        except Exception as exc:
            raise HttpException(
                task_id=request_id,
                status_code=500,
                message=f"{request_id}: material processing failed: {str(exc)}",
            ) from exc
    return utils.get_response(200, {"materials": results})


@router.get("/materials", summary="List local library materials")
def list_materials(
    request: Request,
    query: str = Query(default="", max_length=200),
    status: str = Query(default="", max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
):
    base.get_task_id(request)
    materials, total = _library().list(
        query=query, status=status, page=page, page_size=page_size
    )
    return utils.get_response(
        200,
        {"materials": materials, "total": total, "page": page, "page_size": page_size},
    )


@router.get("/materials/tags", summary="List material tags")
def list_material_tags(
    request: Request,
    query: str = Query(default="", max_length=60),
):
    base.get_task_id(request)
    return utils.get_response(200, {"tags": _library().list_tags(query=query)})


@router.patch("/materials/tags/{tag_name}", summary="Rename a material tag")
def rename_material_tag(
    request: Request,
    body: MaterialTagUpdateRequest,
    tag_name: str = Path(..., max_length=60),
):
    request_id = base.get_task_id(request)
    try:
        updated = _library().rename_tag(tag_name, body.name)
    except material_library.MaterialLibraryError as exc:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        ) from exc
    return utils.get_response(200, {"name": body.name, "updated_materials": updated})


@router.delete("/materials/tags/{tag_name}", summary="Delete a material tag")
def delete_material_tag(request: Request, tag_name: str = Path(..., max_length=60)):
    base.get_task_id(request)
    updated = _library().delete_tag(tag_name)
    return utils.get_response(200, {"name": tag_name, "updated_materials": updated})


@router.put(
    "/materials/tags/{tag_name}/materials",
    summary="Set all materials related to one tag",
)
def set_material_tag_relations(
    request: Request,
    body: MaterialTagMaterialsUpdateRequest,
    tag_name: str = Path(..., max_length=60),
):
    request_id = base.get_task_id(request)
    try:
        material_count = _library().set_tag_materials(tag_name, body.material_ids)
    except material_library.MaterialLibraryError as exc:
        raise HttpException(
            task_id=request_id,
            status_code=400,
            message=f"{request_id}: {str(exc)}",
        ) from exc
    return utils.get_response(
        200,
        {"name": tag_name, "material_count": material_count},
    )


@router.get("/materials/{material_id}", summary="Get one local material")
def get_material(request: Request, material_id: str = Path(...)):
    request_id = base.get_task_id(request)
    return utils.get_response(200, _require_material(material_id, request_id))


@router.patch("/materials/{material_id}", summary="Edit one material and its tags")
def update_material(
    request: Request,
    body: MaterialUpdateRequest,
    material_id: str = Path(...),
):
    request_id = base.get_task_id(request)
    _require_material(material_id, request_id)
    return utils.get_response(
        200,
        _library().update_material(
            material_id,
            tags=body.tags,
            description_zh=body.description_zh,
        ),
    )


@router.patch("/materials/segments/{segment_id}", summary="Edit material tags")
def update_material_segment(
    request: Request,
    body: MaterialSegmentUpdateRequest,
    segment_id: str = Path(...),
):
    request_id = base.get_task_id(request)
    _require_segment(segment_id, request_id)
    _library().update_segment(
        segment_id,
        manual_tags=body.manual_tags,
        description_zh=body.description_zh,
    )
    return utils.get_response(200, _require_segment(segment_id, request_id))


@router.post("/materials/{material_id}/confirm", summary="Confirm material tags")
def confirm_material(request: Request, material_id: str = Path(...)):
    request_id = base.get_task_id(request)
    _require_material(material_id, request_id)
    return utils.get_response(200, _library().confirm(material_id))


@router.post("/materials/{material_id}/analyze", summary="Run visual tagging again")
def analyze_material(request: Request, material_id: str = Path(...)):
    request_id = base.get_task_id(request)
    _require_material(material_id, request_id)
    try:
        result = _library().retag(material_id)
    except Exception as exc:
        raise HttpException(
            task_id=request_id,
            status_code=502,
            message=f"{request_id}: visual tagging failed: {str(exc)}",
        ) from exc
    return utils.get_response(200, result)


@router.delete("/materials/{material_id}", summary="Delete a local material")
def delete_material(request: Request, material_id: str = Path(...)):
    request_id = base.get_task_id(request)
    _require_material(material_id, request_id)
    _library().delete(material_id)
    return utils.get_response(200, {"material_id": material_id})


def _safe_segment_file(segment: dict, field: str, request_id: str) -> str:
    path = str(segment.get(field) or "")
    allowed_roots = (
        utils.storage_dir("materials"),
        utils.storage_dir("local_videos"),
    )
    for root in allowed_roots:
        try:
            return file_security.resolve_path_within_directory(root, path)
        except ValueError:
            continue
    raise HttpException(
        task_id=request_id,
        status_code=404,
        message=f"{request_id}: material file not found",
    )


@router.get(
    "/materials/segments/{segment_id}/thumbnail", summary="Get segment thumbnail"
)
def material_thumbnail(request: Request, segment_id: str = Path(...)):
    request_id = base.get_task_id(request)
    segment = _require_segment(segment_id, request_id)
    path = _safe_segment_file(segment, "thumbnail_path", request_id)
    return FileResponse(path, media_type="image/jpeg")


@router.get(
    "/materials/segments/{segment_id}/file", summary="Stream a material segment"
)
def material_file(request: Request, segment_id: str = Path(...)):
    request_id = base.get_task_id(request)
    segment = _require_segment(segment_id, request_id)
    path = _safe_segment_file(segment, "file_path", request_id)
    size = os.path.getsize(path)
    range_header = request.headers.get("Range")
    if not range_header:
        return FileResponse(path, media_type=mimetypes.guess_type(path)[0])
    from app.controllers.v1.video import _parse_byte_range

    start, end = _parse_byte_range(range_header, size, request_id)
    length = end - start + 1

    def iterator():
        with open(path, "rb") as source:
            source.seek(start)
            remaining = length
            while remaining:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    response = StreamingResponse(iterator(), media_type=mimetypes.guess_type(path)[0])
    response.status_code = 206
    response.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    return response
