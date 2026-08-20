from __future__ import annotations

from pathlib import Path

from app.models.schema import VideoAspect
from app.services import video as video_service
from app.timeline.compile import load_timeline
from app.timeline.models import Timeline
from app.tools.base import FunctionTool, ToolContext, ToolResult
from app.tools.ffmpeg_cmd import FFmpegError, probe_media, run_ffmpeg
from app.utils import utils


def _task_output(ctx: ToolContext, name: str) -> str:
    path = Path(utils.task_dir(ctx.task_id)) / "edits"
    path.mkdir(parents=True, exist_ok=True)
    return str(path / name)


def probe(ctx: ToolContext) -> ToolResult:
    source = str(ctx.extras.get("source") or "")
    if not source:
        return ToolResult(ok=False, error="probe source is required")
    try:
        data = probe_media(source)
    except Exception as exc:
        return ToolResult(ok=False, error=str(exc)[:1000])
    return ToolResult(ok=True, data=data)


def trim_clip(ctx: ToolContext) -> ToolResult:
    source = str(ctx.extras.get("source") or "")
    start = float(ctx.extras.get("start") or 0)
    duration = float(ctx.extras.get("duration") or 0)
    if not source or duration <= 0:
        return ToolResult(ok=False, error="trim requires source and duration")
    output = str(ctx.extras.get("output") or _task_output(ctx, "trim.mp4"))
    try:
        run_ffmpeg(
            [
                "-ss",
                f"{start:.3f}",
                "-i",
                source,
                "-t",
                f"{duration:.3f}",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                output,
            ]
        )
    except FFmpegError as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(ok=True, data={"path": output}, artifacts=[output])


def concat_clips(ctx: ToolContext) -> ToolResult:
    clips = [str(item) for item in (ctx.extras.get("clips") or []) if str(item)]
    if not clips:
        return ToolResult(ok=False, error="concat requires clips")
    output = str(ctx.extras.get("output") or _task_output(ctx, "concat.mp4"))
    try:
        video_service.concat_video_clips_with_ffmpeg(
            clips,
            output,
            threads=int(ctx.params.n_threads or 2),
            output_dir=str(Path(output).parent),
        )
    except Exception as exc:
        return ToolResult(ok=False, error=str(exc)[:1000])
    return ToolResult(ok=True, data={"path": output}, artifacts=[output])


def scale_to_aspect(ctx: ToolContext) -> ToolResult:
    source = str(ctx.extras.get("source") or "")
    if not source:
        return ToolResult(ok=False, error="scale requires source")
    aspect = VideoAspect(ctx.params.video_aspect or VideoAspect.portrait)
    width, height = aspect.to_resolution()
    output = str(ctx.extras.get("output") or _task_output(ctx, "scaled.mp4"))
    try:
        run_ffmpeg(
            [
                "-i",
                source,
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
                "-c:a",
                "copy",
                output,
            ]
        )
    except FFmpegError as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(ok=True, data={"path": output}, artifacts=[output])


def set_speed(ctx: ToolContext) -> ToolResult:
    source = str(ctx.extras.get("source") or "")
    speed = utils.normalize_clip_speed(ctx.extras.get("speed") or ctx.params.video_clip_speed)
    if not source:
        return ToolResult(ok=False, error="speed requires source")
    output = str(ctx.extras.get("output") or _task_output(ctx, "speed.mp4"))
    atempo = max(0.5, min(2.0, speed))
    try:
        run_ffmpeg(
            [
                "-i",
                source,
                "-filter:v",
                f"setpts=PTS/{speed}",
                "-filter:a",
                f"atempo={atempo}",
                output,
            ]
        )
    except FFmpegError as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(ok=True, data={"path": output, "speed": speed}, artifacts=[output])


