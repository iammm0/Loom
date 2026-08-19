from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from app.utils import utils


class FFmpegError(RuntimeError):
    pass


def ffmpeg_binary() -> str:
    return utils.get_ffmpeg_binary()


def ffprobe_binary() -> str:
    ffmpeg = Path(ffmpeg_binary())
    probed = shutil.which("ffprobe") or str(ffmpeg.with_name("ffprobe"))
    return probed


def run_ffmpeg(args: list[str], *, timeout: int = 300) -> None:
    command = [ffmpeg_binary(), "-y", *args]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip()
        raise FFmpegError(detail[-2000:])


def probe_media(path: str) -> dict:
    result = subprocess.run(
        [
            ffprobe_binary(),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,avg_frame_rate",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise FFmpegError((result.stderr or "ffprobe failed").strip()[-2000:])
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    video_stream = next(
        (item for item in streams if item.get("codec_type") == "video"),
        {},
    )
    audio_stream = next(
        (item for item in streams if item.get("codec_type") == "audio"),
        None,
    )
    numerator, _, denominator = str(video_stream.get("avg_frame_rate") or "0/1").partition(
        "/"
    )
    frame_rate = 0.0
    try:
        frame_rate = float(numerator) / max(float(denominator or 1), 1.0)
    except (TypeError, ValueError):
        frame_rate = 0.0
    return {
        "path": path,
        "duration": round(float((payload.get("format") or {}).get("duration") or 0), 3),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "frame_rate": round(frame_rate, 3),
        "has_audio": audio_stream is not None,
    }
