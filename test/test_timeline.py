from app.models.schema import VideoParams
from app.timeline.compile import build_timeline
from app.tools import list_tools, run_tool
from app.tools.base import ToolContext


def test_timeline_keeps_clip_order_and_audio_track():
    params = VideoParams(video_aspect="9:16", video_clip_speed=1.0)
    timeline = build_timeline(
        params=params,
        materials=["a.mp4", "b.mp4"],
        durations=[2.0, 3.5],
        audio_file="audio.mp3",
        audio_duration=5.5,
        subtitle_path="subtitle.srt",
    )
    assert timeline.video_sources() == ["a.mp4", "b.mp4"]
    assert timeline.clip_durations() == [2.0, 3.5]
    assert timeline.track("narration").clips[0].source == "audio.mp3"
    assert timeline.track("subtitle").clips[0].source == "subtitle.srt"
    dumped = timeline.to_dict()
    assert dumped["width"] == 1080
    assert dumped["height"] == 1920


def test_tool_registry_exposes_production_and_edit_tools():
    names = {tool.name for tool in list_tools()}
    assert "generate_script" in names
    assert "match_library_materials" in names
    assert "search_online_materials" in names
    assert "probe_media" in names
    assert "export_timeline" in names
    assert "rough_cut_silence" in names


def test_trim_tool_requires_source_and_duration():
    ctx = ToolContext(task_id="t", params=VideoParams(), extras={"source": "", "duration": 0})
    result = run_tool("trim_clip", ctx)
    assert result.ok is False
    assert "trim" in (result.error or "")
