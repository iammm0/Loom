import json

from app.models.schema import VideoParams
from app.services import generation_report
from app.services.task_store import TaskStore


def test_generation_report_persists_stage_and_seedance_bill(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "app.db")
    store.enqueue(VideoParams(video_subject="报告测试"), task_id="report-task")
    monkeypatch.setattr(generation_report, "get_task_store", lambda: store)
    monkeypatch.setattr(
        generation_report.utils,
        "task_dir",
        lambda _: str(tmp_path / "tasks" / "report-task"),
    )

    generation_report.initialize("report-task", details={"subject": "报告测试"})
    with generation_report.stage("report-task", "script", "生成文案") as details:
        details["characters"] = 120
    generation_report.record_seedance_scene(
        "report-task",
        {
            "scene_index": 0,
            "provider_task_id": "provider-1",
            "billable": True,
            "tokens": 108900,
            "estimated_cost_cny": 5.0094,
            "calculation_method": "api_usage",
        },
    )
    generation_report.finalize("report-task", "completed")

    report = store.get_task("report-task")["generation_report"]
    assert report["status"] == "completed"
    assert report["duration_limit_seconds"] is None
    assert report["started_at"].endswith("+08:00")
    assert "." not in report["started_at"]
    assert report["stage_timings"][0]["details"]["characters"] == 120
    assert report["seedance_billing"]["total_tokens"] == 108900
    assert report["seedance_billing"]["estimated_total_cost_cny"] == 5.0094
    assert "budget_limit_cny" not in report["seedance_billing"]
    assert "budget_status" not in report["seedance_billing"]
    assert report["seedance_billing"]["is_estimate"] is False
    report_file = tmp_path / "tasks" / "report-task" / "generation-report.json"
    assert json.loads(report_file.read_text(encoding="utf-8"))["status"] == "completed"


def test_generation_report_marks_unknown_bill(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "app.db")
    store.enqueue(VideoParams(video_subject="未知账单"), task_id="unknown-task")
    monkeypatch.setattr(generation_report, "get_task_store", lambda: store)
    monkeypatch.setattr(
        generation_report.utils,
        "task_dir",
        lambda _: str(tmp_path / "tasks" / "unknown-task"),
    )
    generation_report.initialize("unknown-task")
    generation_report.record_seedance_scene(
        "unknown-task",
        {
            "scene_index": 0,
            "provider_task_id": None,
            "billable": None,
            "tokens": None,
            "estimated_cost_cny": None,
            "calculation_method": "unavailable",
        },
    )

    billing = store.get_task("unknown-task")["generation_report"]["seedance_billing"]
    assert billing["has_unknown_cost"] is True
    assert billing["estimated_total_cost_cny"] == 0.0


def test_generation_report_removes_legacy_seedance_budget_fields(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "app.db")
    store.enqueue(VideoParams(video_subject="旧预算报告"), task_id="legacy-task")
    monkeypatch.setattr(generation_report, "get_task_store", lambda: store)
    monkeypatch.setattr(
        generation_report.utils,
        "task_dir",
        lambda _: str(tmp_path / "tasks" / "legacy-task"),
    )
    report = generation_report.initialize("legacy-task")
    report["seedance_billing"].update(
        {
            "budget_limit_cny": 30.0,
            "reserved_cost_cny": 9.5,
            "remaining_budget_cny": 20.5,
            "budget_status": "within_budget",
        }
    )
    store.patch_task("legacy-task", generation_report=report)

    cleaned = generation_report.initialize("legacy-task")

    assert cleaned["seedance_billing"]["estimated_total_cost_cny"] == 0.0
    assert not {
        "budget_limit_cny",
        "reserved_cost_cny",
        "remaining_budget_cny",
        "budget_status",
    }.intersection(cleaned["seedance_billing"])
