from types import SimpleNamespace

from app.services import vision


def _chat_response(content='{"ok": true}'):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_extract_json_accepts_fenced_response():
    parsed = vision._extract_json(
        '```json\n{"description_zh":"街道","tags_zh":["城市"],"tags_en":["city"]}\n```'
    )

    assert parsed["tags_en"] == ["city"]


def test_normalize_tags_removes_duplicates_and_empty_values():
    assert vision._normalize_tags([" city ", "City", "", "street"]) == [
        "city",
        "street",
    ]


def test_configured_model_options_include_custom_config_models(monkeypatch):
    monkeypatch.setitem(
        vision.config.vision,
        "model_options",
        [vision.DEFAULT_MODEL_NAME, "custom-vision-model"],
    )

    model_ids = [option["id"] for option in vision.configured_model_options()]

    assert model_ids.count(vision.DEFAULT_MODEL_NAME) == 1
    assert "custom-vision-model" in model_ids


def test_connection_uses_default_ark_model_when_model_name_is_empty(monkeypatch):
    captured = {}

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            captured["chat_kwargs"] = kwargs
            return _chat_response()

    class FakeModels:
        @staticmethod
        def list():
            return SimpleNamespace(
                data=[SimpleNamespace(id=vision.DEFAULT_MODEL_NAME)]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.models = FakeModels()
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(vision, "OpenAI", FakeOpenAI)
    monkeypatch.setitem(vision.config.vision, "api_key", "vision-key")
    monkeypatch.setitem(vision.config.vision, "base_url", "")
    monkeypatch.setitem(vision.config.vision, "model_name", "")

    ok, error = vision.test_connection()

    assert ok is True
    assert error == ""
    assert captured["api_key"] == "vision-key"
    assert captured["base_url"] == vision.DEFAULT_BASE_URL
    assert captured["chat_kwargs"]["model"] == vision.DEFAULT_MODEL_NAME
    assert captured["chat_kwargs"]["messages"][0]["content"][1]["type"] == "image_url"


def test_connection_reuses_volcengine_api_key_when_vision_key_is_empty(monkeypatch):
    captured = {}

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            captured["chat_kwargs"] = kwargs
            return _chat_response()

    class FakeModels:
        @staticmethod
        def list():
            return SimpleNamespace(
                data=[SimpleNamespace(id=vision.DEFAULT_MODEL_NAME)]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.models = FakeModels()
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(vision, "OpenAI", FakeOpenAI)
    monkeypatch.setitem(vision.config.vision, "api_key", "")
    monkeypatch.setitem(vision.config.app, "volcengine_api_key", "volcengine-key")
    monkeypatch.setitem(vision.config.vision, "base_url", "")
    monkeypatch.setitem(vision.config.vision, "model_name", "")

    ok, error = vision.test_connection()

    assert ok is True
    assert error == ""
    assert captured["api_key"] == "volcengine-key"
    assert captured["chat_kwargs"]["model"] == vision.DEFAULT_MODEL_NAME


def test_connection_rejects_model_missing_from_account(monkeypatch):
    class FakeModels:
        @staticmethod
        def list():
            return SimpleNamespace(data=[SimpleNamespace(id="another-model")])

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    monkeypatch.setattr(vision, "OpenAI", FakeOpenAI)
    monkeypatch.setitem(vision.config.vision, "api_key", "vision-key")
    monkeypatch.setitem(vision.config.vision, "base_url", "")
    monkeypatch.setitem(vision.config.vision, "model_name", vision.DEFAULT_MODEL_NAME)

    ok, error = vision.test_connection()

    assert ok is False
    assert vision.DEFAULT_MODEL_NAME in error


def test_connection_rejects_model_that_lists_but_cannot_chat(monkeypatch):
    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            raise RuntimeError("InvalidEndpointOrModel.NotFound")

    class FakeModels:
        @staticmethod
        def list():
            return SimpleNamespace(
                data=[SimpleNamespace(id=vision.DEFAULT_MODEL_NAME)]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.models = FakeModels()
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(vision, "OpenAI", FakeOpenAI)
    monkeypatch.setitem(vision.config.vision, "api_key", "vision-key")
    monkeypatch.setitem(vision.config.vision, "base_url", "")
    monkeypatch.setitem(vision.config.vision, "model_name", vision.DEFAULT_MODEL_NAME)

    ok, error = vision.test_connection()

    assert ok is False
    assert "InvalidEndpointOrModel.NotFound" in error
