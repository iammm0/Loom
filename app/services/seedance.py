from __future__ import annotations

import math
import os
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import requests
from loguru import logger

from app.config import config
from app.utils import utils

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL_ID = "doubao-seedance-2-0-260128"
SEEDANCE_MODEL_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "id": "doubao-seedance-2-0-260128",
        "label": "Seedance 2.0",
    },
)
_DEFAULT_DURATION_LIMITS = (2, None)
_MODEL_DURATION_LIMITS: tuple[tuple[str, int, int | None], ...] = (
    ("seedance-2-0", 4, 15),
    ("seedance-1-5", 4, 12),
    ("seedance-1-0", 2, 12),
)
OFFICIAL_PRICING_URL = "https://www.volcengine.com/docs/82379/1544106"
OFFICIAL_PRICING_RETRIEVED_AT = "2026-07-22"


class SeedanceError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


def configured_model_options() -> tuple[dict[str, str], ...]:
    """Return only models proven to create tasks in the production account.

    Ark's read-only ``/models`` response may include models that still return
    404 from the video-generation endpoint. Configuration candidates therefore
    must not be promoted into the user-facing selector automatically.
    """
    return SEEDANCE_MODEL_OPTIONS


def _model_id() -> str:
    return str(config.seedance.get("model_id") or DEFAULT_MODEL_ID).strip()


def _api_key() -> str:
    return str(
        config.seedance.get("api_key")
        or config.app.get("volcengine_api_key")
        or os.getenv("SEEDANCE_API_KEY")
        or os.getenv("ARK_API_KEY")
        or os.getenv("VOLCENGINE_API_KEY")
        or ""
    ).strip()


def is_enabled() -> bool:
    return bool(_api_key() and _model_id())


