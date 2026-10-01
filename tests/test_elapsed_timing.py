from __future__ import annotations

import asyncio
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.providers.base import ChatResult


class _Loader:
    def get_system_prompt(self, **_: object) -> str:
        return "test"


def _agent(tmp_path: Path) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "test")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    return AgentSession(SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, _Loader(), model, "off")


class _Chat:
    def __init__(self, clock: list[float], advance: float, error: BaseException | None = None) -> None:
        self.clock, self.advance, self.error = clock, advance, error

    async def chat(self, **_: object) -> ChatResult:
        self.clock[0] += self.advance
        if self.error:
            raise self.error
        return ChatResult(text="ok", usage={}, stop_reason="stop", raw={})


class _SyncFailChat:
    def __init__(self, clock: list[float], error: BaseException) -> None:
        self.clock, self.error = clock, error

    def chat(self, **_: object) -> ChatResult:
        self.clock[0] += .125
        raise self.error


def _provider_entries(agent: AgentSession) -> list[dict[str, object]]:
    return [entry for entry in agent.session_manager.get_entries() if entry.get("scope") == "provider_request"]


def test_lifecycle_timing_stats_current_branch_validation_and_reused_tool_id(tmp_path: Path) -> None:
    manager = SessionManager.in_memory(str(tmp_path))
    manager.append_timing("turn", 0, "success")
    manager.append_timing("attempt", 10, "error")
    manager.append_timing("provider_request", 20, "timeout")
    manager.append_timing("tool", 5, "success", toolCallId="reused")
    manager.append_timing("tool", 15, "error", toolCallId="reused")
    # Historical or malformed JSONL must not manufacture measurements.
    parent = manager.get_leaf_id()
    assert parent
    for elapsed, outcome in ((True, "success"), (-1, "success"), (math.inf, "success"), (1, "future")):
        manager._append({"type": "timing", "id": f"bad-{len(manager.get_entries())}", "parentId": parent,
                         "scope": "tool", "elapsedMs": elapsed, "outcome": outcome})
        parent = manager.get_leaf_id()
    stats = manager.get_lifecycle_timing_stats()
    assert stats["turn"] == {
        "measurements": 1, "totalMs": 0, "averageMs": 0, "minMs": 0, "maxMs": 0, "lastMs": 0,
        "byOutcome": {"success": 1, "error": 0, "timeout": 0, "cancelled": 0, "rejected": 0, "budget_exceeded": 0},
    }
    assert stats["tool"]["measurements"] == 2
    assert {key: stats["tool"][key] for key in ("totalMs", "averageMs", "minMs", "maxMs", "lastMs")} == {
        "totalMs": 20, "averageMs": 10, "minMs": 5, "maxMs": 15, "lastMs": 15,
    }
    assert stats["tool"]["byOutcome"]["success"] == 1
    assert stats["tool"]["byOutcome"]["error"] == 1
    assert stats["attempt"]["byOutcome"]["error"] == 1
    assert stats["provider_request"]["byOutcome"]["timeout"] == 1


def test_lifecycle_timing_stats_excludes_sibling_and_survives_reload(tmp_path: Path) -> None:
    directory = tmp_path / "sessions"
    manager = SessionManager(str(tmp_path), str(directory), None, True)
    shared = manager.append_timing("tool", 10, "success")
    compaction = manager.append_compaction("summary", shared, 0)
    manager.append_timing("tool", 99, "error")
    manager.branch(compaction)
    manager.append_timing("tool", 20, "timeout")
    path = manager.session_file
    assert path
    reloaded = SessionManager(str(tmp_path), str(directory), path, True)
    tool = reloaded.get_lifecycle_timing_stats()["tool"]
    assert (tool["measurements"], tool["totalMs"], tool["lastMs"]) == (2, 30, 20)


