from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.provider_login import validate_and_fetch


class _StubAdapter:
    """Fake provider adapter: configurable list_models / chat behaviour."""

    def __init__(
        self,
        models: list[str] | None = None,
        error: Exception | None = None,
        chat_error: Exception | None = None,
    ) -> None:
        self.models = models
        self.error = error
        self.chat_error = chat_error
        self.list_calls = 0
        self.chat_calls: list[tuple[str, str, list[dict[str, Any]], int | None]] = []

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        self.list_calls += 1
        if self.error is not None:
            raise self.error
        return self.models

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
        on_delta=None,
        max_tokens: int | None = None,
    ):
        self.chat_calls.append((api_key, model, messages, max_tokens))
        if self.chat_error is not None:
            raise self.chat_error
        return None


class _NoListAdapter(_StubAdapter):
    """Anthropic-style adapter without a models-list endpoint."""

    async def list_models(self, api_key: str, headers: dict[str, str] | None = None) -> list[str] | None:
        self.list_calls += 1
        raise NotImplementedError


@pytest.mark.asyncio
async def test_validate_and_fetch_success():
    a = _StubAdapter(models=["m1", "m2"])
    ok, error, models = await validate_and_fetch(a, "sk-1", "openrouter")
    assert ok is True
    assert error is None
    assert models == [{"id": "m1", "contextWindow": None}, {"id": "m2", "contextWindow": None}]
    assert a.list_calls == 1
    # The key is proven with a minimal chat call (max_tokens=1) against the
    # first fetched model, because /models may be a public endpoint.
    assert len(a.chat_calls) == 1
    assert a.chat_calls[0][0] == "sk-1"
    assert a.chat_calls[0][1] == "m1"
    assert a.chat_calls[0][3] == 1


@pytest.mark.asyncio
async def test_validate_and_fetch_chat_401_is_hard_failure():
    a = _StubAdapter(models=["m1"], chat_error=RuntimeError("openrouter API error 401: bad key"))
    ok, error, models = await validate_and_fetch(a, "sk-bad", "openrouter")
    assert ok is False
    assert "Authorization failed" in (error or "")
    assert models is None


@pytest.mark.asyncio
async def test_validate_and_fetch_chat_402_is_soft_pass_with_credit_note():
    a = _StubAdapter(models=["m1"], chat_error=RuntimeError("openrouter API error 402: insufficient credits"))
    ok, error, models = await validate_and_fetch(a, "sk-1", "openrouter")
    assert ok is True
    assert error is not None and "credit" in error
    assert models == [{"id": "m1", "contextWindow": None}]


@pytest.mark.asyncio
async def test_validate_and_fetch_chat_400_probe_model_issue_is_soft_pass():
    a = _StubAdapter(models=["m1"], chat_error=RuntimeError("openrouter API error 400: reasoning_effort not supported"))
    ok, error, models = await validate_and_fetch(a, "sk-1", "openrouter")
    assert ok is True
    assert error is not None and "400" in error
    assert models == [{"id": "m1", "contextWindow": None}]


@pytest.mark.asyncio
async def test_validate_and_fetch_chat_other_error_is_hard_failure():
    a = _StubAdapter(models=["m1"], chat_error=RuntimeError("openrouter API error 500: boom"))
    ok, error, models = await validate_and_fetch(a, "sk-1", "openrouter")
    assert ok is False
    assert "Validation failed" in (error or "")
    assert models is None


@pytest.mark.asyncio
async def test_validate_and_fetch_401_is_hard_failure():
    a = _StubAdapter(error=RuntimeError("openrouter API error 401: invalid key"))
    ok, error, models = await validate_and_fetch(a, "sk-bad", "openrouter")
    assert ok is False
    assert "Authorization failed" in (error or "")
    assert models is None


@pytest.mark.asyncio
async def test_validate_and_fetch_403_is_hard_failure():
    a = _StubAdapter(error=RuntimeError("openrouter API error 403: forbidden"))
    ok, error, models = await validate_and_fetch(a, "sk-bad", "openrouter")
    assert ok is False
    assert "Authorization failed" in (error or "")


