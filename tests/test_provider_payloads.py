from __future__ import annotations

import struct
from typing import Any

import pytest

from one.providers.anthropic import AnthropicAdapter
from one.providers.codex_responses import CodexResponsesAdapter
from one.providers.gemini import GeminiAdapter
from one.providers.ollama import OllamaCloudAdapter
from one.providers.openai_compatible import OpenAICompatibleAdapter
from one.providers.registry import build_provider_registry

# ---------------------------------------------------------------------------
# Test data helpers (shared)
# ---------------------------------------------------------------------------


def _make_png() -> bytes:
    """Minimal valid PNG (1×1 transparent pixel)."""
    sig = b"\x89PNG\r\n\x1a\n"
    width = struct.pack(">I", 1)
    height = struct.pack(">I", 1)
    bit_depth_color = b"\x08\x06"
    ihdr_data = width + height + bit_depth_color + b"\x00\x00"
    ihdr_crc = b"\x00\x00\x00\x00"
    ihdr = b"\x00\x00\x00\x0dIHDR" + ihdr_data + ihdr_crc
    idat = b"\x00\x00\x00\x03IDAT\x08\x99c\xfc\xcf\x00\x00\x00\x02\x00\x01"
    idat_crc = b"\x00\x00\x00\x00"
    idat += idat_crc
    iend = b"\x00\x00\x00\x00IEND" + b"\xaeB`\x82"
    return sig + ihdr + idat + iend


def test_openai_compatible_payload_with_reasoning() -> None:
    adapter = OpenAICompatibleAdapter("openai", "https://api.openai.com")
    payload = adapter._build_payload(
        model="gpt-4.1",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="high",
    )
    assert payload["model"] == "gpt-4.1"
    assert payload["messages"][0]["content"] == "hi"
    assert payload["temperature"] == 0.1
    assert payload["reasoning_effort"] == "high"


def test_openai_compatible_payload_without_reasoning() -> None:
    adapter = OpenAICompatibleAdapter(
        "ollama-cloud",
        "https://ollama.com",
        supports_reasoning_effort=False,
        default_temperature=None,
    )
    payload = adapter._build_payload(
        model="glm-5:cloud",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="xhigh",
    )
    assert payload["model"] == "glm-5:cloud"
    assert payload["messages"][0]["content"] == "hi"
    assert "reasoning_effort" not in payload
    assert "temperature" not in payload


@pytest.mark.parametrize(
    ("level", "effort"),
    [("off", None), ("minimal", "minimal"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "high")],
)
def test_openai_compatible_reasoning_effort_maps_every_level(level: str, effort: str | None) -> None:
    payload = OpenAICompatibleAdapter("openai", "https://api.openai.com")._build_payload(
        "gpt", [{"role": "user", "content": "hi"}], level
    )
    assert payload.get("reasoning_effort") == effort


def test_openai_compatible_headers_without_api_key() -> None:
    adapter = OpenAICompatibleAdapter("llama.cpp", "http://127.0.0.1:8080")
    headers = adapter._build_headers("")
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_provider_registry_includes_llama_cpp() -> None:
    registry = build_provider_registry()
    assert "llama.cpp" in registry


def test_provider_registry_llama_cpp_uses_env_base_url(monkeypatch) -> None:
    monkeypatch.setenv("LLAMA_CPP_BASE_URL", "http://192.0.2.38:8089")
    registry = build_provider_registry()
    provider = registry["llama.cpp"]
    assert isinstance(provider, OpenAICompatibleAdapter)
    assert provider.base_url == "http://192.0.2.38:8089"


def test_openai_compatible_with_base_url_returns_reconfigured_adapter() -> None:
    adapter = OpenAICompatibleAdapter(
        "llama.cpp",
        "http://127.0.0.1:8080",
        supports_reasoning_effort=False,
        default_temperature=None,
    )
    changed = adapter.with_base_url("http://192.0.2.38:8089")
    assert changed.base_url == "http://192.0.2.38:8089"
    assert changed.supports_reasoning_effort is False
    assert changed.default_temperature is None


