import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.config import config
from app.models import const
from app.models.schema import VideoParams
from app.services.task_store import TaskStore, TaskStoreError, TaskWorkerPool


def _params(subject="Coffee"):
    return VideoParams(video_subject=subject, video_script="A short script")


def test_queue_persists_across_store_instances(tmp_path):
    database = tmp_path / "app.db"
    first = TaskStore(database)
    created = first.enqueue(_params(), stop_at="audio")

    restored = TaskStore(database).get_task(created["task_id"])

    assert restored["status"] == const.TASK_STATUS_QUEUED
    assert restored["params"]["video_subject"] == "Coffee"
    assert restored["stop_at"] == "audio"
    assert restored["conversation_id"]


def test_enqueue_attaches_local_tasks_to_conversation(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    first = store.enqueue(
        _params("Coffee"),
        payload={"user_prompt": "雨天窗边的手冲咖啡\n文案"},
    )
    second = store.enqueue(
        _params("Follow-up"),
        conversation_id=first["conversation_id"],
        payload={"user_prompt": "再短一点"},
    )

    assert first["conversation_id"]
    assert second["conversation_id"] == first["conversation_id"]
    conversation = store.get_conversation(first["conversation_id"])
    assert [turn["prompt"] for turn in conversation["turns"]] == [
        "雨天窗边的手冲咖啡\n文案",
        "再短一点",
    ]
    listed = store.list_conversations()
    assert listed[0]["conversation_id"] == first["conversation_id"]
    assert listed[0]["task_count"] == 2


def test_rename_and_delete_conversation(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    first = store.enqueue(
        _params("Coffee"),
        payload={"user_prompt": "雨天窗边的手冲咖啡"},
    )
    conversation_id = first["conversation_id"]

    renamed = store.rename_conversation(conversation_id, "手冲日记")
    assert renamed["title"] == "手冲日记"
    assert store.list_conversations()[0]["title"] == "手冲日记"

    deleted = store.delete_conversation(conversation_id)
    assert deleted == [first["task_id"]]
    assert store.get_conversation(conversation_id) is None
    assert store.get_task(first["task_id"]) is None
    assert store.list_conversations() == []


def test_rename_task_updates_subject_and_params(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params("Coffee"))

    renamed = store.rename_task(task["task_id"], "雨天窗边")
    assert renamed["video_subject"] == "雨天窗边"
    assert renamed["params"]["video_subject"] == "雨天窗边"


def test_existing_tasks_are_backfilled_into_conversations(tmp_path):
    database = tmp_path / "app.db"
    store = TaskStore(database)
    task = store.enqueue(_params("历史剪辑"))
    with store.connection() as connection:
        connection.execute("UPDATE tasks SET conversation_id = NULL")
        connection.execute("DELETE FROM conversations")

    restored = TaskStore(database).get_task(task["task_id"])
    conversation = TaskStore(database).get_conversation(restored["conversation_id"])

    assert restored["conversation_id"]
    assert conversation["turns"][0]["task_id"] == task["task_id"]
    assert "历史剪辑" in conversation["turns"][0]["prompt"]


def test_enqueue_persists_preflight_payload(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(
        _params(),
        payload={
            "director_plan": {"video_clip_duration": 6},
            "preflight_model_token_usage": {"total": {"total_tokens": 120}},
        },
    )

    assert task["director_plan"]["video_clip_duration"] == 6
    assert task["preflight_model_token_usage"]["total"]["total_tokens"] == 120


def test_claim_is_atomic_and_respects_global_concurrency(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    store.enqueue(_params("One"))
    store.enqueue(_params("Two"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(
            executor.map(
                lambda worker: store.claim_next(worker, max_concurrent_tasks=1),
                ("worker-a", "worker-b"),
            )
        )

    active = [task for task in claimed if task]
    assert len(active) == 1
    assert active[0]["status"] == const.TASK_STATUS_PROCESSING
    tasks, _ = store.list_tasks(page_size=10)
    assert sorted(task["status"] for task in tasks) == ["processing", "queued"]


def test_cancel_queued_task_is_immediate(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())

    status = store.request_cancel(task["task_id"])

    assert status == const.TASK_STATUS_CANCELLED
    assert store.get_task(task["task_id"])["cancel_requested"] is True


def test_cancel_running_task_waits_for_checkpoint(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)

    status = store.request_cancel(task["task_id"])

    assert status == const.TASK_STATUS_CANCELLATION_REQUESTED
    assert store.is_cancel_requested(task["task_id"])


def test_payload_patch_preserves_waiting_status_and_finishes_attempt(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params("Waiting"))
    store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_PROCESSING,
        progress=45,
        status=const.TASK_STATUS_AWAITING_MATERIAL,
        stage="awaiting_material_upload",
        required_upload_scene_indexes=[11],
    )

    waiting_attempt = store.get_attempts(task["task_id"])[0]
    assert waiting_attempt["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert waiting_attempt["finished_at"] is not None

    store.patch_task(
        task["task_id"],
        generation_report={"status": "processing", "updated_at": "now"},
    )

    patched = store.get_task(task["task_id"])
    assert patched["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert patched["stage"] == "awaiting_material_upload"
    assert patched["required_upload_scene_indexes"] == [11]
    assert patched["generation_report"]["status"] == "processing"
    assert patched.get("lease_owner") is None
    assert patched.get("lease_until") is None
    assert store.get_attempts(task["task_id"])[0] == waiting_attempt

    queued = store.enqueue(_params("Next"))
    claimed = store.claim_next("next-worker", max_concurrent_tasks=1)
    assert claimed["task_id"] == queued["task_id"]


def test_retry_keeps_identity_params_and_pipeline_stage(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params(), stop_at="audio")
    claimed = store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_FAILED,
        status=const.TASK_STATUS_FAILED,
        stage="audio",
        error="failed",
    )
    monkeypatch.setattr(
        "app.services.task_store.utils.task_dir", lambda _: str(tmp_path / "task")
    )

    retried = store.retry_task(task["task_id"])

    assert retried["task_id"] == claimed["task_id"]
    assert retried["attempt_count"] == 1
    assert retried["params"]["video_subject"] == "Coffee"
    assert retried["stop_at"] == "audio"
    assert retried["status"] == const.TASK_STATUS_QUEUED


def test_retry_can_override_stale_voice_params(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(
        VideoParams(
            video_subject="Coffee",
            voice_name="zh-CN-XiaoxiaoNeural-Female",
        ),
        stop_at="audio",
    )
    store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_FAILED,
        status=const.TASK_STATUS_FAILED,
        stage="audio",
        error="failed",
    )
    monkeypatch.setattr(
        "app.services.task_store.utils.task_dir", lambda _: str(tmp_path / "task")
    )

    retried = store.retry_task(
        task["task_id"],
        params_overrides={"voice_name": "mimo:mimo_default-Female"},
    )

    assert retried["params"]["voice_name"] == "mimo:mimo_default-Female"
    assert retried["params"]["video_subject"] == "Coffee"
    assert retried["stop_at"] == "audio"


def test_retry_preserves_paid_seedance_checkpoints(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params(), stop_at="video")
    store.claim_next("worker", max_concurrent_tasks=1)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    generated = task_dir / "scene-0000-ai.mp4"
    generated.write_bytes(b"video")
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_FAILED,
        status=const.TASK_STATUS_FAILED,
        stage="materials",
        error="one provider scene failed",
        script="existing script",
        seedance_provider_task_ids={"0": "provider-0", "1": "provider-1"},
        generated_scene_paths={"0": str(generated)},
    )
    monkeypatch.setattr(
        "app.services.task_store.utils.task_dir", lambda _: str(task_dir)
    )

    retried = store.retry_task(task["task_id"])

    assert retried["status"] == const.TASK_STATUS_QUEUED
    assert retried["script"] == "existing script"
    assert retried["seedance_provider_task_ids"] == {
        "0": "provider-0",
        "1": "provider-1",
    }
    assert retried["generated_scene_paths"] == {"0": str(generated)}
    assert "error" not in retried
    assert generated.exists()


def test_batch_uses_shared_params_and_distinct_subjects(tmp_path):
    store = TaskStore(tmp_path / "app.db")

    batch_id, tasks = store.enqueue_batch(
        ["One", "Two"], _params("ignored"), batch_name="Morning batch"
    )

    assert len(tasks) == 2
    assert {task["batch_id"] for task in tasks} == {batch_id}
    assert {task["params"]["video_subject"] for task in tasks} == {"One", "Two"}
    batches = store.list_batches()
    assert len(batches) == 1
    assert batches[0]["batch_id"] == batch_id
    assert batches[0]["name"] == "Morning batch"
    assert batches[0]["task_count"] == 2
    assert batches[0]["created_at"] <= batches[0]["updated_at"]
    filtered, total = store.list_tasks(page_size=10, batch_id=batch_id)
    assert total == 2
    assert {task["task_id"] for task in filtered} == {task["task_id"] for task in tasks}


def test_all_scene_uploads_are_required_before_task_returns_to_queue(
    tmp_path, monkeypatch
):
    task_root = tmp_path / "tasks"
    monkeypatch.setattr(
        "app.services.task_store.utils.task_dir",
        lambda task_id="": str(task_root / task_id) if task_id else str(task_root),
    )
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_PROCESSING,
        status=const.TASK_STATUS_AWAITING_MATERIAL,
        stage="awaiting_material_upload",
        scene_plan=[
            {"scene_index": 0, "material_prompt": "first"},
            {"scene_index": 1, "material_prompt": "second"},
        ],
    )

    upload_dir = task_root / task["task_id"] / "material-uploads"
    upload_dir.mkdir(parents=True)
    first = upload_dir / "first.mp4"
    first.write_bytes(b"first")
    attached, old_path = store.attach_scene_material(
        task["task_id"], 0, str(first), original_name="first.mp4"
    )

    assert old_path == ""
    assert attached["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert [scene["scene_index"] for scene in attached["missing_scenes"]] == [1]
    with pytest.raises(TaskStoreError, match="incomplete"):
        store.confirm_scene_materials(task["task_id"])

    second = upload_dir / "second.mp4"
    second.write_bytes(b"second")
    store.attach_scene_material(task["task_id"], 1, str(second))
    confirmed = store.confirm_scene_materials(task["task_id"])

    assert confirmed["status"] == const.TASK_STATUS_QUEUED
    assert confirmed["stage"] == "queued"
    assert confirmed["material_upload_confirmed"] is True
    assert confirmed["uploaded_scene_paths"] == {
        "0": str(first.resolve()),
        "1": str(second.resolve()),
    }
    assert confirmed["scene_plan"][0]["material_prompt"] == "first"


def test_failed_scene_plan_can_resume_at_material_upload_stage(tmp_path, monkeypatch):
    task_root = tmp_path / "tasks"
    monkeypatch.setattr(
        "app.services.task_store.utils.task_dir",
        lambda task_id="": str(task_root / task_id) if task_id else str(task_root),
    )
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(
        VideoParams(
            video_subject="Coffee",
            material_strategy="ai_generated",
        )
    )
    store.claim_next("worker", max_concurrent_tasks=1)
    upload_dir = task_root / task["task_id"] / "material-uploads"
    upload_dir.mkdir(parents=True)
    uploaded = upload_dir / "first.mp4"
    uploaded.write_bytes(b"first")
    scenes = [
        {"scene_index": 0, "material_prompt": "first"},
        {"scene_index": 1, "material_prompt": "second"},
    ]
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_FAILED,
        progress=45,
        status=const.TASK_STATUS_FAILED,
        stage="materials",
        failed_stage="materials",
        error="failed to prepare video materials",
        scene_plan=scenes,
        uploaded_scene_paths={"0": str(uploaded)},
    )

    resumed = store.resume_scene_material_uploads(task["task_id"])

    assert resumed["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert resumed["stage"] == "awaiting_material_upload"
    assert resumed["required_upload_scene_indexes"] == [1]
    assert resumed["uploaded_scene_paths"] == {"0": str(uploaded.resolve())}
    assert [scene["scene_index"] for scene in resumed["missing_scenes"]] == [1]
    assert resumed["material_upload_confirmed"] is False
    assert resumed["error"] is None
    assert resumed["failed_stage"] is None


def test_seedance_overdue_failure_recovers_only_missing_scenes(
    tmp_path, monkeypatch
):
    task_root = tmp_path / "tasks"
    monkeypatch.setattr(
        "app.services.task_store.utils.task_dir",
        lambda task_id="": str(task_root / task_id) if task_id else str(task_root),
    )
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    generated_dir = task_root / task["task_id"] / "materials"
    generated_dir.mkdir(parents=True)
    generated = generated_dir / "scene-0000-ai.mp4"
    generated.write_bytes(b"generated")
    scenes = [
        {"scene_index": 0, "material_prompt": "existing"},
        {"scene_index": 1, "material_prompt": "missing"},
    ]
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_FAILED,
        progress=45,
        status=const.TASK_STATUS_FAILED,
        stage="pipeline",
        failed_stage="pipeline",
        error="InsufficientVisualDurationError",
        scene_plan=scenes,
        generated_scene_paths={"0": str(generated)},
        missing_scenes=[scenes[1]],
        seedance_errors=[
            {
                "scene_index": 1,
                "error": "The request failed because your account has an overdue balance.",
            }
        ],
    )

    recovered = store.recover_seedance_billing_failures()
    resumed = store.get_task(task["task_id"])

    assert recovered == [task["task_id"]]
    assert resumed["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert resumed["stage"] == "awaiting_material_upload"
    assert resumed["required_upload_scene_indexes"] == [1]
    assert resumed["generated_scene_paths"] == {"0": str(generated.resolve())}
    assert [scene["scene_index"] for scene in resumed["missing_scenes"]] == [1]
    assert resumed["material_recovery_reason"] == "seedance_billing_failure"
    assert resumed["material_recovery_error"] == "InsufficientVisualDurationError"


def test_non_billing_seedance_failure_is_not_auto_recovered(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_FAILED,
        status=const.TASK_STATUS_FAILED,
        stage="materials",
        failed_stage="materials",
        scene_plan=[{"scene_index": 0}],
        seedance_errors=[
            {
                "scene_index": 0,
                "error": "HTTP 404: The model or endpoint does not exist",
            }
        ],
    )

    assert store.recover_seedance_billing_failures() == []
    assert store.get_task(task["task_id"])["status"] == const.TASK_STATUS_FAILED


def test_visual_duration_failure_appends_only_missing_supplemental_scenes(
    tmp_path, monkeypatch
):
    task_root = tmp_path / "tasks"
    monkeypatch.setattr(
        "app.services.task_store.utils.task_dir",
        lambda task_id="": str(task_root / task_id) if task_id else str(task_root),
    )
    monkeypatch.setattr("app.services.video.get_audio_duration", lambda _: 37.66)
    monkeypatch.setattr(
        "app.services.video.get_effective_visual_duration",
        lambda *args, **kwargs: (24.0, [6.0, 6.0, 6.0, 6.0]),
    )
    store = TaskStore(tmp_path / "app.db")
    params = _params()
    params.video_clip_duration = 6
    params.video_script = "第一段旁白，第二段旁白，第三段旁白，第四段旁白。"
    task = store.enqueue(params)
    store.claim_next("worker", max_concurrent_tasks=1)
    material_dir = task_root / task["task_id"] / "materials"
    material_dir.mkdir(parents=True)
    audio_file = task_root / task["task_id"] / "audio.mp3"
    audio_file.write_bytes(b"audio")
    resolved_paths = {}
    scenes = []
    for index in range(4):
        material = material_dir / f"scene-{index}.mp4"
        material.write_bytes(b"video")
        resolved_paths[str(index)] = str(material)
        scenes.append({"scene_index": index, "text": f"scene {index}"})
    original_error = (
        "InsufficientVisualDurationError: visual duration 24.000s is shorter "
        "than required 37.760s"
    )
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_FAILED,
        status=const.TASK_STATUS_FAILED,
        stage="pipeline",
        failed_stage="pipeline",
        error=original_error,
        audio_file=str(audio_file),
        audio_duration=38,
        director_plan={"video_clip_duration": 6},
        scene_plan=scenes,
        resolved_scene_paths=resolved_paths,
        generation_report={"task_details": {}, "status": "failed"},
    )

    recovered = store.recover_insufficient_visual_duration_failures()
    resumed = store.get_task(task["task_id"])

    assert recovered == [task["task_id"]]
    assert resumed["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert resumed["stage"] == "awaiting_material_upload"
    assert resumed["required_upload_scene_indexes"] == [4, 5, 6]
    assert len(resumed["scene_plan"]) == 7
    assert [
        scene["scene_index"]
        for scene in resumed["scene_plan"]
        if scene.get("duration_shortfall_scene")
    ] == [4, 5, 6]
    assert resumed["resolved_scene_paths"] == {
        key: str(Path(value).resolve()) for key, value in resolved_paths.items()
    }
    assert resumed["material_recovery_error"] == original_error
    assert resumed["material_recovery_reason"] == "insufficient_visual_duration"
    assert resumed["error"] is None
    assert resumed["failed_stage"] is None
    assert resumed["generation_report"]["task_details"][
        "material_recovery_error"
    ] == original_error
    assert resumed["generation_report"]["updated_at"].endswith("+08:00")
    report_file = task_root / task["task_id"] / "generation-report.json"
    saved_report = json.loads(report_file.read_text(encoding="utf-8"))
    assert saved_report["task_details"]["material_recovery_error"] == original_error
    assert store.recover_insufficient_visual_duration_failures() == []
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_FAILED,
        status=const.TASK_STATUS_FAILED,
        stage="pipeline",
        failed_stage="pipeline",
        error=original_error,
    )
    assert store.recover_insufficient_visual_duration_failures() == [task["task_id"]]
    assert len(store.get_task(task["task_id"])["scene_plan"]) == 7


def test_visual_duration_recovery_ignores_other_pipeline_failures(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_FAILED,
        status=const.TASK_STATUS_FAILED,
        stage="pipeline",
        failed_stage="pipeline",
        error="FFmpeg failed while combining videos",
        scene_plan=[{"scene_index": 0}],
    )

    assert store.recover_insufficient_visual_duration_failures() == []
    assert store.get_task(task["task_id"])["status"] == const.TASK_STATUS_FAILED


def test_single_scene_generation_reservation_blocks_duplicate_paid_request(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_PROCESSING,
        status=const.TASK_STATUS_AWAITING_MATERIAL,
        stage="awaiting_material_upload",
        scene_plan=[{"scene_index": 0, "material_prompt": "prompt"}],
        required_upload_scene_indexes=[0],
        missing_scenes=[{"scene_index": 0, "material_prompt": "prompt"}],
    )

    job = store.reserve_scene_generation(task["task_id"], 0)

    assert job["status"] == "queued"
    assert job["run_id"]
    with pytest.raises(TaskStoreError, match="already in progress"):
        store.reserve_scene_generation(task["task_id"], 0)


def test_generated_and_uploaded_scenes_can_be_confirmed_together(
    tmp_path, monkeypatch
):
    task_root = tmp_path / "tasks"
    monkeypatch.setattr(
        "app.services.task_store.utils.task_dir",
        lambda task_id="": str(task_root / task_id) if task_id else str(task_root),
    )
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    scenes = [{"scene_index": 0}, {"scene_index": 1}]
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_PROCESSING,
        status=const.TASK_STATUS_AWAITING_MATERIAL,
        stage="awaiting_material_upload",
        scene_plan=scenes,
        required_upload_scene_indexes=[0, 1],
        missing_scenes=scenes,
    )
    job = store.reserve_scene_generation(task["task_id"], 0)
    material_dir = task_root / task["task_id"] / "materials"
    material_dir.mkdir(parents=True)
    generated = material_dir / "scene-0000-seedance.mp4"
    generated.write_bytes(b"generated")
    assert store.complete_scene_generation(
        task["task_id"],
        0,
        job["run_id"],
        str(generated),
        provider_task_id="provider-0",
        details={"tokens": 100},
    )
    upload_dir = task_root / task["task_id"] / "material-uploads"
    upload_dir.mkdir(parents=True)
    uploaded = upload_dir / "scene-0001.mp4"
    uploaded.write_bytes(b"uploaded")

    attached, _ = store.attach_scene_material(task["task_id"], 1, str(uploaded))
    confirmed = store.confirm_scene_materials(task["task_id"])

    assert attached["missing_scenes"] == []
    assert confirmed["status"] == const.TASK_STATUS_QUEUED
    assert confirmed["generated_scene_paths"] == {"0": str(generated.resolve())}
    assert confirmed["uploaded_scene_paths"] == {"1": str(uploaded.resolve())}


def test_failed_single_scene_generation_stays_waiting_and_can_retry(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    scene = {"scene_index": 0, "material_prompt": "prompt"}
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_PROCESSING,
        status=const.TASK_STATUS_AWAITING_MATERIAL,
        stage="awaiting_material_upload",
        scene_plan=[scene],
        required_upload_scene_indexes=[0],
        missing_scenes=[scene],
        seedance_provider_task_ids={"0": "failed-provider"},
    )
    job = store.reserve_scene_generation(task["task_id"], 0)
    store.fail_scene_generation(
        task["task_id"],
        0,
        job["run_id"],
        "account has an overdue balance",
        details={
            "provider_task_id": "failed-provider",
            "provider_status": "failed",
        },
    )

    failed = store.get_task(task["task_id"])

    assert failed["status"] == const.TASK_STATUS_AWAITING_MATERIAL
    assert failed["scene_generation_jobs"]["0"]["status"] == "failed"
    assert failed["seedance_provider_task_ids"] == {}
    assert failed["seedance_failed_provider_task_ids"]["0"] == ["failed-provider"]
    retry = store.reserve_scene_generation(task["task_id"], 0)
    assert retry["run_id"] != job["run_id"]


def test_only_missing_scene_uploads_are_required_for_online_material_task(
    tmp_path, monkeypatch
):
    task_root = tmp_path / "tasks"
    monkeypatch.setattr(
        "app.services.task_store.utils.task_dir",
        lambda task_id="": str(task_root / task_id) if task_id else str(task_root),
    )
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_PROCESSING,
        status=const.TASK_STATUS_AWAITING_MATERIAL,
        stage="awaiting_material_upload",
        scene_plan=[
            {"scene_index": 0, "material_prompt": "matched"},
            {"scene_index": 1, "material_prompt": "missing"},
        ],
        required_upload_scene_indexes=[1],
        missing_scenes=[{"scene_index": 1, "material_prompt": "missing"}],
    )
    upload_dir = task_root / task["task_id"] / "material-uploads"
    upload_dir.mkdir(parents=True)
    uploaded = upload_dir / "missing.mp4"
    uploaded.write_bytes(b"video")

    with pytest.raises(TaskStoreError, match="does not require"):
        store.attach_scene_material(task["task_id"], 0, str(uploaded))

    attached, _ = store.attach_scene_material(task["task_id"], 1, str(uploaded))
    assert attached["missing_scenes"] == []
    confirmed = store.confirm_scene_materials(task["task_id"])
    assert confirmed["status"] == const.TASK_STATUS_QUEUED


def test_supplemental_scene_approval_is_explicit_and_editable(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_PROCESSING,
        status=const.TASK_STATUS_AWAITING_APPROVAL,
        stage="awaiting_supplemental_scenes",
        supplemental_scenes=[
            {"scene_index": 3, "video_prompt": "original", "target_duration": 2}
        ],
    )

    approved = store.approve_supplemental_scenes(
        task["task_id"], {"3": "edited closing shot"}
    )

    assert approved["status"] == const.TASK_STATUS_QUEUED
    assert approved["supplemental_scenes_approved"] is True
    assert approved["supplemental_scenes"][0]["video_prompt"] == "edited closing shot"


def test_material_actions_reject_illegal_task_state_without_mutation(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_COMPLETE,
        progress=100,
        status=const.TASK_STATUS_COMPLETED,
        stage="completed",
        videos=["final.mp4"],
    )

    with pytest.raises(TaskStoreError):
        store.reject_seedance(task["task_id"])
    with pytest.raises(TaskStoreError):
        store.add_replacement_segments(task["task_id"], {"0": "segment-1"})

    unchanged = store.get_task(task["task_id"])
    assert unchanged["status"] == const.TASK_STATUS_COMPLETED
    assert unchanged["videos"] == ["final.mp4"]


def test_material_actions_apply_only_expected_atomic_transitions(tmp_path):
    store = TaskStore(tmp_path / "app.db")
    task = store.enqueue(_params())
    store.claim_next("worker", max_concurrent_tasks=1)
    store.update_runtime_task(
        task["task_id"],
        state=const.TASK_STATE_PROCESSING,
        progress=45,
        status=const.TASK_STATUS_AWAITING_APPROVAL,
        stage="awaiting_approval",
        missing_scenes=[{"scene_index": 0}],
    )

    rejected = store.reject_seedance(task["task_id"])
    assert rejected["status"] == const.TASK_STATUS_AWAITING_MATERIAL

    replaced = store.add_replacement_segments(
        task["task_id"], {"0": "segment-1", "invalid": "ignored"}
    )
    assert replaced["status"] == const.TASK_STATUS_QUEUED
    assert replaced["replacement_segment_ids"] == {"0": "segment-1"}


def test_worker_pool_reconfigures_concurrency_without_restarting_active_workers(
    monkeypatch,
):
    class EmptyStore:
        def __init__(self):
            self.claim_limits = []

        def claim_next(self, _worker_id, *, max_concurrent_tasks, lease_seconds):
            self.claim_limits.append((max_concurrent_tasks, lease_seconds))
            return None

    monkeypatch.setitem(
        config.app,
        "max_concurrent_tasks",
        1,
    )
    store = EmptyStore()
    pool = TaskWorkerPool(store)
    try:
        pool.start()
        pool.reconfigure(max_workers=3, lease_seconds=90)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if sum(thread.is_alive() for thread in pool._threads.values()) == 3 and any(
                limit == (3, 90) for limit in store.claim_limits
            ):
                break
            time.sleep(0.01)
        assert sum(thread.is_alive() for thread in pool._threads.values()) == 3
        assert pool.max_workers == 3
        assert pool.lease_seconds == 90
        assert any(limit == (3, 90) for limit in store.claim_limits)

        pool.reconfigure(max_workers=1, lease_seconds=60)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if sum(thread.is_alive() for thread in pool._threads.values()) == 1:
                break
            time.sleep(0.01)
        assert sum(thread.is_alive() for thread in pool._threads.values()) == 1
        assert pool.max_workers == 1
        assert pool.lease_seconds == 60
    finally:
        pool.stop()
