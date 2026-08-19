from datetime import datetime, timezone

from app.models import const
from app.services.task_stream import build_task_stream, format_elapsed


NOW = datetime(2026, 7, 22, 8, 2, tzinfo=timezone.utc)


def test_stream_marks_reported_stage_as_running_and_keeps_later_steps_pending():
    task = {
        "status": const.TASK_STATUS_PROCESSING,
        "stage": "script",
        "progress": 10,
        "generation_report": {
            "stage_timings": [
                {
                    "stage": "preflight",
                    "status": "completed",
                    "elapsed_seconds": 1.2,
                    "details": {},
                },
                {
                    "stage": "director_plan",
                    "status": "processing",
                    "started_at": "2026-07-22T08:01:30+00:00",
                    "elapsed_seconds": None,
                    "details": {},
                },
            ]
        },
    }

    stream = build_task_stream(task, now=NOW)
    steps = {step["key"]: step for step in stream["steps"]}

    assert stream["headline"] == "正在生成作品"
    assert stream["active_label"] == "规划分镜"
    assert steps["preflight"]["state"] == "completed"
    assert steps["director_plan"]["state"] == "running"
    assert steps["director_plan"]["elapsed_label"] == "30 秒"
    assert steps["audio"]["state"] == "pending"


def test_stream_aggregates_repeated_material_rounds_and_exposes_result_count():
    task = {
        "status": const.TASK_STATUS_PROCESSING,
        "stage": "materials",
        "progress": 50,
        "generation_report": {
            "stage_timings": [
                {
                    "stage": "materials",
                    "status": "completed",
                    "elapsed_seconds": 20,
                    "details": {"material_count": 2},
                },
                {
                    "stage": "materials",
                    "status": "completed",
                    "elapsed_seconds": 15,
                    "details": {"material_count": 4},
                },
                {
                    "stage": "video",
                    "status": "processing",
                    "started_at": "2026-07-22T08:01:50+00:00",
                    "details": {},
                },
            ]
        },
    }

    stream = build_task_stream(task, now=NOW)
    steps = {step["key"]: step for step in stream["steps"]}

    assert steps["materials"]["state"] == "completed"
    assert steps["materials"]["elapsed_label"] == "35 秒"
    assert steps["materials"]["meta"] == "4 个分镜"
    assert steps["video"]["state"] == "running"


def test_stream_maps_waiting_and_failed_states_to_the_relevant_step():
    approval_waiting = build_task_stream(
        {
            "status": const.TASK_STATUS_AWAITING_APPROVAL,
            "stage": "awaiting_supplemental_scenes",
            "progress": 45,
        },
        now=NOW,
    )
    upload_waiting = build_task_stream(
        {
            "status": const.TASK_STATUS_AWAITING_MATERIAL,
            "stage": "awaiting_material_upload",
            "progress": 45,
        },
        now=NOW,
    )
    failed = build_task_stream(
        {
            "status": const.TASK_STATUS_FAILED,
            "stage": "failed",
            "failed_stage": "audio",
            "progress": 30,
        },
        now=NOW,
    )

    waiting_steps = {step["key"]: step for step in approval_waiting["steps"]}
    failed_steps = {step["key"]: step for step in failed["steps"]}
    assert approval_waiting["headline"] == "等待确认后继续"
    assert upload_waiting["headline"] == "等待上传分镜素材"
    assert upload_waiting["is_live"] is False
    assert waiting_steps["materials"]["state"] == "waiting"
    assert failed["headline"] == "作品生成失败"
    assert failed_steps["audio"]["state"] == "failed"


def test_stream_marks_delivery_complete_and_formats_long_elapsed_time():
    stream = build_task_stream(
        {
            "status": const.TASK_STATUS_COMPLETED,
            "stage": "completed",
            "progress": 100,
        },
        now=NOW,
    )

    assert stream["headline"] == "作品已生成"
    assert stream["steps"][-1]["state"] == "completed"
    assert format_elapsed(65) == "1 分 05 秒"
    assert format_elapsed(3660) == "1 小时 01 分"


def test_completed_legacy_task_does_not_leave_pipeline_steps_pending():
    stream = build_task_stream(
        {
            "status": const.TASK_STATUS_COMPLETED,
            "stage": "completed",
            "progress": 100,
        },
        now=NOW,
    )

    assert {step["state"] for step in stream["steps"]} == {"completed"}
