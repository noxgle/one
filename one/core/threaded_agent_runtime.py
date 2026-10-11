"""A private owner-loop backend for the agent runtime boundary."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

_LOG = logging.getLogger(__name__)
DEFAULT_COMMAND_DTO_MAX_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class RuntimeBootstrapSpec:
    """Detached inputs allowed to cross into a runtime owner thread."""

    cwd: str
    agent_dir: str
    session_path: str | None = None
    identifiers: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identifiers = {str(key): str(value) for key, value in self.identifiers.items()}
        options: dict[str, str | int | float | bool | None] = {}
        for key, value in self.options.items():
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise TypeError(f"bootstrap option {key!r} must be a scalar")
            options[str(key)] = value
        object.__setattr__(self, "identifiers", MappingProxyType(identifiers))
        object.__setattr__(self, "options", MappingProxyType(options))


class RuntimeClosedError(RuntimeError):
    """The worker is unavailable or has started shutting down."""


RuntimeFactory = Callable[[RuntimeBootstrapSpec], Awaitable[Any] | Any]
EventListener = Callable[[dict[str, Any]], None]


def _json_dto(value: Any, *, label: str, maximum: int) -> Any:
    """Return a detached JSON value, rejecting rather than truncating oversized data."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"runtime command {label} must be JSON-compatible") from exc
    size = len(encoded.encode("utf-8"))
    if size > maximum:
        raise ValueError(f"runtime command {label} exceeds {maximum} bytes")
    return json.loads(encoded)


