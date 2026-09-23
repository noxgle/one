from __future__ import annotations

import builtins
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager

# ---------------------------------------------------------------------------
# Helpers copied from tests/test_rpc_mode.py
# ---------------------------------------------------------------------------

class _FakeLoader:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd

    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."

    def get_extensions(self) -> dict[str, Any]:
        return {"extensions": [{"path": "/tmp/fake.py"}], "errors": [], "runtime": {}}

    def get_skills(self) -> dict[str, Any]:
        return {"skills": [{"name": "analiza", "filePath": "/tmp/SKILL.md"}], "diagnostics": []}

    def get_prompts(self) -> dict[str, Any]:
        return {"prompts": [{"name": "review", "description": "d", "source": "/tmp/p.md"}], "diagnostics": []}

    def get_themes(self) -> dict[str, Any]:
        return {"themes": [{"path": "/tmp/theme.json"}], "diagnostics": []}

    def get_agents_files(self) -> dict[str, Any]:
        return {"agentsFiles": [{"path": "/tmp/AGENTS.md", "content": "x"}]}


class _RuntimeHost:
    def __init__(self, session: AgentSession) -> None:
        self.session = session

    async def new_session(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"cancelled": False}

    async def import_from_jsonl(self, path: str) -> dict[str, Any]:  # noqa: ARG002
        return {"cancelled": False}

    async def switch_session(self, path: str) -> dict[str, Any]:  # noqa: ARG002
        return {"cancelled": False}

    async def fork(self, entry_id: str) -> dict[str, Any]:  # noqa: ARG002
        return {"cancelled": False, "selectedText": None}


def _mk_session(tmp_path: Path, retry_settings: dict | None = None) -> AgentSession:
    auth = AuthStorage.in_memory()
    # NOTE: no runtime API key here — a runtime key would shadow stored keys
    # in get_provider_auth_status() and break login/logout assertions.
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    settings_dict = {"tools": {"maxSteps": 4, "timeoutSec": 5}}
    if retry_settings:
        settings_dict["retry"] = retry_settings
    settings = SettingsManager.in_memory(settings_dict)
    session_manager = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session_manager, settings, registry, _FakeLoader(str(tmp_path)), model, "medium")


async def _run_rpc(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session: AgentSession,
    lines: list[str],
) -> tuple[list[dict[str, Any]], str]:
    """Feed JSON-lines to run_rpc_mode and return parsed responses + raw output text."""
    from one.modes.rpc_mode import run_rpc_mode

    feed = iter(lines + [None])

    def fake_input(*args: Any) -> str:  # noqa: ARG001
        try:
            nxt = next(feed)
        except StopIteration:
            raise EOFError from None
        if nxt is None:
            raise EOFError
        return nxt

    monkeypatch.setattr(builtins, "input", fake_input)
    try:
        await run_rpc_mode(_RuntimeHost(session))
    except EOFError:
        # run_rpc_mode only catches EOFError inside command dispatch; EOF on
        # stdin terminates the loop (same as in the real CLI).
        pass
    out = capsys.readouterr().out
    return ([json.loads(l) for l in out.splitlines() if l.strip()], out)


# ---------------------------------------------------------------------------
# Providers (mirrors test_event_snapshots.py pattern)
# ---------------------------------------------------------------------------

class _Provider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
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
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


class _AlwaysFailProvider:
    def __init__(self) -> None:
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
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Golden helper
# ---------------------------------------------------------------------------

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots" / "rpc"


