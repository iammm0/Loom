from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


TrackType = Literal["video", "narration", "bgm", "subtitle"]


class TimelineClip(BaseModel):
    id: str
    source: str
    in_point: float = 0.0
    out_point: float | None = None
    start: float = 0.0
    speed: float = 1.0

    @property
    def source_duration(self) -> float:
        if self.out_point is None:
            return 0.0
        return max(0.0, float(self.out_point) - float(self.in_point))

    @property
    def duration(self) -> float:
        speed = self.speed if self.speed > 0 else 1.0
        return self.source_duration / speed


class TimelineTrack(BaseModel):
    id: str
    type: TrackType
    clips: list[TimelineClip] = Field(default_factory=list)


class Timeline(BaseModel):
    fps: int = 30
    width: int = 1080
    height: int = 1920
    tracks: list[TimelineTrack] = Field(default_factory=list)

    def track(self, track_type: TrackType) -> TimelineTrack | None:
        for item in self.tracks:
            if item.type == track_type:
                return item
        return None

    def video_sources(self) -> list[str]:
        video_track = self.track("video")
        if not video_track:
            return []
        return [clip.source for clip in video_track.clips if clip.source]

    def clip_durations(self) -> list[float]:
        video_track = self.track("video")
        if not video_track:
            return []
        return [max(0.1, clip.duration) for clip in video_track.clips]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def empty_timeline(*, width: int = 1080, height: int = 1920, fps: int = 30) -> Timeline:
    return Timeline(
        fps=fps,
        width=width,
        height=height,
        tracks=[
            TimelineTrack(id="V1", type="video"),
            TimelineTrack(id="A1", type="narration"),
            TimelineTrack(id="A2", type="bgm"),
            TimelineTrack(id="S1", type="subtitle"),
        ],
    )