@pytest.mark.parametrize(
    ("level", "effort"),
    [("off", None), ("minimal", "minimal"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "high")],
)
def test_openrouter_uses_native_reasoning_object(level: str, effort: str | None) -> None:
    payload = OpenAICompatibleAdapter(
        "openrouter", "https://openrouter.ai/api", reasoning_mode="openrouter"
    )._build_payload("provider/model", [{"role": "user", "content": "hi"}], level)
    assert payload.get("reasoning") == ({"effort": effort} if effort else None)
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize(
    ("level", "think"),
    [("off", False), ("minimal", "low"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "high")],
)
def test_ollama_cloud_uses_native_think(level: str, think: bool | str) -> None:
    payload = OllamaCloudAdapter("https://ollama.com")._build_payload(
        "glm-5:cloud", [{"role": "user", "content": "hi"}], level
    )
    assert payload["think"] == think
    assert "reasoning_effort" not in payload
    assert "reasoning" not in payload


def test_registry_keeps_llama_cpp_compatible_and_uses_native_cloud_adapter() -> None:
    registry = build_provider_registry()
    assert type(registry["ollama-cloud"]).__name__ == "OllamaCloudAdapter"
    assert getattr(registry["ollama-cloud"], "endpoint") == "/api/chat"
    assert isinstance(registry["llama.cpp"], OpenAICompatibleAdapter)
    assert registry["llama.cpp"].reasoning_mode == "openai"


def test_openai_compatible_payload_without_images_works() -> None:
    """When no images are present, payload is built normally (phase-free behavior)."""
    adapter = OpenAICompatibleAdapter("openai", "https://api.openai.com")
    payload = adapter._build_payload(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hello"}],
        thinking_level="off",
    )
    assert payload["model"] == "gpt-4o"
    assert payload["messages"][0]["content"] == "hello"
    # No image_url parts should be present
    content = payload["messages"][0]["content"]
    if isinstance(content, list):
        assert all(p.get("type") == "text" for p in content)


# ---------------------------------------------------------------------------
# Codex Responses API payload tests
# ---------------------------------------------------------------------------


