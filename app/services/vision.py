from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

from loguru import logger
from openai import OpenAI

from app.config import config
from app.utils import utils


TAGGING_PROMPT = """
Analyze these frames as one reusable short-video material clip. Return JSON only:
{
  "description_zh": "one factual Chinese sentence describing visible content",
  "tags_zh": ["5 to 12 concise Chinese visual tags"],
  "tags_en": ["matching English stock-search tags"]
}
Describe only visible people, objects, place, action, weather, shot type and mood.
Do not infer identities, brands, private attributes or events that are not visible.
""".strip()

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL_NAME = "doubao-1-5-vision-pro-32k-250115"
SMOKE_TEST_IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKUlEQVR4nGM8ceIEA27AhEduBEsD"
    "Rg0YNWDUgFEDRg0YNWDUNBAAwL0CxRoQPvIAAAAASUVORK5CYII="
)
VISION_MODEL_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "id": "doubao-1-5-vision-pro-32k-250115",
        "label": "Doubao 1.5 Vision Pro 32K",
    },
    {
        "id": "doubao-1-5-thinking-vision-pro-250428",
        "label": "Doubao 1.5 Thinking Vision Pro",
    },
    {
        "id": "doubao-1.5-vision-pro-250328",
        "label": "Doubao 1.5 Vision Pro",
    },
    {
        "id": "doubao-1.5-vision-lite-250315",
        "label": "Doubao 1.5 Vision Lite",
    },
)


def configured_model_options() -> tuple[dict[str, str], ...]:
    options = list(VISION_MODEL_OPTIONS)
    known_ids = {option["id"] for option in options}
    for model_name in config.vision.get("model_options") or []:
        model_name = str(model_name).strip()
        if model_name and model_name not in known_ids:
            options.append({"id": model_name, "label": model_name})
            known_ids.add(model_name)
    return tuple(options)


def _api_key() -> str:
    return str(
        config.vision.get("api_key")
        or config.app.get("volcengine_api_key")
        or os.getenv("VISION_API_KEY")
        or os.getenv("ARK_API_KEY")
        or os.getenv("VOLCENGINE_API_KEY")
        or ""
    ).strip()


def _base_url() -> str:
    return str(config.vision.get("base_url") or DEFAULT_BASE_URL).strip()


def _model_name() -> str:
    return str(config.vision.get("model_name") or DEFAULT_MODEL_NAME).strip()


def is_enabled() -> bool:
    return bool(_api_key() and _model_name())


def _image_data_url(path: str) -> str:
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _extract_json(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, flags=re.DOTALL)
        if not match:
            raise ValueError("vision model did not return JSON")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("vision model returned a non-object response")
    return parsed


def _normalize_tags(values: Any, *, limit: int = 12) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[,，、;；\n]", values)
    if not isinstance(values, list):
        return []
    result = []
    seen = set()
    for value in values:
        tag = re.sub(r"\s+", " ", str(value or "").strip()).strip("#")
        key = tag.casefold()
        if not tag or len(tag) > 60 or key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= limit:
            break
    return result


def analyze_images(
    image_paths: Iterable[str],
    *,
    prompt: str = TAGGING_PROMPT,
) -> dict[str, Any]:
    paths = [str(path) for path in image_paths if os.path.isfile(path)]
    if not paths:
        raise ValueError("no readable images were provided")
    if not is_enabled():
        raise RuntimeError("vision model is not configured")
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(
        {
            "type": "image_url",
            "image_url": {"url": _image_data_url(path), "detail": "low"},
        }
        for path in paths
    )
    client = OpenAI(
        api_key=_api_key(),
        base_url=_base_url() or None,
        timeout=float(config.vision.get("timeout", 120)),
    )
    response = client.chat.completions.create(
        model=_model_name(),
        messages=[{"role": "user", "content": content}],
        temperature=0.1,
    )
    text = response.choices[0].message.content if response.choices else ""
    parsed = _extract_json(text or "")
    return {
        "description_zh": str(parsed.get("description_zh") or "").strip()[:500],
        "tags_zh": _normalize_tags(parsed.get("tags_zh")),
        "tags_en": _normalize_tags(parsed.get("tags_en")),
    }


