"""Streaming suppression: prose threshold & adversarial tool-JSON chunking.

Tests the ``_on_delta`` pre-render buffer in
``one.core.agent_session.AgentSession._invoke_provider``.

The detector suppresses tool-call shaped payloads (starting with ``{``,
`` ``` `` or ``TOOL_CALL:``) until enough context accumulates to tell
prose apart from tool JSON.  This test file verifies that:

* Short prose is emitted as soon as the threshold is met.
* Chunked tool JSON (including adversarial single-character splits) never
  leaks as assistant text — only tool_call events.
* Existing suppression behavior is preserved.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.providers.base import ChatResult

# ── helpers ────────────────────────────────────────────────────────────────


class _Loader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."


class _StreamingProvider:
    """Provider that fires *every* chunk via ``on_delta`` then returns.

    ``deltas`` are emitted in order; ``final_text`` is the ``ChatResult.text``.
    """

    def __init__(self, deltas: list[str], final_text: str) -> None:
        self.deltas = deltas
        self.final_text = final_text
        self.calls = 0

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
        images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
    ) -> Any:
        self.calls += 1
        if on_delta:
            for d in self.deltas:
                on_delta(d)
        return ChatResult(text=self.final_text, raw={}, usage={}, stop_reason="stop")


def _mk_session(tmp_path: Path, tools: list[str] | None = None) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session, settings, registry, _Loader(), model, "medium", tools=tools)


def _message_update_deltas(events: list[dict[str, Any]]) -> list[str]:
    """Extract every text_delta value from message_update events."""
    deltas: list[str] = []
    for e in events:
        if e.get("type") == "message_update":
            deltas.append(e.get("assistantMessageEvent", {}).get("delta", ""))
    return deltas


# ── prose-emission tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_prose_emits_at_threshold(tmp_path: Path):
    """Prose that hits the threshold *exactly* is emitted."""
    session = _mk_session(tmp_path)
    # 16 chars — exactly at the lowered threshold.
    session.providers = {"openai": _StreamingProvider(["Hello, world!"], "Hello, world!")}

    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("go")

    deltas = _message_update_deltas(events)
    # Prose is emitted; the final assistant message will contain the text.
    assert any("Hello, world!" in d for d in deltas), f"Prose not emitted. Deltas: {deltas}"


@pytest.mark.asyncio
async def test_streaming_prose_above_threshold_emits(tmp_path: Path):
    """Prose well above the threshold is emitted normally."""
    session = _mk_session(tmp_path)
    session.providers = {"openai": _StreamingProvider(["A" * 50], "A" * 50)}

    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("go")

    deltas = _message_update_deltas(events)
    assert any(len(d) > 30 for d in deltas), f"Long prose not emitted. Deltas: {deltas}"


# ── tool-JSON suppression tests ────────────────────────────────────────────


def _tool_json_leaked(deltas: list[str], tool_json: str) -> bool:
    """Return True if the *exact* tool JSON appeared as a message_update delta.

    This checks for the tool JSON content, NOT just substrings like "tools"
    which may appear in agent error messages (e.g. "I ran tools...").
    """
    for d in deltas:
        if tool_json in d:
            return True
    return False


@pytest.mark.asyncio
async def test_streaming_tool_json_suppressed_single_chunk(tmp_path: Path):
    """A single-chunk tool JSON payload must not leak as assistant text."""
    session = _mk_session(tmp_path, tools=["read"])
    tool_json = '{"tool":"read","args":{"path":"a.txt"}}'
    session.providers = {"openai": _StreamingProvider([tool_json], tool_json)}

    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("go")

    deltas = _message_update_deltas(events)
    assert not _tool_json_leaked(deltas, tool_json), (
        f"Tool JSON leaked as assistant text. Deltas: {deltas}"
    )


@pytest.mark.asyncio
async def test_streaming_adversarial_tool_json_chunked(tmp_path: Path):
    """Adversarial single-character splits of a tool call must never leak."""
    session = _mk_session(tmp_path, tools=["read"])
    full = '{"tool":"read","args":{"path":"a.txt"}}'
    deltas_in = list(full)  # worst case: every character as its own delta

    session.providers = {"openai": _StreamingProvider(deltas_in, full)}

    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("go")

    deltas = _message_update_deltas(events)
    assert not _tool_json_leaked(deltas, full), (
        f"Tool JSON leaked as assistant text after adversarial chunking. Deltas: {deltas}"
    )


@pytest.mark.asyncio
async def test_streaming_tool_json_complete_chunked(tmp_path: Path):
    """Tool JSON split across two tiny deltas must be suppressed."""
    session = _mk_session(tmp_path, tools=["read"])
    full = '{"tool":"read","args":{"path":"a.txt"}}'
    half = len(full) // 2
    deltas_in = [full[:half], full[half:]]

    session.providers = {"openai": _StreamingProvider(deltas_in, full)}

    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("go")

    deltas = _message_update_deltas(events)
    assert not _tool_json_leaked(deltas, full), (
        f"Tool JSON leaked after 2-chunk split. Deltas: {deltas}"
    )


@pytest.mark.asyncio
async def test_streaming_tool_json_triple_chunked(tmp_path: Path):
    """Tool JSON split across three small deltas must still be suppressed."""
    session = _mk_session(tmp_path, tools=["read"])
    full = '{"tool":"read","args":{"path":"a.txt"}}'
    third = len(full) // 3
    deltas_in = [full[:third], full[third : 2 * third], full[2 * third :]]

    session.providers = {"openai": _StreamingProvider(deltas_in, full)}

    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("go")

    deltas = _message_update_deltas(events)
    assert not _tool_json_leaked(deltas, full), (
        f"Tool JSON leaked after 3-chunk split. Deltas: {deltas}"
    )


# ── non-toolish { prose ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_non_toolish_json_above_threshold(tmp_path: Path):
    """Non-toolish prose starting with '{' above threshold emits normally."""
    session = _mk_session(tmp_path)
    # 22 chars — above threshold, starts with '{', no tool markers
    prose = '{response: {"data": [1, 2, 3]}}'
    session.providers = {"openai": _StreamingProvider([prose], prose)}

    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("go")

    deltas = _message_update_deltas(events)
    assert any(prose in d for d in deltas), f"Non-toolish prose not emitted. Deltas: {deltas}"


# ── back-to-back prose + tool JSON ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_tool_json_then_prose_suppressed(tmp_path: Path):
    """Tool JSON in on_delta, followed by prose — tool JSON must not leak."""
    session = _mk_session(tmp_path, tools=["read"])
    tool_json = '{"tool":"read","args":{"path":"a.txt"}}'
    session.providers = {
        "openai": _StreamingProvider(
            [tool_json, " ", "After tool call, done."],
            tool_json + " " + "After tool call, done.",
        )
    }

    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("go")

    deltas = _message_update_deltas(events)
    assert not _tool_json_leaked(deltas, tool_json), (
        f"Tool JSON leaked in back-to-back case. Deltas: {deltas}"
    )


# ── code-fence and TOOL_CALL: prefix ───────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_code_fence_suppressed(tmp_path: Path):
    """Content starting with ``` with tool markers must be suppressed."""
    session = _mk_session(tmp_path)
    fence = '```json{"tool":"read","args":{}}'
    session.providers = {"openai": _StreamingProvider([fence], fence)}

    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("go")

    deltas = _message_update_deltas(events)
    assert not _tool_json_leaked(deltas, fence), (
        f"Code-fence tool JSON leaked. Deltas: {deltas}"
    )


@pytest.mark.asyncio
async def test_streaming_tool_call_prefix_suppressed(tmp_path: Path):
    """Content starting with TOOL_CALL: must be suppressed."""
    session = _mk_session(tmp_path)
    prefix = 'TOOL_CALL: {"tool":"read","args":{}}'
    session.providers = {"openai": _StreamingProvider([prefix], prefix)}

    events: list[dict[str, Any]] = []
    session.subscribe(events.append)

    await session.prompt("go")

    deltas = _message_update_deltas(events)
    assert not _tool_json_leaked(deltas, prefix), (
        f"TOOL_CALL: prefix tool JSON leaked. Deltas: {deltas}"
    )