class TestCodexPayload:
    @pytest.mark.parametrize(
        ("level", "effort"),
        [("off", None), ("minimal", "low"), ("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "high")],
    )
    def test_codex_reasoning_maps_every_level(self, level: str, effort: str | None) -> None:
        payload = CodexResponsesAdapter()._build_payload(
            "gpt", [{"role": "user", "content": "hi"}], level, stream=False
        )
        assert payload.get("reasoning") == ({"effort": effort, "summary": "auto"} if effort else None)

    def test_codex_text_only_payload(self) -> None:
        """Text-only payload: input_text content part, no image parts."""
        from one.providers.codex_responses import CodexResponsesAdapter

        adapter = CodexResponsesAdapter()
        messages = [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        payload = adapter._build_payload("gpt-5.6-sol", messages, "high", stream=False, max_tokens=77)
        assert payload["model"] == "gpt-5.6-sol"
        assert payload["instructions"] == "be terse"
        assert payload["store"] is False and payload["stream"] is False
        assert payload["max_output_tokens"] == 77
        assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
        # HOTFIX-5: backend requires Responses message items with explicit type.
        assert all(i["type"] == "message" for i in payload["input"])
        assert [i["content"][0]["type"] for i in payload["input"]] == ["input_text", "output_text"]
        assert [i["role"] for i in payload["input"]] == ["user", "assistant"]

    def test_codex_payload_instructions_fallback(self) -> None:
        """Empty instructions fall back to base Codex prompt."""
        from one.providers.codex_responses import CodexResponsesAdapter

        adapter = CodexResponsesAdapter()
        p = adapter._build_payload("gpt-5.6-sol", [{"role": "user", "content": "hi"}], "medium", stream=False)
        assert p["instructions"].startswith("You are Codex")

    def test_codex_text_only_no_images(self) -> None:
        """When no images are present, _build_payload works normally."""
        from one.providers.codex_responses import CodexResponsesAdapter

        adapter = CodexResponsesAdapter()
        payload = adapter._build_payload(
            "gpt-5.6-sol",
            [{"role": "user", "content": "hello"}],
            "off",
            stream=False,
            images=None,
            storage_dir="",
        )
        user_item = payload["input"][0]
        assert user_item["role"] == "user"
        assert user_item["content"][0]["type"] == "input_text"
        assert user_item["content"][0]["text"] == "hello"
        assert all(p.get("type") != "input_image" for p in user_item["content"])

    def test_codex_payload_with_images_as_input_image(self) -> None:
        """Images are serialized as input_image parts alongside input_text."""
        from one.providers.codex_responses import CodexResponsesAdapter

        adapter = CodexResponsesAdapter()
        # Mock _read_blob to return test PNG data.
        png = _make_png()
        adapter._read_blob = lambda storage_dir, blob_hash: png
        messages = [{"role": "user", "content": "look at this"}]
        images = [{"blobHash": "abc", "mime": "image/png"}]
        payload = adapter._build_payload(
            "gpt-5.6-sol", messages, "medium", stream=False,
            images=images, storage_dir="/tmp",
        )
        user_item = payload["input"][0]
        content = user_item["content"]
        assert isinstance(content, list)
        text_parts = [p for p in content if p.get("type") == "input_text"]
        image_parts = [p for p in content if p.get("type") == "input_image"]
        assert len(text_parts) == 1
        assert text_parts[0]["text"] == "look at this"
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"].startswith("data:image/png;base64,")

    def test_codex_payload_image_blob_missing_raises_no_http(self) -> None:
        """Missing blob raises MissingBlobError without making HTTP."""
        from one.providers.codex_responses import (
            CodexResponsesAdapter,
            MissingBlobError,
        )

        adapter = CodexResponsesAdapter()
        adapter._read_blob = lambda storage_dir, blob_hash: None
        messages = [{"role": "user", "content": "hello"}]
        images = [{"blobHash": "missing", "mime": "image/png"}]
        with pytest.raises(MissingBlobError, match="missing or corrupt before HTTP"):
            adapter._build_payload(
                "gpt-5.6-sol", messages, "medium", stream=False,
                images=images, storage_dir="/tmp",
            )

    def test_codex_payload_missing_blob_hash_raises(self) -> None:
        """Image ref without blob_hash raises MissingBlobError."""
        from one.providers.codex_responses import (
            CodexResponsesAdapter,
            MissingBlobError,
        )

        adapter = CodexResponsesAdapter()
        messages = [{"role": "user", "content": "hello"}]
        images = [{"mime": "image/png"}]  # no blobHash
        with pytest.raises(MissingBlobError, match="missing blob_hash"):
            adapter._build_payload(
                "gpt-5.6-sol", messages, "medium", stream=False,
                images=images, storage_dir="/tmp",
            )

    def test_codex_payload_assistant_output_text_unchanged(self) -> None:
        """Assistant turns keep output_text content parts."""
        from one.providers.codex_responses import CodexResponsesAdapter

        adapter = CodexResponsesAdapter()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        payload = adapter._build_payload("gpt-5.6-sol", messages, "off", stream=False)
        assistant_item = payload["input"][1]
        assert assistant_item["role"] == "assistant"
        assert assistant_item["content"][0]["type"] == "output_text"
        assert assistant_item["content"][0]["text"] == "a"


# ---------------------------------------------------------------------------
# Anthropic cache_control breakpoint tests
# ---------------------------------------------------------------------------


def _anthropic_fake_client(monkeypatch) -> tuple[type, dict[str, Any]]:
    """Patch anthropic.httpx.AsyncClient and capture the last request JSON body.

    Returns ``(FakeClient, captured_body)`` where *captured_body* is set after
    each ``chat`` call.
    """
    from one.providers import anthropic as anth_mod

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        text = ""
        is_error = False

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 42,
                    "cache_creation_input_tokens": 17,
                },
                "stop_reason": "end_turn",
            }

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        captured["body"] = kwargs.get("json", {})
        return _Resp()

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def post(self, url: str, **kwargs: Any):
            return handler("POST", url, kwargs)

        def stream(self, method: str, url: str, **kwargs: Any):
            return handler(method, url, kwargs)

    monkeypatch.setattr(anth_mod.httpx, "AsyncClient", _FakeClient)
    return _FakeClient, captured


class TestAnthropicCacheControl:
    """Prompt-caching breakpoints: system + last user message."""

    @pytest.mark.asyncio
    async def test_cache_control_on_system_and_last_user(self, monkeypatch) -> None:
        """Breakpoint present on both system block and last user message."""
        adapter = AnthropicAdapter()
        _, captured = _anthropic_fake_client(monkeypatch)
        await adapter.chat(
            "sk-1",
            "claude-3",
            [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hello"},
            ],
            "off",
        )
        body = captured["body"]
        # System breakpoint — must be a list of content blocks (bare dict rejected by API)
        assert body["system"] == [
            {"text": "be terse", "cache_control": {"type": "ephemeral"}}
        ]
        # Last user message breakpoint
        user_msg = [m for m in body["messages"] if m["role"] == "user"][-1]
        assert user_msg["content"] == [
            {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
        ]
        # Count breakpoints: 1 system + 1 message = 2
        bp_count = 1  # system
        for m in body["messages"]:
            c = m.get("content")
            if isinstance(c, list):
                bp_count += sum(1 for p in c if p.get("cache_control"))
        assert bp_count == 2

    @pytest.mark.asyncio
    async def test_cache_control_with_images(self, monkeypatch) -> None:
        """Content blocks with images — breakpoint on last text block."""
        adapter = AnthropicAdapter()
        png = _make_png()
        adapter._read_blob = lambda storage_dir, blob_hash: png  # type: ignore[attr-defined]
        _, captured = _anthropic_fake_client(monkeypatch)
        await adapter.chat(
            "sk-1",
            "claude-3",
            [{"role": "user", "content": "look at this"}],
            "off",
            images=[{"blobHash": "abc", "mime": "image/png"}],
            storage_dir="/tmp",
        )
        body = captured["body"]
        user_msg = [m for m in body["messages"] if m["role"] == "user"][-1]
        content = user_msg["content"]
        assert isinstance(content, list)
        # Should have text + image blocks; breakpoint on the last text block.
        text_blocks = [p for p in content if p.get("type") == "text"]
        assert len(text_blocks) == 1
        assert text_blocks[0]["cache_control"] == {"type": "ephemeral"}
        image_blocks = [p for p in content if p.get("type") == "image"]
        assert len(image_blocks) == 1
        # No cache_control on image block
        assert "cache_control" not in image_blocks[0]

    @pytest.mark.asyncio
    async def test_cache_control_no_system(self, monkeypatch) -> None:
        """Without a system message, only the last user message has a breakpoint."""
        adapter = AnthropicAdapter()
        _, captured = _anthropic_fake_client(monkeypatch)
        await adapter.chat(
            "sk-1",
            "claude-3",
            [{"role": "user", "content": "hi there"}],
            "off",
        )
        body = captured["body"]
        assert "system" not in body
        user_msg = [m for m in body["messages"] if m["role"] == "user"][-1]
        assert user_msg["content"] == [
            {"type": "text", "text": "hi there", "cache_control": {"type": "ephemeral"}}
        ]

    @pytest.mark.asyncio
    async def test_cache_control_multi_turn_only_last_user(self, monkeypatch) -> None:
        """Only the LAST user message gets a breakpoint, earlier ones don't."""
        adapter = AnthropicAdapter()
        _, captured = _anthropic_fake_client(monkeypatch)
        await adapter.chat(
            "sk-1",
            "claude-3",
            [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "second"},
            ],
            "off",
        )
        body = captured["body"]
        user_msgs = [m for m in body["messages"] if m["role"] == "user"]
        assert len(user_msgs) == 2
        # First user message: no cache_control
        assert "cache_control" not in user_msgs[0]["content"]
        # Last user message: has cache_control
        assert user_msgs[1]["content"] == [
            {"type": "text", "text": "second", "cache_control": {"type": "ephemeral"}}
        ]

    @pytest.mark.asyncio
    async def test_breakpoint_count_max_4(self, monkeypatch) -> None:
        """Even with many messages, Anthropic gets only 2 breakpoints."""
        adapter = AnthropicAdapter()
        _, captured = _anthropic_fake_client(monkeypatch)
        await adapter.chat(
            "sk-1",
            "claude-3",
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "u3"},
            ],
            "off",
        )
        body = captured["body"]
        bp_count = 1  # system
        for m in body["messages"]:
            c = m.get("content")
            if isinstance(c, list):
                bp_count += sum(1 for p in c if p.get("cache_control"))
            elif isinstance(c, str) and c == "":
                pass  # no content
        # Only system + last user = 2 breakpoints (well under 4)
        assert bp_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("level", "budget"),
        [("off", None), ("minimal", 1024), ("low", 2048), ("medium", 4096), ("high", 8192), ("xhigh", 16384)],
    )
    async def test_thinking_payload_maps_every_level(self, monkeypatch, level: str, budget: int | None) -> None:
        adapter = AnthropicAdapter()
        _, captured = _anthropic_fake_client(monkeypatch)
        await adapter.chat("sk-1", "claude", [{"role": "user", "content": "hi"}], level, max_tokens=8)
        body = captured["body"]
        assert body.get("thinking") == ({"type": "enabled", "budget_tokens": budget} if budget else None)
        assert body["max_tokens"] >= (budget + 1024 if budget else 8)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("level", "budget"),
    [("off", None), ("minimal", 1024), ("low", 2048), ("medium", 4096), ("high", 8192), ("xhigh", 16384), ("invalid", None)],
)
async def test_gemini_thinking_payload_maps_every_level(monkeypatch, level: str, budget: int | None) -> None:
    from one.providers import gemini as gemini_mod

    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def post(self, url: str, **kwargs: Any) -> _Resp:  # noqa: ARG002
            captured["body"] = kwargs["json"]
            return _Resp()

    monkeypatch.setattr(gemini_mod.httpx, "AsyncClient", lambda *args, **kwargs: _Client())
    await GeminiAdapter().chat("key", "gemini-2.5-flash", [{"role": "user", "content": "hi"}], level)
    generation_config = captured["body"]["generationConfig"]
    assert generation_config.get("thinkingConfig") == (
        {"thinkingBudget": budget} if budget is not None else None
    )


