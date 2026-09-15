from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.controllers.v1 import workspace
from app.models.schema import LLMModelsProbeRequest, SettingsUpdateRequest


def test_get_settings_returns_workspace_sections():
    response = workspace.get_settings(SimpleNamespace())
    assert response["status"] == 200
    assert "app" in response["data"]
    assert "seedance" in response["data"]
    assert "azure" in response["data"]


def test_workspace_options_include_providers_and_voices():
    response = workspace.get_workspace_options(SimpleNamespace())
    assert response["status"] == 200
    assert response["data"]["providers"]
    assert any(item["id"] == "moonshot" for item in response["data"]["providers"])
    assert [item["id"] for item in response["data"]["voice_groups"]] == ["mimo"]
    assert response["data"]["voice_groups"][0]["voices"]
    assert response["data"]["seedance_models"]
    assert any(item["id"] for item in response["data"]["seedance_models"])
    assert "seedance_enabled" in response["data"]
    assert response["data"]["video_sources"]
    fonts = response["data"]["fonts"]
    assert isinstance(fonts, list)
    if fonts:
        assert any(name.lower().endswith((".ttf", ".ttc", ".otf")) for name in fonts)
    assert [item["id"] for item in response["data"]["upload_post_platforms"]] == [
        "tiktok",
        "instagram",
        "youtube",
    ]
    assert [item["id"] for item in response["data"]["upload_post_youtube_privacy"]] == [
        "public",
        "unlisted",
        "private",
    ]


def test_update_settings_persists_app_and_seedance():
    with patch.object(workspace, "save_config") as save:
        response = workspace.update_settings(
            SimpleNamespace(),
            SettingsUpdateRequest(),
        )

    save.assert_called_once_with()
    assert response["status"] == 200
    assert "app" in response["data"]


def test_probe_llm_models_returns_discovered_ids():
    with patch.object(
        workspace.llm,
        "list_available_models",
        return_value={"models": [{"id": "kimi-k2.7-code", "label": "kimi-k2.7-code"}], "error": ""},
    ) as probe:
        response = workspace.probe_llm_models(
            SimpleNamespace(),
            LLMModelsProbeRequest(provider="moonshot", api_key="key"),
        )

    probe.assert_called_once()
    assert response["status"] == 200
    assert response["data"]["models"][0]["id"] == "kimi-k2.7-code"
