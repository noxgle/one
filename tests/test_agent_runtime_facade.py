# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from one.core.agent_runtime_facade import AgentRuntimeFacade, RuntimeBootstrapSpec
from one.core.threaded_agent_runtime import RuntimeClosedError


class _Session:
    def __init__(self) -> None:
        self.listeners: list[Any] = []
        self.subagent_monitor = _Monitor()
        self.aborted = False

    def subscribe(self, listener: Any) -> Any:
        self.listeners.append(listener)

        def off() -> None:
            self.listeners.remove(listener)

        return off

    def emit(self, event: dict[str, Any]) -> None:
        for listener in self.listeners:
            listener(event)

    async def abort(self) -> None:
        self.aborted = True


class _Monitor:
    def __init__(self) -> None:
        self.listeners: list[Any] = []

    def subscribe(self, listener: Any) -> Any:
        self.listeners.append(listener)

        def off() -> None:
            self.listeners.remove(listener)

        return off

    def emit(self, notice: dict[str, Any]) -> None:
        for listener in self.listeners:
            listener(notice)


class _Runtime:
    def __init__(self) -> None:
        self.session = _Session()
        self.owner_thread = threading.get_ident()
        self.disposed = False
        self.wait_cancelled = False
        self.cancelled = threading.Event()
        self.calls: list[str] = []
        self.not_a_command = "not callable"

    async def work(self, value: str, delay: float = 0) -> str:
        self.calls.append(value)
        if delay:
            await asyncio.sleep(delay)
        self.session.emit({"type": "message_update", "value": value, "token": "do-not-send"})
        return value

    async def echo(self, data: dict[str, Any]) -> dict[str, Any]:
        self.last_data = data
        return data

    async def large_result(self) -> str:
        return "x" * 128

    async def wait_forever(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.wait_cancelled = True
            self.cancelled.set()
            raise

    async def dispose(self) -> None:
        self.disposed = True


def _spec() -> RuntimeBootstrapSpec:
    return RuntimeBootstrapSpec(cwd="/tmp", agent_dir="/tmp/agent", identifiers={"session": "s1"})


@pytest.mark.asyncio
async def test_start_command_events_and_shutdown_stay_on_their_owners() -> None:
    created: list[_Runtime] = []

    async def factory(spec: RuntimeBootstrapSpec) -> _Runtime:
        assert spec.identifiers == {"session": "s1"}
        runtime = _Runtime()
        created.append(runtime)
        return runtime

    ui_thread = threading.get_ident()
    events: list[tuple[int, dict[str, Any]]] = []
    facade = AgentRuntimeFacade(_spec(), factory, command_allowlist={"work"})
    facade.subscribe(lambda event: events.append((threading.get_ident(), event)))
    await facade.start()
    reply = await facade.command("work", {"value": "one"}, correlation_id="request-1")
    await asyncio.sleep(0)

    assert reply == {"correlationId": "request-1", "result": "one"}
    assert created[0].owner_thread != ui_thread
    assert events and events[0][0] == ui_thread
    assert events[0][1]["sequence"] == 1
    assert "token" not in events[0][1]

    await facade.close()
    assert created[0].session.aborted and created[0].disposed
    assert facade.finalize()


@pytest.mark.asyncio
async def test_facade_preserves_safe_rejected_tool_diagnostics() -> None:
    created: list[_Runtime] = []

    def factory(_spec: RuntimeBootstrapSpec) -> _Runtime:
        runtime = _Runtime()
        created.append(runtime)
        return runtime

    facade = AgentRuntimeFacade(_spec(), factory)
    events: list[dict[str, Any]] = []
    facade.subscribe(events.append)
    await facade.start()
    created[0].session.emit({
        "type": "tool_call_parse_failed",
        "source": "format_repair",
        "category": "malformed_json",
        "reason": "untrusted raw candidate must not cross",
        "tool": "bash",
        "toolCallId": "call-1",
        "args": {"command": "secret"},
    })
    await asyncio.sleep(0)
    event = next(event for event in events if event["type"] == "tool_call_parse_failed")
    assert event["source"] == "format_repair"
    assert event["category"] == "malformed_json"
    assert event["tool"] == "bash"
    assert event["toolCallId"] == "call-1"
    assert "args" not in event
    await facade.close()


@pytest.mark.asyncio
async def test_startup_is_awaitable_not_blocking_and_reports_factory_error() -> None:
    entered = asyncio.Event()

    async def delayed_factory(spec: RuntimeBootstrapSpec) -> _Runtime:
        entered.set()
        await asyncio.sleep(0.03)
        return _Runtime()

    facade = AgentRuntimeFacade(_spec(), delayed_factory)
    start = asyncio.create_task(facade.start())
    await entered.wait()
    assert not start.done()
    await start
    await facade.close()
    assert facade.finalize()

    async def broken_factory(spec: RuntimeBootstrapSpec) -> _Runtime:
        raise RuntimeError("factory failed")

    broken = AgentRuntimeFacade(_spec(), broken_factory)
    with pytest.raises(RuntimeError, match="factory failed"):
        await broken.start()
    await broken.close()
    assert broken.finalize()


@pytest.mark.asyncio
async def test_correlation_cancellation_coalescing_and_idempotent_close() -> None:
    runtime = _Runtime()

    async def factory(spec: RuntimeBootstrapSpec) -> _Runtime:
        return runtime

    facade = AgentRuntimeFacade(_spec(), factory, event_capacity=3, command_allowlist={"wait_forever", "work"})
    events: list[dict[str, Any]] = []
    facade.subscribe(events.append)
    await facade.start()

    waiting = asyncio.create_task(facade.command("wait_forever", correlation_id="cancel-me"))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await asyncio.to_thread(runtime.cancelled.wait, 1)
    assert runtime.wait_cancelled

    # Delta floods coalesce, but lifecycle barriers are retained and ordered.
    for value in range(10):
        runtime.session.emit({
            "type": "message_update", "sessionId": "s", "messageId": "m",
            "assistantMessageEvent": {"type": "text_delta", "delta": str(value)},
        })
    runtime.session.emit({"type": "ask_user", "id": "approval"})
    runtime.session.emit({"type": "agent_end", "ok": True})
    await asyncio.sleep(0)
    assert [event["type"] for event in events][-2:] == ["ask_user", "agent_end"]
    assert [event["sequence"] for event in events] == sorted(event["sequence"] for event in events)

    await asyncio.gather(facade.close(), facade.close())
    with pytest.raises(RuntimeClosedError):
        await facade.command("work", {"value": "late"})
    assert runtime.session.aborted and runtime.disposed
    assert facade.finalize()


@pytest.mark.asyncio
async def test_command_allowlist_and_detached_command_dtos() -> None:
    runtime = _Runtime()

    async def factory(spec: RuntimeBootstrapSpec) -> _Runtime:
        return runtime

    facade = AgentRuntimeFacade(_spec(), factory, command_allowlist={"work", "echo", "not_a_command"})
    await facade.start()
    source = {"value": "original"}
    reply = await facade.command("work", source)
    source["value"] = "changed"
    assert runtime.calls == ["original"]
    assert reply["result"] == "original"
    nested = {"data": {"items": ["original"]}}
    detached = await facade.command("echo", nested)
    nested["data"]["items"][0] = "changed"
    detached["result"]["items"][0] = "mutated reply"
    assert runtime.last_data == {"items": ["original"]}
    with pytest.raises(ValueError, match="unknown runtime command"):
        await facade.command("_dispose_runtime")
    with pytest.raises(ValueError, match="unknown runtime command"):
        await facade.command("missing")
    with pytest.raises(ValueError, match="unavailable runtime command"):
        await facade.command("not_a_command")
    await facade.close()
    assert facade.finalize()

    limited = AgentRuntimeFacade(
        _spec(), factory, command_allowlist={"work", "large_result"}, command_dto_max_bytes=64,
    )
    await limited.start()
    with pytest.raises(ValueError, match="payload exceeds 64 bytes"):
        await limited.command("work", {"value": "x" * 128})
    with pytest.raises(ValueError, match="result exceeds 64 bytes"):
        await limited.command("large_result")
    await limited.close()
    assert limited.finalize()


@pytest.mark.asyncio
async def test_close_before_start_and_during_cancellable_factory() -> None:
    never_called = False

    async def factory(spec: RuntimeBootstrapSpec) -> _Runtime:
        nonlocal never_called
        never_called = True
        return _Runtime()

    before_start = AgentRuntimeFacade(_spec(), factory)
    await before_start.close()
    with pytest.raises(RuntimeClosedError):
        await before_start.start()
    assert not never_called and before_start.finalize(0.01)

    entered = threading.Event()
    cancelled = threading.Event()

    async def slow_factory(spec: RuntimeBootstrapSpec) -> Any:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    during_start = AgentRuntimeFacade(_spec(), slow_factory)
    start = asyncio.create_task(during_start.start())
    await asyncio.to_thread(entered.wait, 1)
    await during_start.close()
    await asyncio.to_thread(cancelled.wait, 1)
    with pytest.raises(RuntimeClosedError):
        await start
    assert during_start.finalize(0.5)


@pytest.mark.asyncio
async def test_delta_coalesces_text_and_listener_failure_is_isolated() -> None:
    facade = AgentRuntimeFacade(_spec(), lambda spec: _Runtime(), event_capacity=2)
    received: list[dict[str, Any]] = []
    # Events can arrive before subscription; subscribing must schedule their drain.
    facade._on_worker_event({"type": "text_delta", "sessionId": "s", "messageId": "m", "text": "a"})
    facade._on_worker_event({"type": "text_delta", "sessionId": "s", "messageId": "m", "text": "b"})
    facade.subscribe(lambda event: (_ for _ in ()).throw(RuntimeError("listener failure")))
    facade.subscribe(received.append)
    await asyncio.sleep(0)
    assert received[0]["text"] == "ab"
    assert received[0]["sequence"] == 2
    await facade.close()


@pytest.mark.asyncio
async def test_nested_message_and_thinking_deltas_coalesce_without_identity() -> None:
    facade = AgentRuntimeFacade(_spec(), lambda spec: _Runtime(), event_capacity=8)
    received: list[dict[str, Any]] = []
    for value in ("first streamed ", "message has more than ", "forty characters"):
        facade._on_worker_event({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": value},
        })
    facade._on_worker_event({"type": "thinking_delta", "delta": "think "})
    facade._on_worker_event({"type": "thinking_delta", "delta": "through"})
    facade.subscribe(received.append)
    await asyncio.sleep(0)

    assert [event["type"] for event in received] == ["message_update", "thinking_delta"]
    assert received[0]["assistantMessageEvent"] == {
        "type": "text_delta", "delta": "first streamed message has more than forty characters",
    }
    assert received[0]["sequence"] == 3
    assert received[1]["delta"] == "think through"
    assert received[1]["sequence"] == 5
    await facade.close()


@pytest.mark.asyncio
async def test_host_stamped_epoch_and_owner_identity_are_preserved() -> None:
    """The facade must not relabel a queued old session with a later UI epoch."""
    facade = AgentRuntimeFacade(_spec(), lambda spec: _Runtime(), event_capacity=4)
    received: list[dict[str, Any]] = []
    facade._on_worker_event({  # noqa: SLF001
        "type": "thinking_delta", "delta": "from the old owner", "epoch": 7, "sourceSessionId": "old-session",
    })
    facade.subscribe(received.append)
    await asyncio.sleep(0)
    assert received == [{
        "type": "thinking_delta", "delta": "from the old owner", "sourceSessionId": "old-session",
        "epoch": 7, "sequence": 1,
    }]
    await facade.close()


@pytest.mark.asyncio
async def test_readiness_backpressure_keeps_critical_events_ordered_until_resumed() -> None:
    facade = AgentRuntimeFacade(_spec(), lambda spec: _Runtime(), event_capacity=4)
    ready = False
    received: list[str] = []

    facade.subscribe(
        lambda event: received.append(event["type"]),
        can_accept_event=lambda _event: ready,
    )
    facade._on_worker_event({"type": "warning", "warning": "first"})  # noqa: SLF001
    facade._on_worker_event({"type": "agent_end", "ok": True})  # noqa: SLF001
    await asyncio.sleep(0)

    assert received == []
    assert [event[0]["type"] for event in facade._events] == ["warning", "agent_end"]  # noqa: SLF001

    ready = True
    facade.resume_events()
    await asyncio.sleep(0.01)
    assert received == ["warning", "agent_end"]
    await facade.close()


@pytest.mark.asyncio
async def test_delta_kind_and_lifecycle_barriers_are_not_coalesced() -> None:
    facade = AgentRuntimeFacade(_spec(), lambda spec: _Runtime(), event_capacity=8)
    received: list[dict[str, Any]] = []
    facade._on_worker_event({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "a"}})
    facade._on_worker_event({"type": "thinking_delta", "delta": "b"})
    facade._on_worker_event({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "c"}})
    facade._on_worker_event({"type": "turn_end", "ok": True})
    facade._on_worker_event({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "d"}})
    facade.subscribe(received.append)
    await asyncio.sleep(0)
    assert [event["type"] for event in received] == [
        "message_update", "thinking_delta", "message_update", "turn_end", "message_update",
    ]
    assert [event["sequence"] for event in received] == [1, 2, 3, 4, 5]
    await facade.close()