def test_session_stats_exposes_lifecycle_timing_without_changing_time(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    before = agent.get_session_stats()["time"]
    assert all(scope["measurements"] == 0 and scope["totalMs"] == 0 for scope in agent.get_session_stats()["elapsedTiming"].values())
    agent.session_manager.append_timing("tool", 0, "success")
    stats = agent.get_session_stats()
    assert set(stats["time"]) == set(before)
    assert stats["time"]["responseMeasurements"] == before["responseMeasurements"]
    assert stats["elapsedTiming"]["tool"]["measurements"] == 1


def test_lifecycle_timing_stats_ignores_unhashable_and_nonfinite_legacy_values(tmp_path: Path) -> None:
    manager = SessionManager.in_memory(str(tmp_path))
    parent = None
    for scope, elapsed, outcome in (
        ([], 1, "success"), ({"tool": "x"}, 1, "success"), ("tool", None, "success"),
        ("tool", "1", "success"), ("tool", math.nan, "success"), ("tool", 1, None),
        ("tool", 1e308, "success"), ("tool", 1e308, "success"),
    ):
        entry_id = f"legacy-{len(manager.get_entries())}"
        manager._append({"type": "timing", "id": entry_id, "parentId": parent, "scope": scope,
                         "elapsedMs": elapsed, "outcome": outcome})
        parent = entry_id
    stats = manager.get_lifecycle_timing_stats()
    # The second near-maximum value is ignored rather than making totalMs inf.
    assert stats["tool"]["measurements"] == 1
    assert math.isfinite(stats["tool"]["totalMs"])
    assert stats["turn"] == {
        "measurements": 0, "totalMs": 0, "averageMs": None, "minMs": None, "maxMs": None,
        "lastMs": None,
        "byOutcome": {"success": 0, "error": 0, "timeout": 0, "cancelled": 0, "rejected": 0, "budget_exceeded": 0},
    }


def _prompt_agent(tmp_path: Path, settings: dict[str, Any] | None = None, *, tools: list[str] | None = None) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "test")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    return AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(settings or {}), registry,
        _Loader(), model, "off", tools=tools,
    )


class _TimedPromptProvider:
    def __init__(self, clock: list[float], responses: list[Any], advance: float = .125) -> None:
        self.clock, self.responses, self.advance = clock, responses, advance
        self.calls = 0
        self.requests: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]], **_: object) -> ChatResult:
        self.requests.append(messages)
        self.clock[0] += self.advance
        response = self.responses[self.calls]
        self.calls += 1
        if isinstance(response, BaseException):
            raise response
        return response if isinstance(response, ChatResult) else ChatResult(text=str(response), usage={}, stop_reason="stop", raw={})


class _FatalProviderError(BaseException):
    pass


@pytest.mark.asyncio
async def test_provider_success_runtime_pair_and_duration(tmp_path: Path) -> None:
    agent, clock = _agent(tmp_path), [0.0]
    agent._elapsed_clock = lambda: clock[0]
    agent.providers = {"openai": _Chat(clock, .250)}  # type: ignore[assignment]
    events: list[dict[str, object]] = []
    agent.subscribe(events.append)
    await agent._invoke_provider([])
    assert [e["type"] for e in events] == ["provider_request_start", "provider_request_end"]
    assert events[-1]["elapsedMs"] == 250
    assert _provider_entries(agent)[0]["outcome"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "outcome"), [(RuntimeError("private"), "error"), (TimeoutError(), "timeout")])
async def test_provider_runtime_error_and_timeout_are_durable(tmp_path: Path, error: BaseException, outcome: str) -> None:
    agent, clock = _agent(tmp_path), [0.0]
    agent._elapsed_clock = lambda: clock[0]
    agent.providers = {"openai": _Chat(clock, .125, error)}  # type: ignore[assignment]
    with pytest.raises((RuntimeError, TimeoutError)):
        await agent._invoke_provider([])
    entry = _provider_entries(agent)[0]
    assert entry["elapsedMs"] == 125 and entry["outcome"] == outcome and "private" not in entry.values()


@pytest.mark.asyncio
async def test_provider_runtime_cancellation_cleans_active_tasks(tmp_path: Path) -> None:
    agent, clock = _agent(tmp_path), [0.0]
    agent._elapsed_clock = lambda: clock[0]
    agent.providers = {"openai": _Chat(clock, .125, asyncio.CancelledError())}  # type: ignore[assignment]
    with pytest.raises(asyncio.CancelledError):
        await agent._invoke_provider([])
    assert _provider_entries(agent)[0]["outcome"] == "cancelled"
    assert not agent._active_chat_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), asyncio.CancelledError()])
