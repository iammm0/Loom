from unittest.mock import patch

from app.models.schema import VideoParams
from app.services import llm, preflight


def test_custom_script_preflight_reuses_script_and_resolves_scene_count():
    params = VideoParams(
        video_subject="ForentX",
        video_script="在线签约和在线收租，让租赁管理更高效。" * 8,
        video_clip_duration=3,
        ai_director_enabled=True,
    )
    with (
        patch.object(llm, "generate_script") as generate_script,
        patch.object(
            llm,
            "generate_director_plan",
            return_value={
                "voice_rate": 1.0,
                "video_clip_duration": 6,
                "video_clip_speed": 1.0,
                "video_transition_mode": None,
                "bgm_volume": 0.1,
            },
        ),
    ):
        result = preflight.prepare(params)

    generate_script.assert_not_called()
    assert result["ready"] is True
    assert result["params"]["video_script"] == params.video_script
    assert result["analysis"]["video_clip_duration"] == 6
    assert result["analysis"]["required_scene_count"] >= 1


def test_auto_duration_preflight_accepts_natural_script_length():
    params = VideoParams(
        video_subject="Long",
        video_script="这是一段必须精简的超长旁白内容。" * 100,
        ai_director_enabled=False,
    )

    result = preflight.prepare(params)

    assert result["ready"] is True
    assert result["analysis"]["hard_error"] is False
    assert result["analysis"]["estimated_narration_duration"] > 60
    assert result["analysis"]["target_min"] is None
    assert result["analysis"]["target_max"] is None
    assert any("自然确定篇幅" in message for message in result["analysis"]["messages"])
    assert not any("当前配置范围" in message for message in result["analysis"]["messages"])


def test_ai_script_preflight_generates_script_only_once():
    params = VideoParams(video_subject="Coffee", video_script="")
    with (
        patch.object(llm, "generate_script", return_value="一段简短旁白。") as generate,
        patch.object(
            llm,
            "generate_director_plan",
            return_value={
                "voice_rate": 1.0,
                "video_clip_duration": 5,
                "video_clip_speed": 1.0,
                "video_transition_mode": None,
                "bgm_volume": 0.1,
            },
        ),
    ):
        result = preflight.prepare(params)

    generate.assert_called_once()
    assert result["params"]["video_script"] == "一段简短旁白。"
