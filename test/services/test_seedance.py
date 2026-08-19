from pathlib import Path
from unittest.mock import MagicMock

from app.services import seedance


def test_create_generation_posts_once_and_returns_task_id(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "provider-task"}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(seedance.requests, "post", post)
    monkeypatch.setitem(seedance.config.seedance, "api_key", "key")
    monkeypatch.setitem(seedance.config.seedance, "model_id", "model")
    monkeypatch.setitem(seedance.config.seedance, "base_url", "https://ark.example/v3")

    task_id = seedance.create_generation_task(
        prompt="city", aspect_ratio="9:16", duration=3
    )

    assert task_id == "provider-task"
    assert post.call_count == 1
    assert post.call_args.kwargs["json"]["resolution"] == "720p"


def test_create_generation_normalizes_seedance_2_short_duration(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "provider-task"}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(seedance.requests, "post", post)
    monkeypatch.setitem(seedance.config.seedance, "api_key", "key")
    monkeypatch.setitem(seedance.config.seedance, "model_id", "doubao-seedance-2-0")
    monkeypatch.setitem(seedance.config.seedance, "base_url", "https://ark.example/v3")

    seedance.create_generation_task(prompt="city", aspect_ratio="9:16", duration=3)

    assert post.call_args.kwargs["json"]["duration"] == 4


def test_create_generation_caps_seedance_1_5_duration(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "provider-task"}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(seedance.requests, "post", post)
    monkeypatch.setitem(seedance.config.seedance, "api_key", "key")
    monkeypatch.setitem(
        seedance.config.seedance, "model_id", "doubao-seedance-1-5-pro-251215"
    )
    monkeypatch.setitem(seedance.config.seedance, "base_url", "https://ark.example/v3")

    seedance.create_generation_task(prompt="city", aspect_ratio="9:16", duration=20)

    assert post.call_args.kwargs["json"]["duration"] == 12


def test_create_generation_uses_default_model_when_model_id_is_empty(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "provider-task"}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(seedance.requests, "post", post)
    monkeypatch.setitem(seedance.config.seedance, "api_key", "key")
    monkeypatch.setitem(seedance.config.seedance, "model_id", "")
    monkeypatch.setitem(seedance.config.seedance, "base_url", "https://ark.example/v3")

    seedance.create_generation_task(prompt="city", aspect_ratio="9:16", duration=3)

    assert post.call_args.kwargs["json"]["model"] == seedance.DEFAULT_MODEL_ID


def test_configured_model_options_ignore_unverified_config_models(monkeypatch):
    monkeypatch.setitem(
        seedance.config.seedance,
        "model_options",
        [seedance.DEFAULT_MODEL_ID, "custom-video-model"],
    )

    model_ids = [option["id"] for option in seedance.configured_model_options()]

    assert model_ids == [seedance.DEFAULT_MODEL_ID]
    assert "custom-video-model" not in model_ids