def _base_url() -> str:
    return str(config.seedance.get("base_url") or DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    key = _api_key()
    if not key:
        raise SeedanceError("Seedance API Key 未配置")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _duration_limits(model_id: str) -> tuple[int, int | None]:
    normalized = model_id.lower().replace("_", "-")
    for marker, minimum, maximum in _MODEL_DURATION_LIMITS:
        if marker in normalized:
            return minimum, maximum
    return _DEFAULT_DURATION_LIMITS


def _generation_duration(model_id: str, duration: float) -> int:
    minimum, maximum = _duration_limits(model_id)
    requested = float(duration)
    seconds = int(round(requested)) if math.isfinite(requested) else minimum
    seconds = max(minimum, seconds)
    if maximum is not None:
        seconds = min(maximum, seconds)
    return seconds


def create_generation_task(
    *,
    prompt: str,
    aspect_ratio: str,
    duration: float,
) -> str:
    model_id = _model_id()
    if not model_id:
        raise SeedanceError("Seedance 模型或推理接入点未配置")
    payload = {
        "model": model_id,
        "content": [{"type": "text", "text": prompt.strip()}],
        "ratio": aspect_ratio,
        "duration": _generation_duration(model_id, duration),
        "resolution": str(config.seedance.get("resolution") or "720p"),
        "watermark": False,
    }
    # Creation is deliberately attempted once. Retrying an ambiguous POST can
    # create a second billable generation when the first response was lost.
    response = requests.post(
        f"{_base_url()}/contents/generations/tasks",
        headers=_headers(),
        json=payload,
        timeout=(30, 120),
    )
    if response.status_code >= 400:
        raise SeedanceError(
            f"Seedance 创建任务失败: HTTP {response.status_code}: "
            f"{_response_error(response)}"
        )
    body = response.json()
    task_id = str(body.get("id") or body.get("task_id") or "").strip()
    if not task_id:
        raise SeedanceError("Seedance 创建任务响应缺少任务 ID")
    return task_id


def get_generation_task(provider_task_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{_base_url()}/contents/generations/tasks/{provider_task_id}",
        headers=_headers(),
        timeout=(30, 60),
    )
    if response.status_code >= 400:
        raise SeedanceError(
            f"Seedance 查询任务失败: HTTP {response.status_code}: "
            f"{_response_error(response)}"
        )
    return response.json()


def _result_url(payload: dict[str, Any]) -> str:
    candidates = [
        payload.get("video_url"),
        (payload.get("content") or {}).get("video_url")
        if isinstance(payload.get("content"), dict)
        else None,
        (payload.get("output") or {}).get("video_url")
        if isinstance(payload.get("output"), dict)
        else None,
    ]
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                candidates.extend((item.get("video_url"), item.get("url")))
    return next((str(value) for value in candidates if value), "")


def wait_for_generation_details(
    provider_task_id: str,
) -> tuple[str, dict[str, Any], int]:
    timeout = max(60, int(config.seedance.get("timeout", 900)))
    interval = max(2, int(config.seedance.get("poll_interval", 5)))
    deadline = time.monotonic() + timeout
    poll_count = 0
    while time.monotonic() < deadline:
        payload = get_generation_task(provider_task_id)
        poll_count += 1
        status = str(payload.get("status") or payload.get("state") or "").lower()
        if status in {"succeeded", "success", "completed", "done"}:
            url = _result_url(payload)
            if not url:
                raise SeedanceError("Seedance 任务完成但没有返回视频地址")
            return url, payload, poll_count
        if status in {"failed", "error", "cancelled", "canceled"}:
            message = str(payload.get("error") or payload.get("message") or status)
            raise SeedanceError(
                f"Seedance 生成失败: {message[:500]}",
                details={
                    "provider_status": status,
                    "provider_payload": payload,
                    "poll_count": poll_count,
                },
            )
        time.sleep(interval)
    raise SeedanceError("Seedance 生成超时，任务不会自动重新提交")


def wait_for_generation(provider_task_id: str) -> str:
    url, _, _ = wait_for_generation_details(provider_task_id)
    return url


def download_result(url: str, output_path: str) -> str:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    temp_path = f"{output_path}.part"
    try:
        with requests.get(url, stream=True, timeout=(60, 300)) as response:
            response.raise_for_status()
            with open(temp_path, "wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        output.write(chunk)
        if not os.path.isfile(temp_path) or os.path.getsize(temp_path) <= 0:
            raise SeedanceError("Seedance 视频下载结果为空")
        os.replace(temp_path, output_path)
        return output_path
    except Exception as exc:
        Path(temp_path).unlink(missing_ok=True)
        if isinstance(exc, SeedanceError):
            raise
        raise SeedanceError(f"Seedance 视频下载失败: {str(exc)}") from exc


def generate_clip(
    *,
    prompt: str,
    aspect_ratio: str,
    duration: float,
    output_path: str,
    provider_task_id: str = "",
) -> tuple[str, str]:
    result, task_id, _ = generate_clip_detailed(
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        duration=duration,
        output_path=output_path,
        provider_task_id=provider_task_id,
    )
    return result, task_id


def _unit_price_cny_per_million_tokens(
    model_id: str,
    resolution: str,
) -> float | None:
    normalized = model_id.lower().replace("_", "-")
    normalized_resolution = resolution.lower()
    if "seedance-2-0-fast" in normalized:
        return 37.0
    if "seedance-2-0-mini" in normalized:
        return 23.0
    if "seedance-2-0" in normalized:
        if normalized_resolution == "1080p":
            return 51.0
        if normalized_resolution == "4k":
            return 26.0
        return 46.0
    if "seedance-1-5-pro" in normalized:
        # The application requests text-to-video and removes any incidental
        # provider audio. This corresponds to the official silent-video rate.
        return 8.0
    if "seedance-1-0-pro-fast" in normalized:
        return 4.2
    if "seedance-1-0-pro" in normalized:
        return 15.0
    return None


def _media_metadata(path: str) -> dict[str, Any]:
    ffmpeg = Path(utils.get_ffmpeg_binary())
    ffprobe = shutil.which("ffprobe") or str(ffmpeg.with_name("ffprobe"))
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate:format=duration",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return {}
    try:
        body = json.loads(result.stdout)
        stream = (body.get("streams") or [{}])[0]
        numerator, denominator = str(stream.get("avg_frame_rate") or "0/1").split(
            "/", 1
        )
        frame_rate = float(numerator) / max(float(denominator), 1.0)
        return {
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "frame_rate": round(frame_rate, 3),
            "duration_seconds": round(
                float((body.get("format") or {}).get("duration") or 0), 3
            ),
        }
    except (IndexError, KeyError, TypeError, ValueError):
        return {}


def billing_details(
    *,
    model_id: str,
    resolution: str,
    provider_duration: float,
    provider_payload: dict[str, Any] | None,
    media_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = provider_payload or {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    completion_tokens = usage.get("completion_tokens")
    calculation_method = "api_usage"
    tokens: int | None
    try:
        tokens = int(completion_tokens) if completion_tokens is not None else None
    except (TypeError, ValueError):
        tokens = None

    metadata = media_metadata or {}
    if tokens is None:
        width = int(metadata.get("width") or 0)
        height = int(metadata.get("height") or 0)
        frame_rate = float(metadata.get("frame_rate") or 0)
        if width and height and frame_rate:
            tokens = int(round(provider_duration * width * height * frame_rate / 1024))
            calculation_method = "official_formula_estimate"
        else:
            calculation_method = "unavailable"

    unit_price = _unit_price_cny_per_million_tokens(model_id, resolution)
    cost = (
        round(tokens / 1_000_000 * unit_price, 4)
        if tokens is not None and unit_price is not None
        else None
    )
    return {
        "tokens": tokens,
        "api_completion_tokens": tokens if calculation_method == "api_usage" else None,
        "unit_price_cny_per_million_tokens": unit_price,
        "estimated_cost_cny": cost,
        "calculation_method": calculation_method,
        "is_estimate": calculation_method != "api_usage",
        "pricing_source_url": OFFICIAL_PRICING_URL,
        "pricing_retrieved_at": OFFICIAL_PRICING_RETRIEVED_AT,
        "media": metadata or None,
    }


def generate_clip_detailed(
    *,
    prompt: str,
    aspect_ratio: str,
    duration: float,
    output_path: str,
    provider_task_id: str = "",
    task_created_callback: Callable[[str], None] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    model_id = _model_id()
    resolution = str(config.seedance.get("resolution") or "720p")
    provider_duration = _generation_duration(model_id, duration)
    task_id = str(provider_task_id or "")
    details: dict[str, Any] = {
        "provider_task_id": task_id or None,
        "model": model_id,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "requested_timeline_duration_seconds": round(float(duration), 3),
        "provider_requested_duration_seconds": provider_duration,
        "reused_provider_task": bool(task_id),
        "status": "processing",
        "provider_status": None,
        "billable": None,
        "timings": {},
    }
    final_payload: dict[str, Any] = {}
    downloaded = f"{output_path}.download.mp4"
    try:
        if not task_id:
            started = time.monotonic()
            task_id = create_generation_task(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                duration=duration,
            )
            details["timings"]["create_task_seconds"] = round(
                time.monotonic() - started, 3
            )
            details["provider_task_id"] = task_id
            if task_created_callback:
                task_created_callback(task_id)
        else:
            details["timings"]["create_task_seconds"] = 0.0

        started = time.monotonic()
        url, final_payload, poll_count = wait_for_generation_details(task_id)
        details["timings"]["poll_wait_seconds"] = round(
            time.monotonic() - started, 3
        )
        details["poll_count"] = poll_count
        details["provider_status"] = str(
            final_payload.get("status") or final_payload.get("state") or "succeeded"
        )

        started = time.monotonic()
        download_result(url, downloaded)
        details["timings"]["download_seconds"] = round(
            time.monotonic() - started, 3
        )
        metadata = _media_metadata(downloaded)

        started = time.monotonic()
        result = subprocess_strip_audio(downloaded, output_path, duration)
        details["timings"]["preprocess_seconds"] = round(
            time.monotonic() - started, 3
        )
        details.update(
            billing_details(
                model_id=model_id,
                resolution=resolution,
                provider_duration=provider_duration,
                provider_payload=final_payload,
                media_metadata=metadata,
            )
        )
        details["status"] = "completed"
        details["billable"] = True
        return result, task_id, details
    except Exception as exc:
        if isinstance(exc, SeedanceError):
            error_payload = exc.details.get("provider_payload")
            if isinstance(error_payload, dict):
                final_payload = error_payload
            if exc.details.get("poll_count") is not None:
                details["poll_count"] = exc.details["poll_count"]
        provider_status = str(
            final_payload.get("status") or final_payload.get("state") or ""
        ).lower()
        details["status"] = "failed"
        details["provider_status"] = provider_status or details.get("provider_status")
        details["error"] = f"{type(exc).__name__}: {exc}"[:1000]
        if provider_status in {"succeeded", "success", "completed", "done"}:
            details["billable"] = True
            details.update(
                billing_details(
                    model_id=model_id,
                    resolution=resolution,
                    provider_duration=provider_duration,
                    provider_payload=final_payload,
                    media_metadata=_media_metadata(downloaded)
                    if os.path.isfile(downloaded)
                    else {},
                )
            )
        elif provider_status in {"failed", "error", "cancelled", "canceled"}:
            details["billable"] = False
        raise SeedanceError(str(exc), details=details) from exc
    finally:
        Path(downloaded).unlink(missing_ok=True)


def subprocess_strip_audio(source: str, output: str, duration: float) -> str:
    import subprocess

    result = subprocess.run(
        [
            utils.get_ffmpeg_binary(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source,
            "-t",
            f"{max(0.1, duration):.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-movflags",
            "+faststart",
            "-y",
            output,
        ],
        capture_output=True,
        text=True,
        timeout=max(120, int(duration * 20)),
    )
    if result.returncode != 0 or not os.path.isfile(output):
        raise SeedanceError(result.stderr.strip() or "Seedance 视频预处理失败")
    return output


def _response_error(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:500]

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)[:500]
        if error:
            return str(error)[:500]
        for key in ("message", "msg", "code"):
            if body.get(key):
                return str(body[key])[:500]
    return str(body)[:500]


def _extract_model_ids(payload: dict[str, Any]) -> set[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        return set()
    ids = set()
    for item in data:
        if isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
            if model_id:
                ids.add(model_id)
    return ids


def test_connection() -> tuple[bool, str]:
    if not is_enabled():
        return False, "请先填写 Seedance API Key 和模型接入点"
    try:
        response = requests.get(
            f"{_base_url()}/models",
            headers=_headers(),
            timeout=(15, 30),
        )
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}: {_response_error(response)}"
        model_ids = _extract_model_ids(response.json())
        model_id = _model_id()
        if model_ids and model_id not in model_ids:
            return False, f"当前账号模型列表中没有 {model_id}"
        return True, ""
    except Exception as exc:
        logger.warning(f"Seedance connection test failed: {exc}")
        return False, str(exc)