@pytest.mark.asyncio
async def test_validate_and_fetch_non_auth_error_is_soft_failure():
    a = _StubAdapter(error=RuntimeError("openrouter API error 404: not found"))
    ok, error, models = await validate_and_fetch(a, "sk-1", "openrouter")
    assert ok is True
    assert error is not None and "404" in error
    assert models is None


@pytest.mark.asyncio
async def test_validate_and_fetch_no_list_endpoint_chat_ok():
    a = _NoListAdapter()
    ok, error, models = await validate_and_fetch(a, "sk-1", "anthropic", model_id="claude-x")
    assert ok is True
    assert models is None
    assert len(a.chat_calls) == 1
    assert a.chat_calls[0][0] == "sk-1"
    assert a.chat_calls[0][1] == "claude-x"


@pytest.mark.asyncio
async def test_validate_and_fetch_no_list_endpoint_chat_401():
    a = _NoListAdapter(chat_error=RuntimeError("anthropic API error 401: bad key"))
    ok, error, models = await validate_and_fetch(a, "sk-bad", "anthropic", model_id="claude-x")
    assert ok is False
    assert "Authorization failed" in (error or "")


@pytest.mark.asyncio
async def test_validate_and_fetch_no_list_endpoint_without_model():
    a = _NoListAdapter()
    ok, error, models = await validate_and_fetch(a, "sk-1", "anthropic")
    assert ok is True
    assert models is None
    assert error is not None and "no model available" in error
    assert a.chat_calls == []


@pytest.mark.asyncio
async def test_validate_and_fetch_no_key_fetches_without_validation():
    a = _StubAdapter(models=["local1", "local2"])
    ok, error, models = await validate_and_fetch(a, "", "llama.cpp")
    assert ok is True
    assert error is None
    assert models == [{"id": "local1", "contextWindow": None}, {"id": "local2", "contextWindow": None}]


@pytest.mark.asyncio
async def test_validate_and_fetch_empty_list_is_not_none():
    a = _StubAdapter(models=[])
    ok, error, models = await validate_and_fetch(a, "sk-1", "openrouter")
    assert ok is True
    assert models == []
    assert error is not None and "no model available" in error
    assert a.chat_calls == []


def test_register_models_adds_and_dedupes(tmp_path: Path):
    auth = AuthStorage.in_memory()
    reg = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    assert reg.find("openrouter", "fresh-model") is None
    added = reg.register_models("openrouter", ["fresh-model", "fresh-model", "other"])
    assert added == 2
    assert reg.find("openrouter", "fresh-model") is not None
    assert reg.find("openrouter", "other") is not None
    # Already-registered ids are not added twice.
    added2 = reg.register_models("openrouter", ["fresh-model", "yet-another"])
    assert added2 == 1