def _check_snapshot(name: str, text: str) -> None:
    path = SNAPSHOT_DIR / f"{name}.jsonl"
    if os.environ.get("ONE_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    if not path.exists():
        pytest.fail(f"missing snapshot {path} — run with ONE_UPDATE_SNAPSHOTS=1 to create it")
    assert path.read_text(encoding="utf-8") == text, f"RPC snapshot mismatch: {path} (run with ONE_UPDATE_SNAPSHOTS=1 to update)"


# ---------------------------------------------------------------------------
# Normalisation — removes non-deterministic keys from event payloads
# ---------------------------------------------------------------------------

def _strip_timestamps(obj: Any) -> Any:
    """Recursively remove 'timestamp' keys from dicts and lists."""
    if isinstance(obj, dict):
        return {k: _strip_timestamps(v) for k, v in obj.items() if k != "timestamp"}
    if isinstance(obj, list):
        return [_strip_timestamps(item) for item in obj]
    return obj


def _normalize_event(obj: dict[str, Any]) -> dict[str, Any]:
    """Strip timestamp / message / messages so snapshots are deterministic."""
    if obj.get("type") == "turn_end":
        # turn_end carries a full message object with a timestamp
        cleaned = {k: v for k, v in obj.items() if k != "message"}
    elif obj.get("type") == "agent_end":
        # agent_end carries a messages array with timestamps
        cleaned = {k: v for k, v in obj.items() if k != "messages"}
    else:
        cleaned = dict(obj)

    # Recursively strip all nested timestamps (e.g. message.timestamp,
    # tool_results[].timestamp, etc.) and top-level timestamp.
    cleaned = _strip_timestamps(cleaned)
    return cleaned


def _snapshot_text(output: str) -> str:
    lines = output.splitlines()
    normed = []
    for line in lines:
        if line.strip():
            obj = json.loads(line)
            normed.append(json.dumps(_normalize_event(obj), ensure_ascii=False, sort_keys=True))
    return "\n".join(normed) + "\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rpc_conversation_snapshot_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    session = _mk_session(tmp_path)
    # Provide an API key so the provider is actually invoked (mirrors
    # test_event_snapshots / test_tool_calling pattern).
    session.model_registry.set_stored_api_key("openai", "dummy")

    # Create the file the provider's tool call will read.  The session's cwd
    # is tmp_path (via SessionManager.in_memory(str(tmp_path))).
    test_file = tmp_path / "a.txt"
    test_file.write_text("hello world\n", encoding="utf-8")

    # Provider script mirrors test_event_snapshots success:
    #   response 1 → tool call (read succeeds),
    #   response 2 → valid finish tool call (terminal).
    session.providers = {"openai": _Provider([
        '{"tool":"read","args":{"path":"a.txt"}}',
        '{"tool":"finish","args":{"summary":"read completed","goal_success":true}}',
    ])}

    responses, raw = await _run_rpc(
        monkeypatch, capsys, session,
        [
            json.dumps({"type": "prompt", "id": "p1", "message": "check the repo"}),
            json.dumps({"type": "wait_for_idle", "id": "w1"}),
        ],
    )

    # Sanity: command responses are returned
    prompt_resp = [r for r in responses if r.get("type") == "response" and r.get("command") == "prompt" and r.get("id") == "p1"]
    assert prompt_resp and prompt_resp[0]["success"] is True
    idle_resp = [r for r in responses if r.get("type") == "response" and r.get("command") == "wait_for_idle" and r.get("id") == "w1"]
    assert idle_resp and idle_resp[0]["success"] is True

    _check_snapshot("conversation_success", _snapshot_text(raw))


@pytest.mark.asyncio
async def test_rpc_conversation_snapshot_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    # Explicit retry settings (maxRetries=3). The always-fail provider
    # exhausts all retries and the session terminates with an error.
    session = _mk_session(tmp_path, retry_settings={"maxRetries": 3, "baseDelayMs": 5, "maxDelayMs": 50})
    session.model_registry.set_stored_api_key("openai", "dummy")
    session.providers = {"openai": _AlwaysFailProvider()}

    responses, raw = await _run_rpc(
        monkeypatch, capsys, session,
        [
            json.dumps({"type": "prompt", "id": "p2", "message": "do something"}),
            json.dumps({"type": "wait_for_idle", "id": "w2"}),
        ],
    )

    prompt_resp = [r for r in responses if r.get("type") == "response" and r.get("command") == "prompt" and r.get("id") == "p2"]
    assert prompt_resp and prompt_resp[0]["success"] is True

    _check_snapshot("conversation_error", _snapshot_text(raw))