async def test_synchronous_provider_construction_failures_are_paired(tmp_path: Path, error: BaseException) -> None:
    agent, clock = _agent(tmp_path), [0.0]
    agent._elapsed_clock = lambda: clock[0]
    agent.providers = {"openai": _SyncFailChat(clock, error)}  # type: ignore[assignment]
    expected = RuntimeError if isinstance(error, TimeoutError) else asyncio.CancelledError
    with pytest.raises(expected):
        await agent._invoke_provider([])
    entry = _provider_entries(agent)[0]
    assert entry["elapsedMs"] == 125
    assert entry["outcome"] == ("timeout" if isinstance(error, TimeoutError) else "cancelled")


@pytest.mark.asyncio
@pytest.mark.parametrize(("advance", "ok"), [(.125, True), (.250, False)])
async def test_run_tool_call_runtime_durations(tmp_path: Path, advance: float, ok: bool) -> None:
    agent, clock = _agent(tmp_path), [0.0]
    agent._elapsed_clock = lambda: clock[0]

    async def execute(*_: object, **__: object) -> dict[str, object]:
        clock[0] += advance
        return {"ok": ok, "output": "x", "error": None if ok else "failed"}

    agent._execute_tool_by_name = execute  # type: ignore[method-assign]
    await agent._run_tool_call("read", {}, tool_call_id="same")
    entry = [e for e in agent.session_manager.get_entries() if e.get("scope") == "tool"][0]
    assert entry["elapsedMs"] == int(advance * 1000)


@pytest.mark.asyncio
@pytest.mark.parametrize(("flag", "outcome"), [("timedOut", "timeout"), ("cancelled", "cancelled")])
async def test_run_tool_call_returned_timeout_and_cancel_are_timed(tmp_path: Path, flag: str, outcome: str) -> None:
    agent, clock = _agent(tmp_path), [0.0]
    agent._elapsed_clock = lambda: clock[0]

    async def execute(*_: object, **__: object) -> dict[str, object]:
        clock[0] += .125
        return {"ok": False, "output": "stopped", flag: True}

    agent._execute_tool_by_name = execute  # type: ignore[method-assign]
    await agent._run_tool_call("read", {}, tool_call_id=flag)
    entry = next(e for e in agent.session_manager.get_entries() if e.get("scope") == "tool")
    assert (entry["elapsedMs"], entry["outcome"]) == (125, outcome)


@pytest.mark.asyncio
async def test_run_tool_call_extension_denial_is_rejected_zero_and_cleans_timer(tmp_path: Path) -> None:
    agent, clock = _agent(tmp_path), [0.0]
    agent._elapsed_clock = lambda: clock[0]

    class _Extension:
        def has_hooks(self, hook: str) -> bool:
            return hook == "tool.execute.before"

    async def deny(*_: object) -> tuple[dict[str, Any], str]:
        clock[0] += .125
        return {}, "extension denied"

    agent._extension_runtime = _Extension()  # type: ignore[assignment]
    agent._invoke_extension_before_tool = deny  # type: ignore[method-assign]
    await agent._run_tool_call("read", {"path": "a"}, tool_call_id="denied")
    entry = next(e for e in agent.session_manager.get_entries() if e.get("scope") == "tool")
    assert (entry["elapsedMs"], entry["outcome"]) == (0, "rejected")
    assert not agent._elapsed_tool_started_at


@pytest.mark.asyncio
async def test_run_tool_call_successful_extension_hook_time_is_included(tmp_path: Path) -> None:
    agent, clock = _agent(tmp_path), [0.0]
    agent._elapsed_clock = lambda: clock[0]

    class _Extension:
        def has_hooks(self, hook: str) -> bool:
            return hook == "tool.execute.before"

    async def allow(_tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], None]:
        clock[0] += .125
        return args, None

    async def execute(*_: object, **__: object) -> dict[str, object]:
        clock[0] += .125
        return {"ok": True, "output": "read"}

    agent._extension_runtime = _Extension()  # type: ignore[assignment]
    agent._invoke_extension_before_tool = allow  # type: ignore[method-assign]
    agent._execute_tool_by_name = execute  # type: ignore[method-assign]
    await agent._run_tool_call("read", {}, tool_call_id="allowed")
    entry = next(e for e in agent.session_manager.get_entries() if e.get("scope") == "tool")
    assert (entry["elapsedMs"], entry["outcome"]) == (250, "success")


