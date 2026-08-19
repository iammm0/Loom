from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from app.models.schema import VideoAspect, VideoParams
from app.timeline.models import Timeline, TimelineClip, empty_timeline
from app.utils import utils


def _clip_duration(index: int, durations: Iterable[float] | None, fallback: float) -> float:
    values = list(durations or [])
    if index < len(values):
        try:
            return max(0.1, float(values[index]))
        except (TypeError, ValueError):
            return max(0.1, fallback)
    return max(0.1, fallback)


def build_timeline(
    *,
    params: VideoParams,
    materials: list[str],
    durations: list[float] | None = None,
    audio_file: str = "",
    audio_duration: float = 0.0,
    subtitle_path: str = "",
    bgm_file: str = "",
) -> Timeline:
    aspect = VideoAspect(params.video_aspect or VideoAspect.portrait)
    width, height = aspect.to_resolution()
    timeline = empty_timeline(width=width, height=height)
    speed = utils.normalize_clip_speed(params.video_clip_speed)
    fallback = float(params.video_clip_duration or 5)
    cursor = 0.0
    video_track = timeline.track("video")
    assert video_track is not None
    for index, source in enumerate(materials):
        duration = _clip_duration(index, durations, fallback)
        video_track.clips.append(
            TimelineClip(
                id=f"v{index}",
                source=str(source),
                in_point=0.0,
                out_point=duration * speed,
                start=cursor,
                speed=speed,
            )
        )
        cursor += duration

    if audio_file:
        narration = timeline.track("narration")
        assert narration is not None
        narration.clips.append(
            TimelineClip(
                id="narration-1",
                source=audio_file,
                in_point=0.0,
                out_point=max(0.1, float(audio_duration or 0.1)),
                start=0.0,
            )
        )
    if bgm_file:
        bgm = timeline.track("bgm")
        assert bgm is not None
        bgm.clips.append(
            TimelineClip(
                id="bgm-1",
                source=bgm_file,
                in_point=0.0,
                out_point=max(0.1, float(audio_duration or cursor or 0.1)),
                start=0.0,
            )
        )
    if subtitle_path:
        subtitles = timeline.track("subtitle")
        assert subtitles is not None
        subtitles.clips.append(
            TimelineClip(
                id="subtitle-1",
                source=subtitle_path,
                in_point=0.0,
                out_point=max(0.1, float(audio_duration or cursor or 0.1)),
                start=0.0,
            )
        )
    return timeline


def save_timeline(task_id: str, timeline: Timeline) -> str:
    output = Path(utils.task_dir(task_id)) / "timeline.json"
    output.write_text(timeline.model_dump_json(indent=2), encoding="utf-8")
    return str(output)


def load_timeline(payload: dict[str, Any] | Timeline | None) -> Timeline | None:
    if payload is None:
        return None
    if isinstance(payload, Timeline):
        return payload
    return Timeline.model_validate(payload)
