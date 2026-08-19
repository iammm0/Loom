from __future__ import annotations

import math
from typing import Any

from app.models.schema import VideoParams, VideoTransitionMode
from app.services import llm, video
from app.utils import utils


def _apply_director_plan(params: VideoParams, plan: dict[str, Any]) -> None:
    params.voice_rate = float(plan.get("voice_rate", params.voice_rate))
    params.video_clip_duration = int(
        plan.get("video_clip_duration", params.video_clip_duration)
    )
    params.video_clip_speed = utils.normalize_clip_speed(
        plan.get("video_clip_speed", params.video_clip_speed)
    )
    transition = plan.get("video_transition_mode", params.video_transition_mode)
    params.video_transition_mode = (
        VideoTransitionMode.none
        if transition is None
        else VideoTransitionMode(transition)
    )
    params.bgm_volume = float(plan.get("bgm_volume", params.bgm_volume))
    if params.bgm_type == "sonilo":
        params.sonilo_bgm_prompt = str(
            plan.get("sonilo_bgm_prompt", params.sonilo_bgm_prompt) or ""
        )


def _recommendation(target_duration: str, estimated: float) -> str:
    ranges = {
        "15-30": (15, 30),
        "30-60": (30, 60),
    }
    valid = ranges.get(str(target_duration))
    if valid is None:
        return f"已按文案内容自然确定篇幅，预计旁白 {estimated:.2f} 秒。"
    if estimated < valid[0]:
        return f"当前文案偏短，建议选择 {valid[0]}～{valid[1]} 秒档位或补充旁白。"
    if estimated > valid[1]:
        return f"当前文案偏长，建议选择更长档位或精简约 {estimated - valid[1]:.1f} 秒内容。"
    return "当前文案时长与配置基本匹配。"


def prepare(
    params: VideoParams,
    *,
    video_subject: str | None = None,
) -> dict[str, Any]:
    """Generate a non-queued task draft and return deterministic duration advice."""
    draft = VideoParams.model_validate(params.model_dump(mode="json", warnings=False))
    if video_subject is not None:
        draft.video_subject = str(video_subject).strip()
    script = str(draft.video_script or "").strip()
    if not script:
        script = llm.generate_script(
            video_subject=draft.video_subject,
            language=draft.video_language,
            paragraph_number=draft.paragraph_number,
            video_script_prompt=draft.video_script_prompt,
            custom_system_prompt=draft.custom_system_prompt,
            target_duration=draft.target_duration,
            ai_director_enabled=draft.ai_director_enabled,
        )
    if not script or "Error: " in script:
        return {
            "ready": False,
            "params": draft.model_dump(mode="json", warnings=False),
            "script": script,
            "director_plan": {},
            "analysis": {
                "estimated_narration_duration": 0.0,
                "messages": ["文案生成失败，请修改主题或稍后重试。"],
                "hard_error": True,
            },
            "model_token_usage": llm.get_collected_token_usage(),
        }

    if draft.ai_director_enabled:
        director_plan = llm.generate_director_plan(
            video_subject=draft.video_subject,
            video_script=script,
            video_request=draft.video_script_prompt,
            target_duration=draft.target_duration,
        )
        _apply_director_plan(draft, director_plan)
    else:
        director_plan = {
            "source": "manual",
            "video_clip_duration": draft.video_clip_duration,
            "video_clip_speed": draft.video_clip_speed,
            "voice_rate": draft.voice_rate,
        }

    natural_estimate = llm.estimate_narration_duration(script)
    estimated = round(natural_estimate / max(0.1, float(draft.voice_rate or 1.0)), 2)
    target_ranges = {
        "15-30": (15, 30),
        "30-60": (30, 60),
    }
    target_range = target_ranges.get(str(draft.target_duration))
    target_min, target_max = target_range or (None, None)
    required_visual_duration = video.get_required_video_duration(estimated)
    required_scene_count = max(
        1,
        math.ceil(
            required_visual_duration / max(1, int(draft.video_clip_duration or 1))
        ),
    )
    messages = [_recommendation(str(draft.target_duration), estimated)]
    hard_error = False
    if target_range and (estimated < target_range[0] or estimated > target_range[1]):
        messages.append(
            f"预计旁白 {estimated:.2f} 秒，当前配置范围为 {target_range[0]}～{target_range[1]} 秒，"
            "确认后仍会按实际旁白时长规划分镜。"
        )
    director_plan = {
        **director_plan,
        "source": director_plan.get("source") or "preflight",
        "target_duration": draft.target_duration,
        "estimated_narration_duration": estimated,
        "required_visual_duration": round(required_visual_duration, 3),
        "required_scene_count": required_scene_count,
    }
    draft.video_script = script
    return {
        "ready": True,
        "params": draft.model_dump(mode="json", warnings=False),
        "script": script,
        "director_plan": director_plan,
        "analysis": {
            "estimated_narration_duration": estimated,
            "target_min": target_min,
            "target_max": target_max,
            "required_visual_duration": round(required_visual_duration, 3),
            "required_scene_count": required_scene_count,
            "video_clip_duration": draft.video_clip_duration,
            "messages": messages,
            "hard_error": hard_error,
        },
        "model_token_usage": llm.get_collected_token_usage(),
    }
