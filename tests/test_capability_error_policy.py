"""Tests for _CapabilityError / _CapabilityErrorCooperative handling policy.

In autonomous mode the session emits a controlled assistant response.
In cooperation mode (approval_callback is not None) the session raises
_CapabilityErrorCooperative so the caller knows no provider call was made.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import (
    AgentSession,
    _CapabilityError,
    _CapabilityErrorCooperative,
)
from one.core.attachments import import_image
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_png() -> bytes:
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


@pytest.fixture
def png_path(tmp_path: Path) -> Path:
    p = tmp_path / "img.png"
    p.write_bytes(_make_png())
    return p


class _FakeLoader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:
        return "You are a coding agent."


# ---------------------------------------------------------------------------
# Helper: build a session with a non-image-capable model.
# ---------------------------------------------------------------------------


def _make_image_disabled_session(
    tmp_path: Path,
    approval_callback: Any = None,
) -> AgentSession:
    """Create a session whose model explicitly lacks input_image."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    # Use a model that has input_image=False (the default).
    model = ModelInfo(provider="openai", id="gpt-4o")  # input_image defaults False
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    session = AgentSession(
        session_manager,
        settings,
        registry,
        _FakeLoader(),
        model,
        "medium",
        storage_dir=str(tmp_path / "store"),
        approval_callback=approval_callback,
    )
    return session


# ---------------------------------------------------------------------------
# Test: _CapabilityErrorCooperative inherits from _CapabilityError
# ---------------------------------------------------------------------------


def test_capability_error_cooperative_inherits():
    err = _CapabilityErrorCooperative("test message")
    assert isinstance(err, _CapabilityError)
    assert isinstance(err, RuntimeError)


def test_capability_error_inherits_runtime_error():
    err = _CapabilityError("test message")
    assert isinstance(err, RuntimeError)


# ---------------------------------------------------------------------------
# Autonomous mode: _CapabilityError → controlled assistant response
# ---------------------------------------------------------------------------


class _CaptureProvider:
    """Captures messages + images passed to chat."""

    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []
        self.chat_call_count = 0

    async def chat(self, api_key, model, messages, thinking_level, headers=None, images=None, storage_dir=""):
        self.chat_call_count += 1
        self.captured.append({"images": images, "messages": messages})
        from one.providers.base import ChatResult
        return ChatResult(text="done", raw={}, usage={}, stop_reason="stop")


@pytest.mark.asyncio
async def test_autonomous_mode_capability_error_emits_assistant_response(
    tmp_path: Path, png_path: Path
):
    """Autonomous mode: model without input_image + images → controlled assistant error, no provider call."""
    session = _make_image_disabled_session(tmp_path)
    assert session.approval_callback is None  # autonomous

    # Import an image to trigger the capability check.
    ref = import_image(str(tmp_path / "store"), str(png_path))
    image_refs = [ref.__dict__]

    # Replace providers with a capture provider so we can verify it's NOT called.
    session.providers = {"openai": _CaptureProvider()}

    await session.prompt("look at this", images=image_refs)

    # Provider must NOT have been called.
    assert session.providers["openai"].chat_call_count == 0

    # The session messages must contain an assistant error response.
    assistant_msgs = [m for m in session.messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) >= 1
    last_assistant = assistant_msgs[-1]
    assert last_assistant.get("stopReason") == "unsupported_vision"
    assert "does not support image input" in str(last_assistant.get("content", "")).lower()


