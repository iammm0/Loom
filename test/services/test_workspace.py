from types import SimpleNamespace
from unittest.mock import patch

from app.controllers.v1 import workspace
from app.models.schema import SettingsUpdateRequest


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
    assert response["data"]["voice_groups"]
    assert response["data"]["seedance_models"]
    assert any(item["id"] for item in response["data"]["seedance_models"])
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