def test_other_adapters_unchanged_by_cache_control(monkeypatch) -> None:
    """Non-Anthropic adapters should not include cache_control fields."""
    adapter = OpenAICompatibleAdapter("openai", "https://api.openai.com")
    payload = adapter._build_payload(
        model="gpt-4",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="off",
    )
    # Guard: OpenAI payloads never have cache_control.
    assert "cache_control" not in payload
    # Recursion/serialization check — round-trip through json (executes serialization path).
    import json

    json.loads(json.dumps(payload))


# ---------------------------------------------------------------------------
# Cache token accounting test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_usage_maps_cache_tokens(tmp_path, monkeypatch) -> None:
    """Fake transport echoing cache_read/cache_creation tokens flows into session stats."""
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager

    # Create in-memory managers (follows test_event_snapshots pattern).
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("anthropic", "sk-dummy")
    reg = ModelRegistry.create(auth)
    reg.register_models("anthropic", [{"id": "claude-3", "reasoning": False}])
    model = reg.find("anthropic", "claude-3")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    sessions = SessionManager.in_memory(str(tmp_path))

    # Fake Anthropic provider that echoes cache tokens in usage.
    class _FakeAnthropic:
        async def chat(self, api_key, model, messages, thinking_level, headers=None,
                       on_delta=None, on_thinking_delta=None, max_tokens=None,
                       images=None, storage_dir=""):  # noqa: ARG002
            from one.providers.base import ChatResult
            return ChatResult(
                text="cached response",
                raw={},
                usage={
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 88,
                    "cache_creation_input_tokens": 33,
                },
                stop_reason="end_turn",
            )

    class _Loader:
        def get_system_prompt(self, selected_tools=None):  # noqa: ARG002
            return "You are a coding agent."

    session = AgentSession(sessions, settings, reg, _Loader(), model, "off")
    session.providers = {"anthropic": _FakeAnthropic()}  # type: ignore[assignment]

    # Call _invoke_provider which wraps the raw provider response.
    event = await session._invoke_provider([{"role": "user", "content": "test"}])

    assert event["usage"]["input"] == 20
    assert event["usage"]["output"] == 10
    assert event["usage"]["cacheRead"] == 88
    assert event["usage"]["cacheWrite"] == 33