def extract_video_frames(
    video_path: str,
    *,
    start: float = 0,
    duration: float | None = None,
    max_frames: int | None = None,
) -> list[str]:
    max_frames = max(1, min(4, int(max_frames or config.app.get("material_vision_max_frames", 4))))
    duration = max(0.1, float(duration or 1))
    positions = [start + duration * (index + 1) / (max_frames + 1) for index in range(max_frames)]
    frame_dir = tempfile.mkdtemp(prefix="mpt-vision-", dir=utils.storage_dir("temp", create=True))
    frames = []
    ffmpeg = utils.get_ffmpeg_binary()
    try:
        for index, position in enumerate(positions, start=1):
            output = os.path.join(frame_dir, f"frame-{index}.jpg")
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{position:.3f}",
                    "-i",
                    video_path,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(960,iw)':-2",
                    "-y",
                    output,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0 and os.path.isfile(output):
                frames.append(output)
        return frames
    except Exception:
        for frame in frames:
            Path(frame).unlink(missing_ok=True)
        Path(frame_dir).rmdir()
        raise


def analyze_video_clip(video_path: str, duration: float) -> dict[str, Any]:
    frames = extract_video_frames(video_path, duration=duration)
    if not frames:
        raise RuntimeError("failed to extract frames for visual analysis")
    frame_dir = Path(frames[0]).parent
    try:
        return analyze_images(frames)
    finally:
        for frame in frames:
            Path(frame).unlink(missing_ok=True)
        try:
            frame_dir.rmdir()
        except OSError:
            logger.debug(f"vision frame directory is not empty: {frame_dir}")


def review_candidate(image_paths: Iterable[str], scene_text: str) -> dict[str, Any]:
    prompt = f"""
Decide whether the visible content is suitable B-roll for this narration:
{scene_text[:1000]}

Return JSON only: {{"relevant": true, "reason_zh": "short reason"}}.
Reject generic or unrelated footage. Do not require literal word-for-word identity.
""".strip()
    parsed = _extract_json(
        _raw_analyze_images(image_paths, prompt=prompt)
    )
    return {
        "relevant": bool(parsed.get("relevant")),
        "reason_zh": str(parsed.get("reason_zh") or "").strip()[:300],
    }


def _raw_analyze_images(image_paths: Iterable[str], *, prompt: str) -> str:
    paths = [str(path) for path in image_paths if os.path.isfile(path)]
    if not paths or not is_enabled():
        raise RuntimeError("vision model is not configured or frames are unavailable")
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(
        {
            "type": "image_url",
            "image_url": {"url": _image_data_url(path), "detail": "low"},
        }
        for path in paths
    )
    client = OpenAI(
        api_key=_api_key(),
        base_url=_base_url() or None,
        timeout=float(config.vision.get("timeout", 120)),
    )
    response = client.chat.completions.create(
        model=_model_name(),
        messages=[{"role": "user", "content": content}],
        temperature=0,
    )
    return response.choices[0].message.content if response.choices else ""


def _extract_model_ids(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        data = payload.get("data")
    else:
        data = getattr(payload, "data", None)
    if not isinstance(data, list):
        return set()
    ids = set()
    for item in data:
        if isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
        else:
            model_id = str(getattr(item, "id", "") or "").strip()
        if model_id:
            ids.add(model_id)
    return ids


def test_connection() -> tuple[bool, str]:
    if not is_enabled():
        return False, "请先填写视觉模型 API Key 和模型名称"
    try:
        client = OpenAI(
            api_key=_api_key(),
            base_url=_base_url() or None,
            timeout=float(config.vision.get("timeout", 120)),
        )
        models = client.models.list()
        model_ids = _extract_model_ids(models)
        model_name = _model_name()
        if model_ids and model_name not in model_ids:
            return False, f"当前账号模型列表中没有 {model_name}"
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": 'Return JSON only: {"ok": true}',
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": SMOKE_TEST_IMAGE_DATA_URL,
                                "detail": "low",
                            },
                        },
                    ],
                }
            ],
            temperature=0,
            max_tokens=20,
        )
        text = response.choices[0].message.content if response.choices else ""
        if not str(text or "").strip():
            return False, "视觉模型测试返回为空，请检查模型是否支持图片理解"
        return True, ""
    except Exception as exc:
        return False, str(exc)