@pytest.mark.asyncio
async def test_run_tool_call_reused_native_id_in_one_turn_has_independent_timings(tmp_path: Path) -> None:
    agent, clock = _agent(tmp_path), [0.0]
    agent._elapsed_clock = lambda: clock[0]
    advances = iter([.125, .250])

    async def execute(*_: object, **__: object) -> dict[str, object]:
        clock[0] += next(advances)
        return {"ok": True, "output": "read"}

    agent._execute_tool_by_name = execute  # type: ignore[method-assign]
    await agent._run_tool_call("read", {}, tool_call_id="native-id")
    await agent._run_tool_call("read", {}, tool_call_id="native-id")
    entries = [e for e in agent.session_manager.get_entries() if e.get("scope") == "tool"]
    assert [(e["toolCallId"], e["elapsedMs"]) for e in entries] == [("native-id", 125), ("native-id", 250)]
    assert not agent._elapsed_tool_started_at


def test_timing_entries_are_durable_whitelisted_and_not_context(tmp_path: Path) -> None:
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "hello"})
    timing_id = sm.append_timing(
        "provider_request", math.inf, "success", provider="openai", model="m",
        providerRequestId="request", prompt="must not persist",
    )
    timing = next(entry for entry in sm.get_entries() if entry["id"] == timing_id)
    assert timing["elapsedMs"] == 0
    assert "prompt" not in timing
    assert [message["content"] for message in sm.build_session_context()["messages"]] == ["hello"]
    with pytest.raises(ValueError):
        sm.append_timing("bad", 1, "success")
    with pytest.raises(ValueError):
        sm.append_timing("turn", 1, "bad")

    reopened = SessionManager.open(str(sm.session_file), str(tmp_path))
    assert any(entry.get("type") == "timing" for entry in reopened.get_entries())
    assert len(reopened.build_session_context()["messages"]) == 1