@pytest.mark.asyncio
async def test_ui_drain_uses_bounded_slices_and_yields_to_ready_input() -> None:
    """A flood stays in the bounded source queue between UI-loop slices."""
    facade = AgentRuntimeFacade(
        _spec(), lambda spec: _Runtime(), event_capacity=8,
        event_drain_max_items=2, event_drain_max_seconds=0.1,
    )
    received: list[dict[str, Any]] = []
    input_ran = asyncio.Event()

    def listener(event: dict[str, Any]) -> None:
        received.append(event)
        if len(received) == 2:
            asyncio.get_running_loop().call_soon(input_ran.set)

    for index in range(5):
        facade._on_worker_event({"type": "turn_start", "id": str(index)})
    facade.subscribe(listener)
    await input_ran.wait()
    # The continuation is delayed, so an input-ready callback runs before all
    # slices finish and no lifecycle barrier was moved across its neighbors.
    assert len(received) == 2
    await asyncio.sleep(0.02)
    assert [event["sequence"] for event in received] == [1, 2, 3, 4, 5]
    await facade.close()


@pytest.mark.asyncio
async def test_oversized_delta_is_explicitly_marked_and_bounded() -> None:
    facade = AgentRuntimeFacade(
        _spec(), lambda spec: _Runtime(), event_capacity=2, event_dto_max_bytes=256,
        event_queue_max_bytes=512,
    )
    facade._on_worker_event({"type": "thinking_delta", "delta": "x" * 1000})
    with facade._space:
        event, size = facade._events[0]
    assert event["deltaTruncated"] is True
    assert len(event["delta"]) < 1000
    assert size <= 256
    await facade.close()