# ---------------------------------------------------------------------------
# Cooperation mode: _CapabilityErrorCooperative is raised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooperation_mode_capability_error_raises(
    tmp_path: Path, png_path: Path
):
    """Cooperation mode: model without input_image + images → _CapabilityErrorCooperative raised."""

    async def dummy_approval(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        return (True, "")

    session = _make_image_disabled_session(tmp_path, approval_callback=dummy_approval)
    assert session.approval_callback is not None  # cooperation

    ref = import_image(str(tmp_path / "store"), str(png_path))
    image_refs = [ref.__dict__]

    with pytest.raises(_CapabilityErrorCooperative, match="does not support image input"):
        await session.prompt("look at this", images=image_refs)


@pytest.mark.asyncio
async def test_cooperation_mode_capability_error_not_cached_as_assistant(
    tmp_path: Path, png_path: Path
):
    """Cooperation mode: no assistant error message is cached in session.messages.

    Note: prompt() always appends a user message before calling the provider,
    so the message count will be 1 after the exception (the user message).
    What matters is that NO assistant error message was cached.
    """

    async def dummy_approval(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        return (True, "")

    session = _make_image_disabled_session(tmp_path, approval_callback=dummy_approval)

    ref = import_image(str(tmp_path / "store"), str(png_path))
    image_refs = [ref.__dict__]

    with pytest.raises(_CapabilityErrorCooperative):
        await session.prompt("look at this", images=image_refs)

    # Only the user message should exist — no assistant error message.
    assistant_msgs = [m for m in session.messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 0


# ---------------------------------------------------------------------------
# Normal prompt (no images) still works in both modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autonomous_mode_normal_prompt_works(tmp_path: Path):
    """Autonomous mode: plain prompt without images works normally."""
    session = _make_image_disabled_session(tmp_path)
    session.providers = {"openai": _CaptureProvider()}

    await session.prompt("hello")
    # Provider is called at least once for the initial turn.
    assert session.providers["openai"].chat_call_count >= 1


@pytest.mark.asyncio
async def test_cooperation_mode_normal_prompt_works(tmp_path: Path):
    """Cooperation mode: plain prompt without images works normally."""

    async def dummy_approval(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        return (True, "")

    session = _make_image_disabled_session(tmp_path, approval_callback=dummy_approval)
    session.providers = {"openai": _CaptureProvider()}

    await session.prompt("hello")
    # Provider is called at least once for the initial turn.
    assert session.providers["openai"].chat_call_count >= 1


# ---------------------------------------------------------------------------
# Model WITH input_image=True doesn't trigger capability error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_with_input_image_support_passes_check(tmp_path: Path, png_path: Path):
    """Model that declares input_image=True does NOT raise _CapabilityError."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = ModelInfo(provider="openai", id="gpt-4o", input_image=True)
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    session = AgentSession(
        session_manager, settings, registry, _FakeLoader(),
        model, "medium", storage_dir=str(tmp_path / "store"),
    )
    ref = import_image(str(tmp_path / "store"), str(png_path))
    image_refs = [ref.__dict__]
    session.providers = {"openai": _CaptureProvider()}

    await session.prompt("look at this", images=image_refs)
    # Should NOT raise; provider must be called.
    assert session.providers["openai"].chat_call_count >= 1
    assert session.providers["openai"].captured[0]["images"] is not None


# ---------------------------------------------------------------------------
# Cooperative mode: state reset + subsequent prompt (regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooperation_mode_capability_error_resets_state_and_allows_subsequent_prompt(
    tmp_path: Path, png_path: Path
):
    """Cooperation mode: after _CapabilityErrorCooperative the session is fully reset.

    Asserts:
    - `_is_streaming` is ``False`` after the exception
    - an ``agent_end`` event was emitted
    - transient ``_images`` / ``_tool_images`` are cleared
    - a subsequent ``prompt()`` with a text-only provider completes successfully.
    """

    async def dummy_approval(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        return (True, "")

    session = _make_image_disabled_session(tmp_path, approval_callback=dummy_approval)
    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    ref = import_image(str(tmp_path / "store"), str(png_path))
    image_refs = [ref.__dict__]

    with pytest.raises(_CapabilityErrorCooperative, match="does not support image input"):
        await session.prompt("look at this", images=image_refs)

    # ── State reset assertions ──────────────────────────────────────────────
    assert session._is_streaming is False, "streaming flag must be cleared after cooperative error"
    assert session._images is None, "_images must be None after cooperative error"
    assert session._tool_images == [], "_tool_images must be empty after cooperative error"

    # ── agent_end event ─────────────────────────────────────────────────────
    agent_end_events = [e for e in events if e.get("type") == "agent_end"]
    assert len(agent_end_events) == 1, "exactly one agent_end event should be emitted"

    # ── Subsequent prompt succeeds ──────────────────────────────────────────
    session.providers = {"openai": _CaptureProvider()}
    await session.prompt("hello from reset session")
    assert session.providers["openai"].chat_call_count >= 1
    # Session should be back to non-streaming.
    assert session._is_streaming is False


# ---------------------------------------------------------------------------
# Fake adapter without images param → explicit capability error
# ---------------------------------------------------------------------------


class _NoImagesAdapter:
    """Provider adapter whose chat() signature deliberately lacks 'images'."""

    def __init__(self) -> None:
        self.chat_call_count = 0

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
        storage_dir: str = "",
    ):
        self.chat_call_count += 1
        from one.providers.base import ChatResult
        return ChatResult(text="done", raw={}, usage={}, stop_reason="stop")


@pytest.mark.asyncio
async def test_fake_adapter_without_images_param_raises_capability_error(
    tmp_path: Path, png_path: Path
):
    """Model with input_image=True but adapter signature lacks 'images' → explicit
    _CapabilityError (autonomous) or _CapabilityErrorCooperative (cooperation).

    This verifies the fix: images are never silently dropped — the caller gets
    a visible capability-style error instead of a text-only request with no image.
    """
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    # Model declares input_image=True (vision-capable model)
    model = ModelInfo(provider="openai", id="gpt-4o", input_image=True)
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    session = AgentSession(
        session_manager, settings, registry, _FakeLoader(),
        model, "medium", storage_dir=str(tmp_path / "store"),
    )

    ref = import_image(str(tmp_path / "store"), str(png_path))
    image_refs = [ref.__dict__]

    # Replace with adapter that lacks 'images' in its chat signature
    no_images_provider = _NoImagesAdapter()
    session.providers = {"openai": no_images_provider}

    await session.prompt("look at this", images=image_refs)

    # Provider must NOT have been called — images were dropped at the capability gate
    assert no_images_provider.chat_call_count == 0

    # The session messages must contain an assistant error response.
    assistant_msgs = [m for m in session.messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) >= 1
    last_assistant = assistant_msgs[-1]
    assert last_assistant.get("stopReason") == "unsupported_vision"
    assert "does not support image input" in str(last_assistant.get("content", "")).lower()


# ---------------------------------------------------------------------------
# Codex adapter accepts images — wire-payload tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_adapter_nonstream_payloads_images_as_input_image(tmp_path: Path, png_path: Path):
    """Codex adapter serializes images as input_image parts in non-stream mode."""
    import httpx

    from one.providers.codex_responses import (
        CodexResponsesAdapter,
    )

    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        is_error = False

        def json(self):
            return {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {},
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url: str, json: dict | None, **kw):
            captured["payload"] = json
            return _Resp()

        async def stream(self, *a, **kw):
            raise RuntimeError("should not reach HTTP in non-stream test")

    original_client = httpx.AsyncClient
    httpx.AsyncClient = _Client
    try:
        adapter = CodexResponsesAdapter()
        ref = import_image(str(tmp_path / "store"), str(png_path))
        image_refs = [ref.__dict__]
        result = await adapter.chat(
            api_key="dummy",
            model="gpt-5.6-sol",
            messages=[{"role": "system", "content": "be concise"}, {"role": "user", "content": "look at this"}],
            thinking_level="medium",
            images=image_refs,
            storage_dir=str(tmp_path / "store"),
        )
        assert result.text == "ok"
        payload = captured["payload"]
        # System message is stripped; first (and only) input item is the user message.
        user_item = payload["input"][0]
        assert user_item["role"] == "user"
        content = user_item["content"]
        assert isinstance(content, list)
        text_parts = [p for p in content if p.get("type") == "input_text"]
        image_parts = [p for p in content if p.get("type") == "input_image"]
        assert len(text_parts) == 1
        assert text_parts[0]["text"] == "look at this"
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"].startswith("data:image/png;base64,")
        # store:false and reasoning must still be present.
        assert payload["store"] is False
        assert payload["reasoning"] == {"effort": "medium"}
    finally:
        httpx.AsyncClient = original_client


@pytest.mark.asyncio
async def test_codex_adapter_stream_payloads_images_as_input_image(tmp_path: Path, png_path: Path):
    """Codex adapter serializes images as input_image parts in stream mode."""
    import httpx

    from one.providers.codex_responses import CodexResponsesAdapter

    captured: dict[str, Any] = {}

    class _StreamResp:
        status_code = 200
        is_error = False

        async def aiter_lines(self):
            yield 'data: {"type":"response.output_text.delta","delta":"ok"}'
            yield 'data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{}}}'
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class _Ctx:
        def __init__(self, resp):
            self.resp = resp

        async def __aenter__(self):
            return self.resp

        async def __aexit__(self, *exc):
            return False

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *a, **kw):
            raise RuntimeError("should not reach HTTP in stream test")

        def stream(self, method: str, url: str, **kw):
            captured["payload"] = kw.get("json")
            return _Ctx(_StreamResp())

    original_client = httpx.AsyncClient
    httpx.AsyncClient = _Client
    try:
        adapter = CodexResponsesAdapter()
        ref = import_image(str(tmp_path / "store"), str(png_path))
        image_refs = [ref.__dict__]
        deltas: list[str] = []
        result = await adapter.chat(
            api_key="dummy",
            model="gpt-5.6-sol",
            messages=[{"role": "user", "content": "view this"}],
            thinking_level="off",
            images=image_refs,
            storage_dir=str(tmp_path / "store"),
            on_delta=deltas.append,
        )
        assert result.text == "ok"
        payload = captured["payload"]
        user_item = payload["input"][0]
        content = user_item["content"]
        assert isinstance(content, list)
        image_parts = [p for p in content if p.get("type") == "input_image"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"].startswith("data:image/png;base64,")
        assert payload["stream"] is True
    finally:
        httpx.AsyncClient = original_client


@pytest.mark.asyncio
async def test_codex_adapter_invalid_blob_no_http_request(tmp_path: Path):
    """Missing/invalid blob raises MissingBlobError BEFORE any HTTP call."""
    import httpx

    from one.providers.codex_responses import CodexResponsesAdapter, MissingBlobError

    called = {"post": False, "stream": False}

    class _SpyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *a, **kw):
            called["post"] = True
            raise RuntimeError("should not reach HTTP")

        async def stream(self, *a, **kw):
            called["stream"] = True
            raise RuntimeError("should not reach HTTP")

    original_client = httpx.AsyncClient
    httpx.AsyncClient = _SpyClient
    try:
        adapter = CodexResponsesAdapter()
        # Image ref pointing to a non-existent blob
        image_refs = [{"blobHash": "nonexistent_blob_hash", "mime": "image/png"}]
        with pytest.raises(MissingBlobError, match="missing or corrupt before HTTP"):
            await adapter.chat(
                api_key="dummy",
                model="gpt-5.6-sol",
                messages=[{"role": "user", "content": "hello"}],
                thinking_level="medium",
                images=image_refs,
                storage_dir=str(tmp_path / "store"),
            )
        assert called["post"] is False, "POST should not be called"
        assert called["stream"] is False, "stream should not be called"
    finally:
        httpx.AsyncClient = original_client


# ---------------------------------------------------------------------------
# Registry round-trip: built-in vision flag survives persist/reload;
# persisted false does NOT downgrade built-in.
# ---------------------------------------------------------------------------


def test_registry_round_trip_preserves_vision_flag(tmp_path: Path):
    """Persist then reload: gpt-5.6-sol keeps input_image=True."""
    auth = AuthStorage.in_memory()
    models_path = str(tmp_path / "models.json")
    registry = ModelRegistry.create(auth, models_path)
    # Persist with inputImage: true (simulating a login that writes the flag)
    registry.persist_models("chatgpt", ["gpt-5.6-sol", "gpt-5.6-terra"])
    # Reload from the persisted file
    registry2 = ModelRegistry.create(auth, models_path)
    sol = registry2.find("chatgpt", "gpt-5.6-sol")
    assert sol is not None and sol.input_image is True
    terra = registry2.find("chatgpt", "gpt-5.6-terra")
    assert terra is not None and terra.input_image is True


def test_persisted_false_does_not_downgrade_builtin_vision(tmp_path: Path):
    """When models.json has inputImage: false, the builtin input_image=True wins."""
    import json

    auth = AuthStorage.in_memory()
    models_path = str(tmp_path / "models.json")
    # Pre-write models.json with inputImage: false for gpt-5.6-sol
    data = {
        "providers": {
            "chatgpt": [
                {"id": "gpt-5.6-sol", "reasoning": True, "inputImage": False},
            ]
        }
    }
    (tmp_path / "models.json").write_text(json.dumps(data))
    # Create registry — builtin vision must NOT be downgraded.
    registry = ModelRegistry.create(auth, models_path)
    sol = registry.find("chatgpt", "gpt-5.6-sol")
    assert sol is not None and sol.input_image is True


def test_persisted_true_preserved(tmp_path: Path):
    """When models.json has inputImage: true it stays true on reload."""
    import json

    auth = AuthStorage.in_memory()
    models_path = str(tmp_path / "models.json")
    data = {
        "providers": {
            "chatgpt": [
                {"id": "gpt-5.6-sol", "reasoning": True, "inputImage": True},
            ]
        }
    }
    (tmp_path / "models.json").write_text(json.dumps(data))
    registry = ModelRegistry.create(auth, models_path)
    sol = registry.find("chatgpt", "gpt-5.6-sol")
    assert sol is not None and sol.input_image is True