def test_reused_tool_call_id_records_each_invocation_and_terminal_events_are_stamped(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    now = [1.0]
    agent._elapsed_clock = lambda: now[0]
    events: list[dict[str, object]] = []
    agent.subscribe(events.append)

    agent._emit({"type": "agent_start", "turnId": "turn-1"})
    agent._emit({"type": "turn_start", "attempt": 1})
    agent._elapsed_tool_started_at.setdefault("native-id", []).append(now[0])
    now[0] += 0.125
    agent._emit({"type": "tool_call_end", "tool": "read", "toolCallId": "native-id", "ok": True})
    agent._elapsed_tool_started_at.setdefault("native-id", []).append(now[0])
    now[0] += 0.250
    agent._emit({"type": "tool_call_end", "tool": "read", "toolCallId": "native-id", "ok": False})
    agent._emit({"type": "turn_end", "attempt": 1, "ok": False, "reason": "error", "willRetry": True})
    # A backoff abort has no started attempt but remains visibly stamped and
    # must not duplicate the completed attempt timing entry.
    now[0] += 1
    agent._emit({"type": "turn_end", "attempt": 2, "ok": True, "reason": "abort", "aborted": True})
    agent._emit({"type": "agent_end", "messages": []})

    ends = [event for event in events if event["type"] == "tool_call_end"]
    assert [event["elapsedMs"] for event in ends] == [125, 250]
    turns = [event for event in events if event["type"] == "turn_end"]
    assert [event["elapsedMs"] for event in turns] == [375, 0]
    timing = [entry for entry in agent.session_manager.get_entries() if entry.get("type") == "timing"]
    assert [entry["elapsedMs"] for entry in timing if entry["scope"] == "tool"] == [125, 250]
    assert len([entry for entry in timing if entry["scope"] == "attempt"]) == 1
    assert [entry["outcome"] for entry in timing if entry["scope"] == "turn"] == ["cancelled"]


@pytest.mark.asyncio
async def test_prompt_retry_timings_are_correlated_and_exclude_provider_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, clock = _prompt_agent(tmp_path, {"retry": {"enabled": True, "maxRetries": 1, "baseDelayMs": 500, "maxDelayMs": 500}}, tools=[]), [0.0]
    provider = _TimedPromptProvider(clock, [RuntimeError("temporary"), "recovered " * 50])
    agent._elapsed_clock = lambda: clock[0]
    agent.providers = {"openai": provider}  # type: ignore[assignment]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("retry", {"requestId": "rpc-parent"})

    starts = [e for e in events if e["type"] == "provider_request_start"]
    ends = [e for e in events if e["type"] == "provider_request_end"]
    assert len(starts) == len(ends) == 2
    assert len({e["providerRequestId"] for e in starts}) == 2
    assert all(e["parentRequestId"] == "rpc-parent" for e in starts)
    assert [e["elapsedMs"] for e in ends] == [125, 125]
    assert [e["elapsedMs"] for e in events if e["type"] == "turn_end"] == [125, 125]
    assert next(e for e in events if e["type"] == "agent_end")["elapsedMs"] == 750
    timing = [e for e in agent.session_manager.get_entries() if e.get("type") == "timing"]
    assert [e["scope"] for e in timing] == ["provider_request", "attempt", "provider_request", "attempt", "turn"]
    assert all("telemetry" not in str(request) for request in provider.requests)
    assert agent._active_attempt is None and not agent._elapsed_attempt_started_at and not agent._elapsed_tool_started_at


@pytest.mark.asyncio
async def test_provider_start_listener_mutation_cannot_corrupt_terminal_correlation(tmp_path: Path) -> None:
    agent, clock = _prompt_agent(tmp_path, tools=[]), [0.0]
    agent._elapsed_clock = lambda: clock[0]
    agent.providers = {"openai": _TimedPromptProvider(clock, ["done"])}  # type: ignore[assignment]
    original_start: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []

    def corrupt_start(event: dict[str, Any]) -> None:
        if event["type"] == "provider_request_start":
            original_start.append(dict(event))
            event.update({"requestId": "corrupt", "providerRequestId": "corrupt", "provider": "bad", "model": "bad", "turnId": "bad"})

    agent.subscribe(corrupt_start)
    agent.subscribe(observed.append)
    await agent.prompt("prompt", {"requestId": "parent"})
    start = original_start[0]
    end = next(event for event in observed if event["type"] == "provider_request_end")
    timing = _provider_entries(agent)[0]
    assert end["providerRequestId"] == start["providerRequestId"]
    assert (end["provider"], end["model"], end["turnId"]) == (start["provider"], start["model"], start["turnId"])
    assert (timing["providerRequestId"], timing["provider"], timing["model"], timing["turnId"]) == (
        start["providerRequestId"], start["provider"], start["model"], start["turnId"],
    )


@pytest.mark.asyncio
async def test_prompt_fatal_second_attempt_records_active_attempt_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, clock = _prompt_agent(tmp_path, {"retry": {"enabled": True, "maxRetries": 1, "baseDelayMs": 1, "maxDelayMs": 1}}, tools=[]), [0.0]
    agent._elapsed_clock = lambda: clock[0]
    agent.providers = {"openai": _TimedPromptProvider(clock, [RuntimeError("retry"), _FatalProviderError()])}  # type: ignore[assignment]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(_FatalProviderError):
        await agent.prompt("fatal")
    timing = [entry for entry in agent.session_manager.get_entries() if entry.get("type") == "timing"]
    assert [(entry["scope"], entry.get("attempt"), entry["outcome"]) for entry in timing] == [
        ("provider_request", 1, "error"), ("attempt", 1, "error"),
        ("provider_request", 2, "error"), ("attempt", 2, "error"), ("turn", None, "error"),
    ]
    assert agent._active_attempt is None and not agent._elapsed_attempt_started_at


@pytest.mark.asyncio
async def test_prompt_abort_during_retry_backoff_records_one_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent, clock = _prompt_agent(tmp_path, {"retry": {"enabled": True, "maxRetries": 1, "baseDelayMs": 500, "maxDelayMs": 500}}, tools=[]), [0.0]
    agent._elapsed_clock = lambda: clock[0]
    agent.providers = {"openai": _TimedPromptProvider(clock, [RuntimeError("retry")])}  # type: ignore[assignment]

    async def aborting_sleep(seconds: float) -> None:
        clock[0] += seconds
        if clock[0] >= .625:
            agent._abort_requested = True

    monkeypatch.setattr(asyncio, "sleep", aborting_sleep)
    await agent.prompt("abort")
    timing = [entry for entry in agent.session_manager.get_entries() if entry.get("type") == "timing"]
    assert [(entry["scope"], entry.get("attempt"), entry["elapsedMs"], entry["outcome"]) for entry in timing] == [
        ("provider_request", 1, 125, "error"), ("attempt", 1, 125, "error"), ("turn", None, 625, "cancelled"),
    ]
    assert agent._active_attempt is None and not agent._elapsed_attempt_started_at


@pytest.mark.asyncio
@pytest.mark.parametrize(("approved", "expected_tool", "expected_turn"), [(True, 125, 875), (False, 0, 750)])
async def test_prompt_approval_timings_exclude_human_wait(
    tmp_path: Path, approved: bool, expected_tool: int, expected_turn: int,
) -> None:
    agent, clock = _prompt_agent(tmp_path, {"tools": {"maxSteps": 3}}, tools=["write", "finish"]), [0.0]
    native = ChatResult(text="", usage={}, stop_reason="stop", raw={}, had_native_tool_call=True,
                        native_tool_calls=[{"id": "write-1", "name": "write", "arguments": {"path": "blocked.txt", "content": "x"}}])
    agent.providers = {"openai": _TimedPromptProvider(clock, [native, '{"tool":"finish","args":{"summary":"done","goal_success":true}}'])}  # type: ignore[assignment]
    agent._elapsed_clock = lambda: clock[0]

    async def approve(*_: object) -> tuple[bool, str]:
        clock[0] += .5
        return approved, "no" if not approved else ""

    async def execute(tool_name: str, *_: object, **__: object) -> dict[str, object]:
        if tool_name == "write":
            clock[0] += .125
        return {"ok": True, "output": "written"}

    agent.approval_callback = approve
    agent._execute_tool_by_name = execute  # type: ignore[method-assign]
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("write")

    tool_end = next(e for e in events if e["type"] == "tool_call_end" and e["tool"] == "write")
    assert tool_end["elapsedMs"] == expected_tool
    assert next(e for e in events if e["type"] == "agent_end")["elapsedMs"] == expected_turn
    if approved:
        assert not (tmp_path / "blocked.txt").exists()
    else:
        assert tool_end["rejected"] is True and not (tmp_path / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_native_call_ids_are_reusable_across_prompts_and_nonlive_calls_are_unscoped(tmp_path: Path) -> None:
    agent, clock = _prompt_agent(tmp_path, {"tools": {"maxSteps": 2}}, tools=["read"]), [0.0]
    calls = [
        ChatResult(text="", usage={}, stop_reason="stop", raw={}, had_native_tool_call=True,
                   native_tool_calls=[{"id": "same", "name": "read", "arguments": {"path": "a"}}]),
        "x" * 300, ChatResult(text="", usage={}, stop_reason="stop", raw={}, had_native_tool_call=True,
                              native_tool_calls=[{"id": "same", "name": "read", "arguments": {"path": "a"}}]), "y" * 300, "compact",
    ]
    provider = _TimedPromptProvider(clock, calls)
    agent.providers = {"openai": provider}  # type: ignore[assignment]
    agent._elapsed_clock = lambda: clock[0]

    async def execute(*_: object, **__: object) -> dict[str, object]:
        clock[0] += .125
        return {"ok": True, "output": "read"}

    agent._execute_tool_by_name = execute  # type: ignore[method-assign]
    await agent.prompt("one")
    await agent.prompt("two")
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent._invoke_provider([], allow_live_stream=False, purpose="compaction")
    tools = [e for e in agent.session_manager.get_entries() if e.get("scope") == "tool"]
    compaction = [e for e in agent.session_manager.get_entries() if e.get("scope") == "provider_request"][-1]
    assert [e["elapsedMs"] for e in tools] == [125, 125]
    assert "turnId" not in compaction and "attempt" not in compaction
    assert not [e for e in events if e["type"] in {"message_start", "message_update", "message_end", "thinking_start", "thinking_update", "thinking_end"}]
    assert agent._active_attempt is None and not agent._elapsed_tool_started_at


@pytest.mark.asyncio
async def test_native_batch_abort_records_cancelled_calls(tmp_path: Path) -> None:
    agent, clock = _prompt_agent(tmp_path, {"tools": {"maxSteps": 2}}, tools=["write"]), [0.0]
    native = ChatResult(text="", usage={}, stop_reason="stop", raw={}, had_native_tool_call=True, native_tool_calls=[
        {"id": "first", "name": "write", "arguments": {"path": "a", "content": "x"}},
        {"id": "second", "name": "write", "arguments": {"path": "b", "content": "x"}},
    ])
    agent.providers = {"openai": _TimedPromptProvider(clock, [native])}  # type: ignore[assignment]
    agent._elapsed_clock = lambda: clock[0]

    async def execute(*_: object, **__: object) -> dict[str, object]:
        clock[0] += .125
        agent._abort_requested = True
        return {"ok": True, "output": "written"}

    agent._execute_tool_by_name = execute  # type: ignore[method-assign]
    await agent.prompt("batch")
    tools = [e for e in agent.session_manager.get_entries() if e.get("scope") == "tool"]
    assert [(e["toolCallId"], e["elapsedMs"], e["outcome"]) for e in tools] == [("first", 125, "success"), ("second", 0, "cancelled")]
    assert all(e.get("turnId") == "turn-1" for e in tools)


@pytest.mark.asyncio
async def test_budget_and_capability_failures_end_as_error_turns(tmp_path: Path) -> None:
    agent, clock = _prompt_agent(tmp_path), [0.0]
    agent._elapsed_clock = lambda: clock[0]
    agent.providers = {"openai": _TimedPromptProvider(clock, ["unused"])}  # type: ignore[assignment]
    agent._budget_exceeded = lambda: ("time", "budget")  # type: ignore[method-assign]
    events: list[dict[str, Any]] = []
    agent.subscribe(events.append)
    await agent.prompt("budget")
    assert next(e for e in events if e["type"] == "agent_end")["elapsedMs"] == 0
    assert next(e for e in agent.session_manager.get_entries() if e.get("scope") == "turn")["outcome"] == "budget_exceeded"

    capability = _prompt_agent(tmp_path)
    capability._images = [{"blob_hash": "x"}]
    assert capability.model is not None
    capability.model = deepcopy(capability.model)
    capability.model.input_image = False
    capability_events: list[dict[str, Any]] = []
    capability.subscribe(capability_events.append)
    await capability.prompt("image")
    assert [e["type"] for e in capability_events if e["type"] == "agent_end"] == ["agent_end"]
    assert next(e for e in capability.session_manager.get_entries() if e.get("scope") == "turn")["outcome"] == "error"


def test_timing_survives_branch_reload_export_compaction_and_old_sessions(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sm = SessionManager.create(str(tmp_path), str(sessions))
    first = sm.append_message({"role": "user", "content": "u"})
    sm.append_timing("turn", 125, "success", turnId="turn-1")
    leaf = sm.append_message({"role": "assistant", "content": "a"})
    path = sm.create_branched_session(leaf)
    assert path is not None
    reopened = SessionManager.open(path, str(sessions))
    reopened.append_compaction("summary", first, tokens_before=2)
    exported = reopened.export_to_jsonl(str(tmp_path / "export.jsonl"))
    reexported = SessionManager.open(exported, str(sessions))
    assert any(e.get("type") == "timing" for e in reexported.get_entries())
    assert any(e.get("type") == "timing" for e in reexported.get_branch())
    assert all(m.get("type") != "timing" for m in reexported.build_session_context()["messages"])
    old = SessionManager.create(str(tmp_path), str(sessions))
    old.append_message({"role": "user", "content": "old"})
    assert SessionManager.open(str(old.session_file), str(sessions)).build_session_context()["messages"][0]["content"] == "old"


@pytest.mark.asyncio
async def test_prompt_nudge_and_repair_provider_purposes_are_timed(tmp_path: Path) -> None:
    clock = [0.0]
    nudge = _prompt_agent(tmp_path, tools=["finish"])
    nudge._elapsed_clock = lambda: clock[0]
    nudge.providers = {"openai": _TimedPromptProvider(clock, ["I will do it.", '{"tool":"finish","args":{"summary":"done","goal_success":true}}'])}  # type: ignore[assignment]
    await nudge.prompt("nudge")
    assert [(e["purpose"], e["elapsedMs"]) for e in _provider_entries(nudge)] == [("response", 125), ("nudge", 125)]

    repair = _prompt_agent(tmp_path, {"tools": {"maxSteps": 3}}, tools=["read", "finish"])
    repair._elapsed_clock = lambda: clock[0]
    repair.providers = {"openai": _TimedPromptProvider(clock, ['{"tool":"read","args":{"path":"a"}}', "", '{"tool":"finish","args":{"summary":"done","goal_success":true}}'])}  # type: ignore[assignment]
    async def execute(*_: object, **__: object) -> dict[str, object]:
        return {"ok": True, "output": "read"}
    repair._execute_tool_by_name = execute  # type: ignore[method-assign]
    await repair.prompt("repair")
    assert [(e["purpose"], e["elapsedMs"]) for e in _provider_entries(repair)] == [("response", 125), ("response", 125), ("repair", 125)]