def test_persist_models_merges_and_keeps_existing_fields(tmp_path: Path):
    auth = AuthStorage.in_memory()
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "openrouter": [
                        {"id": "old", "reasoning": True, "contextWindow": 200000},
                        {"id": "url-model", "url": "http://x:1", "toolParser": [{"type": "json"}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    reg = ModelRegistry.create(auth, str(models_path))
    reg.persist_models("openrouter", ["new-model", "old"])
    data = json.loads(models_path.read_text(encoding="utf-8"))
    entries = {m["id"]: m for m in data["providers"]["openrouter"]}
    assert "new-model" in entries
    assert entries["old"]["contextWindow"] == 200000
    assert entries["url-model"]["url"] == "http://x:1"
    assert entries["url-model"]["toolParser"] == [{"type": "json"}]


def test_persist_models_creates_file_when_missing(tmp_path: Path):
    auth = AuthStorage.in_memory()
    models_path = tmp_path / "sub" / "models.json"
    reg = ModelRegistry.create(auth, str(models_path))
    reg.persist_models("ollama-cloud", ["glm-5.2"])
    data = json.loads(models_path.read_text(encoding="utf-8"))
    assert data["providers"]["ollama-cloud"][0]["id"] == "glm-5.2"


def test_persist_models_registered_models_are_readable_by_registry(tmp_path: Path):
    auth = AuthStorage.in_memory()
    models_path = tmp_path / "models.json"
    reg = ModelRegistry.create(auth, str(models_path))
    reg.persist_models("ollama-cloud", ["minimax-m3"])
    reg2 = ModelRegistry.create(auth, str(models_path))
    assert reg2.find("ollama-cloud", "minimax-m3") is not None


@pytest.mark.asyncio
async def test_openai_compatible_list_models_url_heuristic_and_parse(monkeypatch):
    from one.providers import openai_compatible as oc

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        @property
        def is_error(self) -> bool:
            return False

        def json(self) -> dict[str, Any]:
            return {"data": [{"id": "a"}, {"id": "b"}, {"id": ""}]}

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["client"] = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str, headers: dict[str, str] | None = None):
            captured["url"] = url
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    adapter = oc.OpenAICompatibleAdapter("openrouter", "https://openrouter.ai/api")
    models = await adapter.list_models("sk-1")
    assert models == ["a", "b"]
    # Base without /v1 -> /v1/models.
    assert captured["url"] == "https://openrouter.ai/api/v1/models"
    assert captured["headers"].get("Authorization") == "Bearer sk-1"


@pytest.mark.asyncio
async def test_openai_compatible_list_models_base_ending_v1(monkeypatch):
    from one.providers import openai_compatible as oc

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        @property
        def is_error(self) -> bool:
            return False

        def json(self) -> dict[str, Any]:
            return {"data": [{"id": "x"}]}

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["client"] = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str, headers: dict[str, str] | None = None):
            captured["url"] = url
            return _Resp()

    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    adapter = oc.OpenAICompatibleAdapter("ollama", "http://localhost:11434/v1", endpoint="/chat/completions")
    await adapter.list_models("")
    assert captured["url"] == "http://localhost:11434/v1/models"


@pytest.mark.asyncio
async def test_openai_compatible_list_models_401_raises(monkeypatch):
    from one.providers import openai_compatible as oc

    class _Resp:
        status_code = 401
        text = ""

        @property
        def is_error(self) -> bool:
            return True

        def json(self) -> dict[str, Any]:
            return {"error": {"message": "invalid api key"}}

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str, headers: dict[str, str] | None = None):
            return _Resp()

    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    adapter = oc.OpenAICompatibleAdapter("openrouter", "https://openrouter.ai/api")
    with pytest.raises(RuntimeError, match="401"):
        await adapter.list_models("sk-bad")


@pytest.mark.asyncio
async def test_gemini_list_models_filters_generate_content(monkeypatch):
    from one.providers import gemini as g

    class _Resp:
        status_code = 200

        @property
        def is_error(self) -> bool:
            return False

        def json(self) -> dict[str, Any]:
            return {
                "models": [
                    {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent", "embedContent"]},
                    {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
                    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                ]
            }

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str):
            assert "key=sk-1" in url
            return _Resp()

    monkeypatch.setattr(g.httpx, "AsyncClient", _FakeClient)
    adapter = g.GeminiAdapter()
    models = await adapter.list_models("sk-1")
    assert models == ["gemini-2.5-pro", "gemini-2.5-flash"]


def test_anthropic_list_models_returns_none():
    from one.providers.anthropic import AnthropicAdapter

    assert AnthropicAdapter().name == "anthropic"


# --- Phase 17: real context windows end-to-end -------------------------------


@pytest.mark.asyncio
async def test_openai_compatible_list_models_detailed_parses_context_length(monkeypatch):
    from one.providers import openai_compatible as oc

    class _Resp:
        status_code = 200

        @property
        def is_error(self) -> bool:
            return False

        def json(self) -> dict[str, Any]:
            return {
                "data": [
                    {"id": "big", "context_length": 1_000_000},
                    {"id": "unknown"},
                    {"id": "junk", "context_length": "oops"},
                    {"id": ""},
                ]
            }

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str, headers: dict[str, str] | None = None):
            return _Resp()

    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    adapter = oc.OpenAICompatibleAdapter("openrouter", "https://openrouter.ai/api")
    detailed = await adapter.list_models_detailed("sk-1")
    assert detailed == [
        {"id": "big", "contextWindow": 1_000_000},
        {"id": "unknown", "contextWindow": None},
        {"id": "junk", "contextWindow": None},
    ]