def test_connection_uses_models_endpoint_and_checks_selected_model(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": [{"id": seedance.DEFAULT_MODEL_ID}]}
    get = MagicMock(return_value=response)
    monkeypatch.setattr(seedance.requests, "get", get)
    monkeypatch.setitem(seedance.config.seedance, "api_key", "key")
    monkeypatch.setitem(seedance.config.seedance, "model_id", seedance.DEFAULT_MODEL_ID)
    monkeypatch.setitem(seedance.config.seedance, "base_url", "https://ark.example/v3")

    ok, error = seedance.test_connection()

    assert ok is True
    assert error == ""
    assert get.call_args.args[0] == "https://ark.example/v3/models"


def test_connection_reuses_volcengine_api_key_when_seedance_key_is_empty(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": [{"id": seedance.DEFAULT_MODEL_ID}]}
    get = MagicMock(return_value=response)
    monkeypatch.setattr(seedance.requests, "get", get)
    monkeypatch.setitem(seedance.config.seedance, "api_key", "")
    monkeypatch.setitem(seedance.config.app, "volcengine_api_key", "volcengine-key")
    monkeypatch.setitem(seedance.config.seedance, "model_id", seedance.DEFAULT_MODEL_ID)
    monkeypatch.setitem(seedance.config.seedance, "base_url", "https://ark.example/v3")

    ok, error = seedance.test_connection()

    assert ok is True
    assert error == ""
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer volcengine-key"


def test_connection_rejects_model_missing_from_account(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": [{"id": "another-model"}]}
    monkeypatch.setattr(seedance.requests, "get", MagicMock(return_value=response))
    monkeypatch.setitem(seedance.config.seedance, "api_key", "key")
    monkeypatch.setitem(seedance.config.seedance, "model_id", seedance.DEFAULT_MODEL_ID)
    monkeypatch.setitem(seedance.config.seedance, "base_url", "https://ark.example/v3")

    ok, error = seedance.test_connection()

    assert ok is False
    assert seedance.DEFAULT_MODEL_ID in error


def test_wait_for_generation_extracts_nested_result_url(monkeypatch):
    monkeypatch.setattr(
        seedance,
        "get_generation_task",
        lambda _: {
            "status": "succeeded",
            "content": {"video_url": "https://cdn/video.mp4"},
        },
    )

    assert seedance.wait_for_generation("task") == "https://cdn/video.mp4"


def test_wait_for_generation_preserves_confirmed_failure_status(monkeypatch):
    monkeypatch.setattr(
        seedance,
        "get_generation_task",
        lambda _: {"status": "failed", "error": "content rejected"},
    )

    try:
        seedance.wait_for_generation_details("task")
    except seedance.SeedanceError as exc:
        assert exc.details["provider_status"] == "failed"
        assert exc.details["provider_payload"]["error"] == "content rejected"
    else:
        raise AssertionError("SeedanceError was not raised")


def test_download_result_uses_atomic_part_file(tmp_path, monkeypatch):
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.iter_content.return_value = [b"abc", b"def"]
    monkeypatch.setattr(seedance.requests, "get", lambda *args, **kwargs: response)
    output = tmp_path / "video.mp4"

    seedance.download_result("https://cdn/video.mp4", str(output))

    assert output.read_bytes() == b"abcdef"
    assert not Path(f"{output}.part").exists()


def test_billing_prefers_api_completion_tokens():
    result = seedance.billing_details(
        model_id="doubao-seedance-2-0-260128",
        resolution="720p",
        provider_duration=5,
        provider_payload={
            "status": "succeeded",
            "usage": {"completion_tokens": 108900, "total_tokens": 108900},
        },
        media_metadata={
            "width": 720,
            "height": 1280,
            "frame_rate": 24,
        },
    )

    assert result["tokens"] == 108900
    assert result["calculation_method"] == "api_usage"
    assert result["unit_price_cny_per_million_tokens"] == 46
    assert result["estimated_cost_cny"] == 5.0094
    assert result["is_estimate"] is False


def test_billing_uses_official_formula_when_api_usage_is_missing():
    result = seedance.billing_details(
        model_id="doubao-seedance-2-0-fast-260128",
        resolution="720p",
        provider_duration=5,
        provider_payload={"status": "succeeded"},
        media_metadata={
            "width": 720,
            "height": 1280,
            "frame_rate": 24,
        },
    )

    assert result["tokens"] == 108000
    assert result["calculation_method"] == "official_formula_estimate"
    assert result["unit_price_cny_per_million_tokens"] == 37
    assert result["estimated_cost_cny"] == 3.996
    assert result["is_estimate"] is True


def test_generate_clip_detailed_reuses_provider_task_without_new_post(
    tmp_path, monkeypatch
):
    output = tmp_path / "scene.mp4"
    downloaded = Path(f"{output}.download.mp4")
    create = MagicMock()
    monkeypatch.setattr(seedance, "create_generation_task", create)
    monkeypatch.setattr(
        seedance,
        "wait_for_generation_details",
        lambda _: (
            "https://cdn/video.mp4",
            {"status": "succeeded", "usage": {"completion_tokens": 108900}},
            2,
        ),
    )
    monkeypatch.setattr(
        seedance,
        "download_result",
        lambda _url, path: Path(path).write_bytes(b"video") or path,
    )
    monkeypatch.setattr(
        seedance,
        "_media_metadata",
        lambda _: {"width": 720, "height": 1280, "frame_rate": 24},
    )
    monkeypatch.setattr(
        seedance,
        "subprocess_strip_audio",
        lambda _source, path, _duration: Path(path).write_bytes(b"final") or path,
    )

    result, provider_id, details = seedance.generate_clip_detailed(
        prompt="city",
        aspect_ratio="9:16",
        duration=5,
        output_path=str(output),
        provider_task_id="existing-provider-task",
    )

    assert result == len(b"final")
    assert provider_id == "existing-provider-task"
    assert details["reused_provider_task"] is True
    assert details["tokens"] == 108900
    assert not downloaded.exists()
    create.assert_not_called()