def mix_audio(ctx: ToolContext) -> ToolResult:
    video_path = str(ctx.extras.get("video_path") or "")
    audio_path = str(ctx.extras.get("audio_path") or "")
    output = str(ctx.extras.get("output") or _task_output(ctx, "mixed.mp4"))
    if not video_path or not audio_path:
        return ToolResult(ok=False, error="mix_audio requires video_path and audio_path")
    bgm_ok = video_service.generate_video(
        video_path=video_path,
        audio_path=audio_path,
        subtitle_path=str(ctx.extras.get("subtitle_path") or ""),
        output_file=output,
        params=ctx.params,
        bgm_file_override=ctx.extras.get("bgm_file_override"),
    )
    return ToolResult(
        ok=True,
        data={"path": output, "bgm_ok": bool(bgm_ok)},
        artifacts=[output],
    )


def burn_subtitles(ctx: ToolContext) -> ToolResult:
    return mix_audio(ctx)


def apply_transition(ctx: ToolContext) -> ToolResult:
    clips = [str(item) for item in (ctx.extras.get("clips") or []) if str(item)]
    audio_file = str(ctx.extras.get("audio_file") or "")
    output = str(ctx.extras.get("output") or _task_output(ctx, "transition.mp4"))
    if not clips or not audio_file:
        return ToolResult(ok=False, error="transition requires clips and audio_file")
    video_service.combine_videos(
        combined_video_path=output,
        video_paths=clips,
        audio_file=audio_file,
        video_aspect=ctx.params.video_aspect,
        video_concat_mode=ctx.params.video_concat_mode,
        video_transition_mode=ctx.params.video_transition_mode,
        max_clip_duration=ctx.params.video_clip_duration,
        threads=ctx.params.n_threads,
        clip_speed=ctx.params.video_clip_speed,
        clip_durations=ctx.extras.get("clip_durations"),
    )
    return ToolResult(ok=True, data={"path": output}, artifacts=[output])


def export_timeline(ctx: ToolContext) -> ToolResult:
    from app.services import task as tm

    timeline = load_timeline(ctx.extras.get("timeline"))
    if timeline is None:
        return ToolResult(ok=False, error="timeline is required")
    materials = timeline.video_sources()
    if not materials:
        return ToolResult(ok=False, error="timeline has no video clips")
    audio_file = str(ctx.extras.get("audio_file") or "")
    subtitle_path = str(ctx.extras.get("subtitle_path") or "")
    audio_duration = float(ctx.extras.get("audio_duration") or 0)
    narration = timeline.track("narration")
    if narration and narration.clips:
        audio_file = audio_file or narration.clips[0].source
        audio_duration = audio_duration or narration.clips[0].duration
    subtitles = timeline.track("subtitle")
    if subtitles and subtitles.clips:
        subtitle_path = subtitle_path or subtitles.clips[0].source
    try:
        finals, combined, warnings = tm.generate_final_videos(
            ctx.task_id,
            ctx.params,
            materials,
            audio_file,
            subtitle_path,
            audio_duration,
        )
    except tm._TaskCancellationCheckpoint:
        raise
    except Exception as exc:
        return ToolResult(ok=False, error=str(exc)[:1000])
    if not finals:
        return ToolResult(ok=False, error="failed to generate final video")
    return ToolResult(
        ok=True,
        data={
            "videos": finals,
            "combined_videos": combined,
            "warnings": warnings or [],
            "timeline": timeline.to_dict(),
        },
        artifacts=list(finals) + list(combined),
    )


EDIT_TOOLS = [
    FunctionTool("probe_media", "探测媒体时长、分辨率和音轨", probe),
    FunctionTool("trim_clip", "按入出点裁剪片段", trim_clip),
    FunctionTool("concat_clips", "拼接视频片段", concat_clips),
    FunctionTool("scale_to_aspect", "按作品画幅缩放填充", scale_to_aspect),
    FunctionTool("set_speed", "调整片段播放速度", set_speed),
    FunctionTool("mix_audio", "混合旁白、BGM 并输出成片", mix_audio),
    FunctionTool("burn_subtitles", "烧录字幕", burn_subtitles),
    FunctionTool("apply_transition", "按导演参数拼接并加转场", apply_transition),
    FunctionTool("export_timeline", "把时间轴编译成最终视频", export_timeline),
]