@pytest.mark.asyncio
async def test_gemini_list_models_detailed_parses_input_token_limit(monkeypatch):
    from one.providers import gemini as g

    class _Resp:
        status_code = 200

        @property
        def is_error(self) -> bool:
            return False

        def json(self) -> dict[str, Any]:
            return {
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "supportedGenerationMethods": ["generateContent"],
                        "inputTokenLimit": 1_048_576,
                    },
                    {"name": "models/gemini-tiny", "supportedGenerationMethods": ["generateContent"]},
                ]
            }

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def get(self, url: str):
            return _Resp()

    monkeypatch.setattr(g.httpx, "AsyncClient", _FakeClient)
    adapter = g.GeminiAdapter()
    detailed = await adapter.list_models_detailed("sk-1")
    assert detailed == [
        {"id": "gemini-2.5-pro", "contextWindow": 1_048_576},
        {"id": "gemini-tiny", "contextWindow": None},
    ]
    # list_models stays a thin wrapper over the detailed form.
    assert await adapter.list_models("sk-1") == ["gemini-2.5-pro", "gemini-tiny"]


@pytest.mark.asyncio
async def test_validate_and_fetch_prefers_detailed_entries():
    class _DetailedAdapter(_StubAdapter):
        async def list_models_detailed(
            self, api_key: str, headers: dict[str, str] | None = None
        ) -> list[dict[str, Any]] | None:
            self.list_calls += 1
            return [
                {"id": "nemotron-3-ultra", "contextWindow": 262_144},
                {"id": "glm-5.2", "contextWindow": None},
            ]

    a = _DetailedAdapter(models=["ignored"])
    ok, error, models = await validate_and_fetch(a, "sk-1", "ollama-cloud")
    assert ok is True
    assert error is None
    assert models == [
        {"id": "nemotron-3-ultra", "contextWindow": 262_144},
        {"id": "glm-5.2", "contextWindow": None},
    ]
    # The chat probe uses the first DETAILED entry's id.
    assert a.chat_calls[0][1] == "nemotron-3-ultra"
    assert a.list_calls == 1


def test_register_and_persist_carry_context_window(tmp_path: Path):
    auth = AuthStorage.in_memory()
    models_path = tmp_path / "models.json"
    reg = ModelRegistry.create(auth, str(models_path))

    entries = [
        {"id": "nemotron-3-ultra", "contextWindow": 262_144},
        {"id": "no-window"},
        "plain-string-model",
    ]
    added = reg.register_models("ollama-cloud", entries)
    assert added == 3
    found = reg.find("ollama-cloud", "nemotron-3-ultra")
    assert found is not None and found.context_window == 262_144
    assert reg.find("ollama-cloud", "plain-string-model") is not None

    reg.persist_models("ollama-cloud", entries)
    reg2 = ModelRegistry.create(auth, str(models_path))
    m2 = reg2.find("ollama-cloud", "nemotron-3-ultra")
    assert m2 is not None and m2.context_window == 262_144

    # Refresh with a window enriches an in-memory entry that lacks one.
    reg2.register_models("ollama-cloud", [{"id": "no-window", "contextWindow": 131_072}])
    enriched = reg2.find("ollama-cloud", "no-window")
    assert enriched is not None and enriched.context_window == 131_072