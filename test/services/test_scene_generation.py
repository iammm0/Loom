from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

from app.models import const
from app.models.schema import VideoParams
from app.services import scene_generation, seedance
from app.services.task_store import TaskStore


def _waiting_task(store: TaskStore, task_id: str):
    scenes = [
        {
            "scene_index": 0,
            "text": "第一段",
            "target_duration": 3,
            "material_prompt": "first prompt",
        },
        {
            "scene_index": 1,
            "text": "第二段",
            "target_duration": 4,
            "material_prompt": "second prompt",
        },
    ]
    store.update_runtime_task(
        task_id,
        state=const.TASK_STATE_PROCESSING,
        status=const.TASK_STATUS_AWAITING_MATERIAL,
        stage="awaiting_material_upload",
        scene_plan=scenes,
        required_upload_scene_indexes=[0, 1],
        missing_scenes=scenes,
    )


@contextmanager
def _report_stage(*args, **kwargs):
    yield {}


def _mock_report(monkeypatch):
    monkeypatch.setattr(scene_generation.generation_report, "initialize", MagicMock())
    monkeypatch.setattr(scene_generation.generation_report, "stage", _report_stage)
    monkeypatch.setattr(
        scene_generation.generation_report, "record_seedance_scene", MagicMock()
    )
    monkeypatch.setattr(
        scene_generation.generation_report, "record_paid_operation", MagicMock()
    )


def test_single_scene_worker_generates_only_requested_scene(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(
        VideoParams(video_subject="单分镜", video_aspect="9:16"), task_id="task"
    )
    store.claim_next("worker", max_concurrent_tasks=1)
    _waiting_task(store, task["task_id"])
    job = store.reserve_scene_generation(task["task_id"], 1)
    monkeypatch.setattr(scene_generation, "get_task_store", lambda: store)
    monkeypatch.setattr(scene_generation.utils, "task_dir", lambda _: str(tmp_path))
    _mock_report(monkeypatch)
    library = MagicMock()
    monkeypatch.setattr(scene_generation, "get_material_library", lambda: library)
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        kwargs["task_created_callback"]("provider-1")
        Path(kwargs["output_path"]).write_bytes(b"video")
        return (
            kwargs["output_path"],
            "provider-1",
            {
                "provider_task_id": "provider-1",
                "status": "completed",
                "billable": True,
                "tokens": 108900,
                "estimated_cost_cny": 5.0094,
            },
        )

    monkeypatch.setattr(scene_generation.seedance, "generate_clip_detailed", generate)

    scene_generation._run_scene_generation(
        task["task_id"], 1, job["run_id"], ""
    )
    completed = store.get_task(task["task_id"])

    assert len(calls) == 1
    assert calls[0]["prompt"] == "second prompt"
    assert completed["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert set(completed["generated_scene_paths"]) == {"1"}
    assert [scene["scene_index"] for scene in completed["missing_scenes"]] == [0]
    assert completed["scene_generation_jobs"]["1"]["status"] == "completed"
    library.import_task_scene.assert_called_once()


def test_single_scene_worker_failure_does_not_fail_whole_task(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(VideoParams(video_subject="欠费"), task_id="task")
    store.claim_next("worker", max_concurrent_tasks=1)
    _waiting_task(store, task["task_id"])
    job = store.reserve_scene_generation(task["task_id"], 0)
    monkeypatch.setattr(scene_generation, "get_task_store", lambda: store)
    monkeypatch.setattr(scene_generation.utils, "task_dir", lambda _: str(tmp_path))
    _mock_report(monkeypatch)

    def fail(**kwargs):
        raise seedance.SeedanceError(
            "account has an overdue balance",
            details={
                "provider_task_id": None,
                "status": "failed",
                "provider_status": "",
                "billable": False,
            },
        )

    monkeypatch.setattr(scene_generation.seedance, "generate_clip_detailed", fail)

    scene_generation._run_scene_generation(
        task["task_id"], 0, job["run_id"], ""
    )
    failed = store.get_task(task["task_id"])

    assert failed["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert failed["stage"] == "awaiting_material_upload"
    assert failed["scene_generation_jobs"]["0"]["status"] == "failed"
    assert "overdue balance" in failed["scene_generation_jobs"]["0"]["error"]
    assert [scene["scene_index"] for scene in failed["missing_scenes"]] == [0, 1]
