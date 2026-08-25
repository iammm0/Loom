import os

from fastapi import Request

from app.config import config
from app.config.config import (
    azure,
    chatterbox,
    elevenlabs,
    save_config,
    seedance,
    siliconflow,
    ui,
)
from app.controllers.v1.base import new_router
from app.models.llm_provider import LLM_PROVIDER_REGISTRY
from app.models.schema import SettingsUpdateRequest
from app.services import billing, seedance as seedance_service, task_store, voice
from app.utils import utils

router = new_router()

PROVIDER_LABELS = {
    "moonshot": "Kimi",
    "openai": "OpenAI",
    "gemini": "Gemini",
    "deepseek": "DeepSeek",
    "qwen": "通义千问",
    "azure": "Azure OpenAI",
    "volcengine": "火山方舟",
    "grok": "Grok",
    "minimax": "MiniMax",
    "mimo": "小米 MiMo",
    "cloudflare": "Cloudflare",
    "modelscope": "魔搭",
    "aihubmix": "AIHubMix",
    "aimlapi": "AIML API",
    "evolink": "EvoLink",
    "ollama": "Ollama",
    "oneapi": "OneAPI",
    "litellm": "LiteLLM",
    "groq": "Groq",
    "pollinations": "Pollinations",
}


def _settings_payload() -> dict:
    return {
        "app": dict(config.app),
        "seedance": dict(seedance),
        "azure": dict(azure),
        "siliconflow": dict(siliconflow),
        "elevenlabs": dict(elevenlabs),
        "chatterbox": dict(chatterbox),
        "ui": dict(ui),
        "listen_host": config.listen_host,
        "listen_port": config.listen_port,
    }


def _list_fonts() -> list[str]:
    fonts_dir = utils.font_dir()
    try:
        names = [
            name
            for name in os.listdir(fonts_dir)
            if name.lower().endswith((".ttf", ".ttc", ".otf"))
        ]
    except OSError:
        return []
    return sorted(names)


def _voice_groups() -> list[dict]:
    azure_voices = voice.get_all_azure_voices()
    edge = [item for item in azure_voices if "-V2" not in item]
    azure_v2 = [item for item in azure_voices if "-V2" in item]
    return [
        {"id": "mimo", "label": "小米 MiMo", "voices": voice.get_mimo_voices()},
        {"id": "edge", "label": "Edge TTS", "voices": edge},
        {"id": "azure", "label": "Azure 语音", "voices": azure_v2},
        {"id": "siliconflow", "label": "硅基流动", "voices": voice.get_siliconflow_voices()},
        {"id": "gemini", "label": "Gemini", "voices": voice.get_gemini_voices()},
        {
            "id": "elevenlabs",
            "label": "ElevenLabs",
            "voices": voice.get_elevenlabs_voices(str(elevenlabs.get("api_key") or "")),
        },
        {"id": "chatterbox", "label": "Chatterbox", "voices": voice.get_chatterbox_voices()},
        {"id": "none", "label": "无配音", "voices": [voice.NO_VOICE_NAME]},
    ]


def _provider_options() -> list[dict]:
    options = []
    for provider in LLM_PROVIDER_REGISTRY:
        options.append(
            {
                "id": provider.provider_id,
                "label": PROVIDER_LABELS.get(provider.provider_id, provider.default_label),
                "api_key_url": provider.api_key_url,
                "show_api_key": provider.show_api_key,
                "show_base_url": provider.show_base_url,
                "requires_api_key": provider.requires_api_key,
                "default_model": provider.default_model,
                "default_base_url": provider.default_base_url,
                "extra_fields": [
                    {
                        "suffix": field.config_suffix,
                        "label": {
                            "account_id": "账户 ID",
                            "gateway_id": "网关 ID",
                        }.get(field.config_suffix, field.label_key),
                        "required": field.required,
                        "secret": field.secret,
                    }
                    for field in provider.extra_fields
                ],
            }
        )
    return options


@router.get("/settings", summary="读取工作台配置")
def get_settings(_request: Request):
    return utils.get_response(200, _settings_payload())


@router.put("/settings", summary="保存工作台配置")
def update_settings(_request: Request, body: SettingsUpdateRequest):
    if body.app:
        config.app.update(body.app)
    if body.seedance:
        seedance.update(body.seedance)
    if body.azure:
        azure.update(body.azure)
    if body.siliconflow:
        siliconflow.update(body.siliconflow)
    if body.elevenlabs:
        elevenlabs.update(body.elevenlabs)
    if body.chatterbox:
        chatterbox.update(body.chatterbox)
    if body.ui:
        ui.update(body.ui)
    save_config()
    return utils.get_response(200, _settings_payload())


@router.get("/workspace/options", summary="读取工作台表单选项")
def get_workspace_options(_request: Request):
    return utils.get_response(
        200,
        {
            "providers": _provider_options(),
            "voice_groups": _voice_groups(),
            "fonts": _list_fonts(),
            "seedance_models": [
                {"id": item["id"], "label": item["label"]}
                for item in seedance_service.configured_model_options()
            ],
            "upload_post_platforms": [
                {"id": "tiktok", "label": "TikTok", "hint": "短视频"},
                {"id": "instagram", "label": "Instagram", "hint": "Reels"},
                {"id": "youtube", "label": "YouTube", "hint": "Shorts"},
            ],
            "upload_post_youtube_privacy": [
                {"id": "public", "label": "公开"},
                {"id": "unlisted", "label": "不列出"},
                {"id": "private", "label": "私密"},
            ],
        },
    )


@router.get("/billing/seedance", summary="查询 Seedance 账单")
def get_seedance_billing(_request: Request):
    tasks = billing.load_all_tasks(task_store.get_task_store())
    payload = billing.collect_seedance_billing(tasks)
    payload["display_rows"] = billing.display_rows(payload.get("rows") or [])
    return utils.get_response(200, payload)
