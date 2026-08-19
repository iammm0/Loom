"""Application implementation - ASGI."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import config
from app.models.exception import HttpException
from app.router import root_api_router
from app.utils import utils


@asynccontextmanager
async def application_lifespan(_: FastAPI):
    """集中处理 API 进程启动恢复和关闭日志。"""
    logger.info("startup event")

    # 跨平台发布由当前进程线程池执行，不会在服务重启后恢复。启动时把 Redis
    # 中确认已失去执行进程的活动状态收敛为失败，避免任务永久无法删除。
    from app.services import task as task_service
    from app.services import task_store

    task_service.recover_interrupted_cross_posts()
    task_store.ensure_task_workers_started()
    try:
        yield
    finally:
        task_store.stop_task_workers()
        logger.info("shutdown event")


def exception_handler(request: Request, e: HttpException):
    return JSONResponse(
        status_code=e.status_code,
        content=utils.get_response(e.status_code, e.data, e.message),
    )


def validation_exception_handler(request: Request, e: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content=utils.get_response(
            status=400, data=e.errors(), message="field required"
        ),
    )


def get_application() -> FastAPI:
    """Initialize FastAPI application.

    Returns:
       FastAPI: Application object instance.

    """
    instance = FastAPI(
        title=config.project_name,
        description=config.project_description,
        version=config.project_version,
        debug=False,
        lifespan=application_lifespan,
    )
    instance.include_router(root_api_router)
    instance.add_exception_handler(HttpException, exception_handler)
    instance.add_exception_handler(RequestValidationError, validation_exception_handler)
    return instance


app = get_application()

# Configures the CORS middleware for the FastAPI app
cors_allowed_origins_str = os.getenv("CORS_ALLOWED_ORIGINS", "")
origins = cors_allowed_origins_str.split(",") if cors_allowed_origins_str else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

task_dir = utils.task_dir()
app.mount(
    "/tasks", StaticFiles(directory=task_dir, html=True, follow_symlink=True), name=""
)

_webui_dist = Path(utils.root_dir()) / "webui" / "dist"
_public_dir = Path(utils.public_dir())
_spa_root = _webui_dist if (_webui_dist / "index.html").is_file() else _public_dir
_spa_assets = _spa_root / "assets"
if _spa_assets.is_dir():
    app.mount("/assets", StaticFiles(directory=_spa_assets), name="webui-assets")


@app.get("/{full_path:path}")
async def serve_webui(full_path: str):
    reserved = {"api", "tasks", "docs", "redoc", "openapi.json"}
    head = full_path.split("/", 1)[0]
    if head in reserved:
        return JSONResponse(
            status_code=404,
            content=utils.get_response(404, message="not found"),
        )
    candidate = _spa_root / full_path
    if full_path and candidate.is_file():
        return FileResponse(candidate)
    index = _spa_root / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        status_code=404,
        content=utils.get_response(404, message="webui is not built"),
    )
