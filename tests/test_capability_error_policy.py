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