class ThreadedAgentRuntime:
    """Own a runtime and every command coroutine on one private event loop."""

    def __init__(
        self,
        spec: RuntimeBootstrapSpec,
        factory: RuntimeFactory,
        event_listener: EventListener,
        *,
        command_allowlist: Iterable[str] = (),
        command_dto_max_bytes: int = DEFAULT_COMMAND_DTO_MAX_BYTES,
    ) -> None:
        if command_dto_max_bytes < 1:
            raise ValueError("command_dto_max_bytes must be positive")
        self._spec = spec
        self._factory = factory
        self._event_listener = event_listener
        self._commands = frozenset(command_allowlist)
        self._command_dto_max_bytes = command_dto_max_bytes
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started: Future[None] = Future()
        self._closed: Future[None] = Future()
        self._state = "new"
        self._runtime: Any = None
        self._factory_task: asyncio.Task[Any] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._pending: set[Future[Any]] = set()
        self._stop: asyncio.Event | None = None
        self._listener_unsubscribers: list[Callable[[], None]] = []

    @property
    def startup(self) -> Future[None]:
        return self._started

    def start(self) -> Future[None]:
        with self._lock:
            if self._state == "new":
                self._state = "starting"
                self._thread = threading.Thread(target=self._thread_main, name="one-agent-runtime", daemon=True)
                self._thread.start()
            return self._started

    def submit(self, correlation_id: str, command: str, payload: Mapping[str, Any]) -> Future[dict[str, Any]]:
        if command not in self._commands:
            raise ValueError(f"unknown runtime command: {command}")
        if not isinstance(payload, Mapping):
            raise ValueError("runtime command payload must be a JSON object")
        snapshot = _json_dto(dict(payload), label="payload", maximum=self._command_dto_max_bytes)
        assert isinstance(snapshot, dict)
        with self._lock:
            loop = self._loop
            if self._state != "running" or loop is None:
                raise RuntimeClosedError("agent runtime is not available")
            future = asyncio.run_coroutine_threadsafe(self._run_command(correlation_id, command, snapshot), loop)
            self._pending.add(future)
        future.add_done_callback(self._forget_pending)
        return future

    def _forget_pending(self, future: Future[Any]) -> None:
        with self._lock:
            self._pending.discard(future)

    async def _run_command(self, correlation_id: str, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Production adapters can own a strict dispatcher. The reflective
        # fallback preserves the pre-existing generic facade test backend.
        dispatcher = getattr(self._runtime, "dispatch", None)
        if callable(dispatcher):
            value = dispatcher(command, payload)
            if inspect.isawaitable(value):
                value = await value
            result = _json_dto(value, label="result", maximum=self._command_dto_max_bytes)
            return {"correlationId": correlation_id, "result": result}
        target = getattr(self._runtime, command, None)
        if not callable(target):
            raise ValueError(f"unavailable runtime command: {command}")
        value = target(**payload)
        if inspect.isawaitable(value):
            value = await value
        result = _json_dto(value, label="result", maximum=self._command_dto_max_bytes)
        return {"correlationId": correlation_id, "result": result}

    def close(self) -> Future[None]:
        with self._lock:
            if self._closed.done():
                return self._closed
            if self._state == "new":
                self._state = "closed"
                self._started.set_exception(RuntimeClosedError("agent runtime was closed before start"))
                self._closed.set_result(None)
                return self._closed
            if self._state in {"closing", "closed", "failed"}:
                return self._closed
            self._state = "closing"
            pending = tuple(self._pending)
            loop = self._loop
        for future in pending:
            future.cancel()
        if loop is not None:
            loop.call_soon_threadsafe(self._begin_shutdown)
        return self._closed

    def _begin_shutdown(self) -> None:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown())

    async def _shutdown(self) -> None:
        try:
            task = self._factory_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    _LOG.exception("agent runtime factory failed during shutdown")
            await self._dispose_runtime()
        except Exception:
            _LOG.exception("agent runtime shutdown failed")
        finally:
            assert self._stop is not None
            self._stop.set()

    async def _dispose_runtime(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        self._runtime = None
        self._detach_listeners()
        session = getattr(runtime, "session", None)
        abort = getattr(session, "abort", None) or getattr(runtime, "abort", None)
        # Cancellation is advisory: a broken abort must not leak the owner
        # runtime and its transports.  Preserve the first failure for the
        # shutdown logger after every cleanup opportunity has been attempted.
        failure: BaseException | None = None
        if callable(abort):
            try:
                value = abort()
                if inspect.isawaitable(value):
                    await value
            except BaseException as exc:
                failure = exc
                _LOG.exception("agent runtime abort during disposal failed")
        dispose = getattr(runtime, "dispose", None) or getattr(runtime, "close", None)
        if callable(dispose):
            try:
                value = dispose()
                if inspect.isawaitable(value):
                    await value
            except BaseException:
                _LOG.exception("agent runtime disposal failed")
                if failure is None:
                    raise
        if failure is not None:
            raise failure

    def _detach_listeners(self) -> None:
        """Release session and monitor callbacks before disposing their owner."""
        unsubscribers, self._listener_unsubscribers = self._listener_unsubscribers, []
        for unsubscribe in unsubscribers:
            try:
                unsubscribe()
            except Exception:
                _LOG.exception("agent runtime listener cleanup failed")

    def join(self, timeout: float | None = None) -> bool:
        """Bounded join for explicit post-UI CLI finalization only."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        self._stop = asyncio.Event()
        try:
            loop.run_until_complete(self._run())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            with self._lock:
                self._loop = None
                if self._state != "failed":
                    self._state = "closed"
            if not self._closed.done():
                self._closed.set_result(None)

    async def _create_runtime(self) -> Any:
        value = self._factory(self._spec)
        runtime = await value if inspect.isawaitable(value) else value
        # Store before the task completes so a concurrent shutdown always disposes it.
        self._runtime = runtime
        return runtime

    async def _run(self) -> None:
        with self._lock:
            already_closing = self._state == "closing"
        if already_closing:
            self._started.set_exception(RuntimeClosedError("agent runtime closed during startup"))
            return
        self._factory_task = asyncio.create_task(self._create_runtime())
        try:
            runtime = await self._factory_task
            with self._lock:
                closing = self._state == "closing"
                if not closing:
                    self._state = "running"
            if closing:
                await self._dispose_runtime()
                assert self._stop is not None
                self._stop.set()
            else:
                self._attach_listeners(runtime)
                self._started.set_result(None)
                assert self._stop is not None
                await self._stop.wait()
        except asyncio.CancelledError:
            if not self._started.done():
                self._started.set_exception(RuntimeClosedError("agent runtime closed during startup"))
        except BaseException as exc:
            if not self._started.done():
                self._started.set_exception(exc)
            with self._lock:
                if self._state != "closing":
                    self._state = "failed"
        finally:
            # A host can have bound listeners before startup reports success.
            # Always release/dispose that partially-started owner on failure too.
            if self._runtime is not None:
                await self._dispose_runtime()

    def _attach_listeners(self, runtime: Any) -> None:
        session = getattr(runtime, "session", runtime)
        subscribe = getattr(session, "subscribe", None)
        if callable(subscribe):
            unsubscribe = subscribe(self._event_listener)
            if callable(unsubscribe):
                self._listener_unsubscribers.append(cast(Callable[[], None], unsubscribe))
        # TuiRuntimeHost routes monitor invalidations through the normal session
        # event stream, which supplies the session-epoch rebind boundary.
        if getattr(runtime, "owns_monitor_events", False):
            return
        monitor = getattr(session, "subagent_monitor", None)
        monitor_subscribe = getattr(monitor, "subscribe", None)
        if callable(monitor_subscribe):
            def on_monitor_changed(notice: dict[str, Any]) -> None:
                self._event_listener({
                    "type": "subagent_monitor_invalidated",
                    "sessionId": notice.get("sessionId"),
                    "revision": notice.get("revision"),
                    "changeType": notice.get("type"),
                })

            unsubscribe = monitor_subscribe(on_monitor_changed)
            if callable(unsubscribe):
                self._listener_unsubscribers.append(cast(Callable[[], None], unsubscribe))
