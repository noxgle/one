"""Tests for the explicit read_image tool and tool-image session flow."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo
from one.providers.anthropic import AnthropicAdapter
from one.providers.gemini import GeminiAdapter
from one.providers.openai_compatible import OpenAICompatibleAdapter
from one.tools.read import read_tool
from one.tools.read_image import read_image_tool


def _make_png() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    width = struct.pack(">I", 1)
    height = struct.pack(">I", 1)
    ihdr_data = width + height + b"\x08\x06\x00\x00"
    ihdr = b"\x00\x00\x00\x0dIHDR" + ihdr_data + b"\x00\x00\x00\x00"
    idat = b"\x00\x00\x00\x03IDAT\x08\x99c\xfc\xcf\x00\x00\x00\x02\x00\x01" + b"\x00\x00\x00\x00"
    iend = b"\x00\x00\x00\x00IEND" + b"\xaeB`\x82"
    return sig + ihdr + idat + iend


@pytest.fixture
def png_bytes() -> bytes:
    return _make_png()


@pytest.fixture
def png_file(tmp_path: Path, png_bytes: bytes) -> Path:
    p = tmp_path / "img.png"
    p.write_bytes(png_bytes)
    return p


class _Loader:
    def get_system_prompt(self, selected_tools=None) -> str:
        return "You are a coding agent."


class _CaptureProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.captured: list[dict[str, Any]] = []

    async def chat(self, api_key, model, messages, thinking_level, headers=None, images=None, storage_dir=""):
        from one.providers.base import ChatResult

        self.captured.append({"images": images, "messages": messages, "storage_dir": storage_dir})
        text = self.responses.pop(0) if self.responses else "done"
        return ChatResult(text=text, raw={}, usage={}, stop_reason="stop")


def _make_session(tmp_path: Path, provider: _CaptureProvider, input_image: bool = True) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    from one.core.model_registry import ModelRegistry

    registry = ModelRegistry.create(auth)
    model = ModelInfo(provider="openai", id="gpt-4o", input_image=input_image)
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))
    session = AgentSession(
        session_manager,
        settings,
        registry,
        _Loader(),
        model,
        "medium",
        storage_dir=str(tmp_path / "store"),
    )
    session.providers = {"openai": provider}  # type: ignore[dict-item]
    return session


def test_read_image_absolute_path(tmp_path: Path, png_file: Path) -> None:
    storage = str(tmp_path / "store")
    result = read_image_tool(str(tmp_path), str(png_file), storage)
    assert result["image"]["mime"] == "image/png"
    assert result["details"]["mime"] == "image/png"
    assert "Loaded image" in result["content"][0]["text"]
    # No source path or base64 in the returned metadata beyond the file name.
    assert str(png_file) not in json.dumps(result["details"])


def test_read_image_relative_and_spaces(tmp_path: Path, png_bytes: bytes) -> None:
    sub = tmp_path / "my dir"
    sub.mkdir()
    target = sub / "pic.png"
    target.write_bytes(png_bytes)
    storage = str(tmp_path / "store")
    result = read_image_tool(str(tmp_path), "my dir/pic.png", storage)
    assert result["image"]["mime"] == "image/png"


def test_read_image_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_image_tool(str(tmp_path), str(tmp_path / "nope.png"), str(tmp_path / "store"))


def test_read_image_unsupported_file(tmp_path: Path) -> None:
    bad = tmp_path / "note.txt"
    bad.write_text("hello")
    with pytest.raises(ValueError, match="Unsupported image format"):
        read_image_tool(str(tmp_path), str(bad), str(tmp_path / "store"))


def test_read_text_image_suggests_read_image(tmp_path: Path, png_file: Path) -> None:
    with pytest.raises(ValueError, match="read_image"):
        read_tool(str(tmp_path), str(png_file))


def test_read_text_normal_file_still_works(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("line1\nline2\n")
    result = read_tool(str(tmp_path), str(target))
    assert "line1" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_read_image_tool_flows_to_next_provider_call(tmp_path: Path, png_file: Path) -> None:
    provider = _CaptureProvider(
        [
            json.dumps({"tool": "read_image", "args": {"path": png_file.name}}),
            json.dumps({"tool": "finish", "args": {"summary": "described", "goal_success": True}}),
        ]
    )
    # png_file already lives in the session cwd (tmp_path).
    session = _make_session(tmp_path, provider, input_image=True)
    await session.prompt("find the image and describe it")
    assert len(provider.captured) >= 2
    second = provider.captured[1]
    assert second["images"] is not None
    assert len(second["images"]) == 1
    assert second["images"][0]["mime"] == "image/png"


@pytest.mark.asyncio
async def test_read_image_unsupported_model_sends_no_bytes(tmp_path: Path, png_file: Path) -> None:
    provider = _CaptureProvider(
        [
            json.dumps({"tool": "read_image", "args": {"path": png_file.name}}),
            json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}}),
        ]
    )
    session = _make_session(tmp_path, provider, input_image=False)
    await session.prompt("describe the image")
    # Capability gate produces controlled response without provider image bytes.
    assert provider.captured[0]["images"] is None
    assistants = [m for m in session.messages if m.get("role") == "assistant"]
    assert assistants[-1].get("stopReason") == "unsupported_vision"


@pytest.mark.asyncio
async def test_tool_result_message_contains_no_blob_or_path(tmp_path: Path, png_file: Path) -> None:
    provider = _CaptureProvider(
        [
            json.dumps({"tool": "read_image", "args": {"path": png_file.name}}),
            json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}}),
        ]
    )
    session = _make_session(tmp_path, provider, input_image=True)
    await session.prompt("look")
    tool_msgs = [m for m in session.messages if m.get("role") == "toolResult"]
    assert tool_msgs
    dumped = json.dumps(tool_msgs, ensure_ascii=False)
    assert str(png_file) not in dumped
    assert "base64" not in dumped.lower()


def test_openai_attaches_to_last_user_message() -> None:
    adapter = OpenAICompatibleAdapter("openai", "https://api.openai.com")
    adapter._read_blob = lambda storage_dir, blob_hash: b"image-bytes"  # type: ignore[method-assign]
    payload = adapter._build_payload(
        model="m",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "tooling"},
            {"role": "user", "content": "tool result text"},
        ],
        thinking_level="off",
        images=[{"blob_hash": "abc", "mime": "image/png"}],
        storage_dir="/tmp/store",
    )
    assert payload["messages"][0]["content"] == "first"
    last = payload["messages"][2]["content"]
    assert isinstance(last, list)
    assert last[0] == {"type": "text", "text": "tool result text"}
    assert last[1]["type"] == "image_url"


def test_anthropic_attaches_to_last_user_message() -> None:
    adapter = AnthropicAdapter()
    adapter._read_blob = lambda storage_dir, blob_hash: b"image-bytes"  # type: ignore[method-assign]
    messages = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "current"},
    ]
    # Simulate chat expansion logic: last user message gets images.
    expanded = []
    last_user = 1
    for i, m in enumerate(messages):
        content = m["content"]
        if m["role"] == "user" and i == last_user:
            content = adapter._resolve_message_content("user", content, [{"blob_hash": "abc"}], "/tmp")
        expanded.append(content)
    assert expanded[0] == "first"
    assert isinstance(expanded[1], list)
    assert expanded[1][-1]["type"] == "image"


def test_gemini_attaches_to_last_user_message() -> None:
    adapter = GeminiAdapter()
    adapter._read_blob = lambda storage_dir, blob_hash: b"image-bytes"  # type: ignore[method-assign]
    parts = adapter._resolve_message_parts("user", "current", [{"blob_hash": "abc"}], "/tmp")
    assert parts[0] == {"text": "current"}
    assert "inline_data" in parts[1]


# ---------------------------------------------------------------------------
# Failed read_image: path sanitisation in events + messages (regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_image_tool_error_sanitises_path_in_events_and_messages(
    tmp_path: Path,
) -> None:
    """Failed ``read_image`` must not leak the full source path.

    Assertions:
    - no full source path in persisted JSONL messages
    - ``tool_call_error`` event has sanitised ``args`` / ``error``
    - ``tool_call_end`` result has sanitised ``args``
    - ``turn_end.toolResults`` has sanitised ``error``
    - only the basename appears in all of the above.
    """
    provider = _CaptureProvider(
        [
            json.dumps({"tool": "read_image", "args": {"path": "/absolute/missing.png"}}),
        ]
    )
    session = _make_session(tmp_path, provider, input_image=True)
    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("describe it")

    full_path = str(tmp_path / "absolute" / "missing.png")
    basename = "missing.png"

    # ── JSONL messages must not contain the full path ───────────────────────
    all_messages_dump = json.dumps(session.messages, ensure_ascii=False)
    assert full_path not in all_messages_dump, "full path must not appear in JSONL messages"

    # ── tool_call_error event ───────────────────────────────────────────────
    error_events = [e for e in events if e.get("type") == "tool_call_error"]
    assert len(error_events) >= 1, "tool_call_error event must be emitted for failed read_image"
    err_ev = error_events[0]
    assert err_ev.get("tool") == "read_image"
    ev_args = err_ev.get("args", {})
    assert full_path not in json.dumps(ev_args), "tool_call_error args must be sanitised"
    # The basename should appear in the args instead.
    assert basename in str(ev_args.get("path", "")), "basename must replace full path in event args"
    # Error text must also be sanitised.
    assert full_path not in str(err_ev.get("error", "")), "tool_call_error error must not contain full path"

    # ── tool_call_end event ─────────────────────────────────────────────────
    end_events = [e for e in events if e.get("type") == "tool_call_end"]
    assert len(end_events) >= 1, "tool_call_end event must be emitted"
    end_result = end_events[0].get("result", {})
    assert full_path not in json.dumps(end_result), "tool_call_end result must be sanitised"

    # ── turn_end.toolResults ────────────────────────────────────────────────
    turn_end_events = [e for e in events if e.get("type") == "turn_end"]
    assert len(turn_end_events) >= 1, "turn_end event must be emitted"
    turn_tool_results = turn_end_events[0].get("toolResults", [])
    assert len(turn_tool_results) >= 1, "toolResults must contain the failed tool payload"
    tr = turn_tool_results[0]
    assert full_path not in json.dumps(tr), "turn_end.toolResults must not contain full path"
    # The error in toolResults should only have the basename.
    if tr.get("error"):
        assert full_path not in str(tr["error"]), "toolResults error must be sanitised"
        assert basename in str(tr.get("args", {})), "toolResults args must contain basename"