@pytest.mark.asyncio
async def test_monitor_invalidation_is_identity_only_and_listeners_detach_on_close() -> None:
    runtime = _Runtime()
    facade = AgentRuntimeFacade(_spec(), lambda spec: runtime)
    received: list[dict[str, Any]] = []
    facade.subscribe(received.append)
    await facade.start()
    runtime.session.subagent_monitor.emit({
        "type": "updated", "sessionId": "child-1", "revision": 7,
        "children": [{"task": "raw snapshot must not cross"}],
    })
    await asyncio.sleep(0)
    assert received == [{
        "type": "subagent_monitor_invalidated", "sessionId": "child-1", "revision": 7,
        "changeType": "updated", "epoch": 0, "sequence": 1,
    }]
    await facade.close()
    assert runtime.session.listeners == []
    assert runtime.session.subagent_monitor.listeners == []
    assert facade.finalize()


@pytest.mark.asyncio
async def test_lifecycle_display_fields_are_redacted_and_control_sanitized() -> None:
    facade = AgentRuntimeFacade(_spec(), lambda spec: _Runtime(), event_capacity=8)
    received: list[dict[str, Any]] = []
    unsafe = "Bearer secret-token\x1b[31m\x00"
    facade._on_worker_event({"type": "subagent_start", "task": unsafe})
    facade._on_worker_event({"type": "ask_user", "question": unsafe})
    facade._on_worker_event({"type": "error", "error": unsafe})
    facade._on_worker_event({"type": "turn_end", "reason": unsafe})
    facade.subscribe(received.append)
    await asyncio.sleep(0)
    for event, field in zip(received, ("task", "question", "error", "reason"), strict=True):
        assert "secret-token" not in event[field]
        assert "\x1b" not in event[field]
        assert "\x00" not in event[field]
    await facade.close()


