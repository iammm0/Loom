from unittest.mock import MagicMock, patch

from app.agent.runner import run_agent
from app.models.schema import VideoParams
from app.services import task as tm
from app.services.state import MemoryState
from app.tools.base import ToolContext, ToolResult
from app.tools.production import match_library_materials


def test_agent_stop_at_script_returns_script_only():
    params = VideoParams(video_subject="Coffee")
    state = MemoryState()
    with (
        patch.object(tm, "generate_script", return_value="generated script"),
        patch.object(tm.sm, "state", state),
    ):
        result = run_agent("agent-script", params, stop_at="script")
    assert result == {"script": "generated script"}
    assert state.get_task("agent-script")["status"] == tm.const.TASK_STATUS_COMPLETED


def test_agent_auto_fill_uses_material_tool_not_human_gate():
    params = VideoParams(video_subject="Coffee", material_strategy="ai_generated")
    state = MemoryState()
    with (
        patch.object(tm.seedance, "is_enabled", return_value=True),
        patch.object(tm, "generate_script", return_value="script"),
        patch.object(tm, "prepare_director_plan", return_value={"source": "test"}),
        patch.object(tm, "save_script_data"),
        patch.object(tm, "generate_audio", return_value=("audio.mp3", 4, object())),
        patch.object(tm, "generate_subtitle", return_value="subtitle.srt"),
        patch.object(tm, "get_video_materials", return_value=["clip.mp4"]),
        patch.object(
            tm,
            "generate_final_videos",
            return_value=(["final.mp4"], ["combined.mp4"], []),
        ),
        patch.object(tm.sm, "state", state),
    ):
        result = run_agent("agent-fill", params, stop_at="video")
    assert result["status"] == tm.const.TASK_STATUS_COMPLETED
    assert result["videos"] == ["final.mp4"]
    assert state.get_task("agent-fill")["status"] != tm.const.TASK_STATUS_AWAITING_MATERIAL


def test_match_library_auto_fills_missing_scenes_with_seedance():
    params = VideoParams(video_subject="Coffee", material_strategy="ai_generated")
    state = MemoryState()
    missing = [
        {"scene_index": 0, "video_prompt": "a coffee cup", "target_duration": 2}
    ]
    state.update_task("fill-seedance", missing_scenes=missing)
    generated = "scene-0000-seedance.mp4"
    calls = {"n": 0}

    def fake_materials(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return [generated]

    store = MagicMock()
    store.get_task.return_value = {"generated_scene_paths": {}}
    store.patch_task.return_value = True

    with (
        patch.object(tm, "get_video_materials", side_effect=fake_materials),
        patch("app.tools.production.seedance.is_enabled", return_value=True),
        patch(
            "app.tools.production.generate_scene_video",
            return_value=ToolResult(
                ok=True,
                data={
                    "path": generated,
                    "scene_index": 0,
                    "provider_task_id": "seedance-1",
                },
            ),
        ),
        patch("app.tools.production.get_task_store", return_value=store),
        patch.object(tm.sm, "state", state),
    ):
        result = match_library_materials(
            ToolContext(
                task_id="fill-seedance",
                params=params,
                extras={"script": "coffee", "audio_duration": 2},
            )
        )

    assert result.ok is True
    assert result.data["materials"] == [generated]
    assert calls["n"] == 2


def test_auto_fill_fails_when_seedance_cannot_generate():
    params = VideoParams(video_subject="Coffee", material_strategy="ai_generated")
    state = MemoryState()
    state.update_task(
        "fill-fail",
        missing_scenes=[{"scene_index": 0, "video_prompt": "a cup", "target_duration": 2}],
    )
    store = MagicMock()
    store.get_task.return_value = {"generated_scene_paths": {}}
    store.patch_task.return_value = True

    with (
        patch.object(tm, "get_video_materials", return_value=None),
        patch("app.tools.production.seedance.is_enabled", return_value=True),
        patch(
            "app.tools.production.generate_scene_video",
            return_value=ToolResult(ok=False, error="Seedance is not configured"),
        ),
        patch("app.tools.production.get_task_store", return_value=store),
        patch.object(tm.sm, "state", state),
    ):
        result = match_library_materials(
            ToolContext(task_id="fill-fail", params=params, extras={"script": "s"})
        )

    assert result.ok is False
    assert "Seedance" in (result.error or "")


def test_match_library_skips_seedance_when_video_api_key_missing():
    params = VideoParams(video_subject="Coffee", material_strategy="local_first")
    state = MemoryState()
    state.update_task(
        "fill-online",
        missing_scenes=[{"scene_index": 0, "video_prompt": "a cup", "target_duration": 2}],
    )
    generate = MagicMock()

    with (
        patch.object(tm, "get_video_materials", return_value=None),
        patch("app.tools.production.seedance.is_enabled", return_value=False),
        patch("app.tools.production.generate_scene_video", generate),
        patch.object(tm.sm, "state", state),
    ):
        result = match_library_materials(
            ToolContext(task_id="fill-online", params=params, extras={"script": "s"})
        )

    assert result.ok is False
    assert "在线素材" in (result.error or "")
    generate.assert_not_called()