@pytest.mark.asyncio
async def test_full_queue_backpressures_critical_events_until_subscribed_drain() -> None:
    facade = AgentRuntimeFacade(_spec(), lambda spec: _Runtime(), event_capacity=2)
    facade._on_worker_event({"type": "ask_user", "id": "a"})
    facade._on_worker_event({"type": "agent_end"})
    entered = threading.Event()

    def emit_error() -> None:
        entered.set()
        facade._on_worker_event({"type": "error", "message": "must arrive"})

    producer = threading.Thread(target=emit_error)
    producer.start()
    await asyncio.to_thread(entered.wait, 1)
    assert producer.is_alive()
    received: list[dict[str, Any]] = []
    facade.subscribe(received.append)
    await asyncio.to_thread(producer.join, 1)
    assert not producer.is_alive()
    await asyncio.sleep(0)
    assert [event["type"] for event in received] == ["ask_user", "agent_end", "error"]
    await facade.close()


@pytest.mark.asyncio
async def test_event_projection_is_allowlisted_and_queue_remains_bounded() -> None:
    facade = AgentRuntimeFacade(
        _spec(), lambda spec: _Runtime(), event_capacity=6, event_dto_max_bytes=512,
        event_queue_max_bytes=1024,
    )
    raw_secret = "raw-secret-must-not-cross"
    facade._on_worker_event({
        "type": "tool_call_start", "tool": "bash", "toolCallId": "call-1", "effectiveTimeout": 30,
        "args": {"command": "printf safe", "token": raw_secret, "content": "image-body" * 1000},
        "rawResult": {"evidence": raw_secret}, "huge": {"nested": [raw_secret] * 1000},
    })
    facade._on_worker_event({
        "type": "tool_call_end", "tool": "bash", "toolCallId": "call-1", "ok": True,
        "result": {
            "outputText": "visible output", "rawResult": {"evidence": raw_secret},
            "args": {"token": raw_secret}, "content": [{"image": raw_secret}],
        }, "evidence": raw_secret, "image": raw_secret,
    })
    facade._on_worker_event({
        "type": "message_end", "message": {
            "role": "assistant", "model": "safe-model", "content": [{"text": raw_secret, "image": raw_secret}],
        }, "suppressed": True, "content": raw_secret,
    })
    facade._on_worker_event({
        "type": "tool_call_end", "tool": "bash", "toolCallId": "call-2", "ok": False,
        "result": {"error": "visible error", "rawResult": {"evidence": raw_secret}},
    })
    facade._on_worker_event({
        "type": "turn_end", "ok": True, "reason": "completed", "toolResults": [{"evidence": raw_secret}],
        "message": {"content": raw_secret},
    })
    facade._on_worker_event({"type": "future_event", "data": {"evidence": raw_secret}, "huge": [raw_secret] * 1000})

    with facade._space:
        assert len(facade._events) == 6
        assert facade._event_bytes <= facade._event_queue_max_bytes
        assert all(size <= facade._event_dto_max_bytes for _, size in facade._events)

    received: list[dict[str, Any]] = []
    facade.subscribe(received.append)
    await asyncio.sleep(0)

    start, end, message, failed_end, turn_end, unknown = received
    assert start["displayArgs"] == "command=printf safe, token=[REDACTED], content=<10000 chars>"
    assert "args" not in start and "rawResult" not in start
    assert end["outputText"] == "visible output"
    assert not {"result", "rawResult", "args", "content", "evidence", "image"} & set(end)
    assert message == {
        "type": "message_end", "role": "assistant", "model": "safe-model", "suppressed": True,
        "epoch": 0, "sequence": 3,
    }
    assert failed_end["error"] == "visible error"
    assert turn_end == {"type": "turn_end", "ok": True, "reason": "completed", "epoch": 0, "sequence": 5}
    assert unknown == {"type": "future_event", "epoch": 0, "sequence": 6}
    assert raw_secret not in json.dumps(received)
    await facade.close()


def test_plan_update_projection_is_explicit_bounded_and_literal() -> None:
    from one.core.agent_runtime_facade import _event_projection

    projected = _event_projection({
        "type": "plan_update",
        "plan": "[not markup]",
        "planItems": [{"step": f"step {index}", "status": "pending", "secret": "no"} for index in range(12)],
    })
    assert projected["planPresent"] is True
    assert projected["cleared"] is False
    assert projected["plan"] == "[not markup]"
    assert len(projected["planItems"]) == 12
    assert "secret" not in projected["planItems"][0]
    assert _event_projection({"type": "plan_update", "plan": ""})["cleared"] is True


@pytest.mark.asyncio
async def test_shutdown_attempts_dispose_when_abort_fails() -> None:
    class BrokenAbort(_Runtime):
        async def abort(self) -> None:
            raise RuntimeError("abort failed")

    runtime = BrokenAbort()
    facade = AgentRuntimeFacade(_spec(), lambda _spec: runtime)
    await facade.start()
    await facade.close()
    assert runtime.disposed
    assert facade.finalize()
