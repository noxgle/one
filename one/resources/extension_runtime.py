"""Runtime for `one` extensions — an opencode-style hooks contract.

An extension is a ``.py`` file discovered by the ResourceLoader
(``agent_dir/extensions``, ``<project>/.one/extensions`` and ``--extensions``
paths). Each file must export a callable ``register(ctx) -> hooks`` (sync or
async). ``hooks`` is a dict mapping hook names to callables (sync or async).

Supported hooks (opencode parity):

- ``tool.execute.before (input, output)`` — mutate ``output["args"]`` or
  return a new args dict; raise ``ExtensionDenied`` (or any exception) to
  deny the tool call.
- ``tool.execute.after (input, output)`` — info-only, runs after a tool call
  produced a result.
- ``chat.message (input, output)`` — info-only, runs for each new user message.
- ``experimental.session.compacting (input, output)`` — push items onto
  ``output["context"]`` and/or set ``output["prompt"]`` before the compaction
  summary is produced.
- ``dispose ()`` — cleanup, called on session dispose.

Unknown hook names are silently ignored (opencode parity). Load and runtime
errors are reported through the ``extension_load_error`` event and never crash
the session.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import os
import sys
from pathlib import Path
from typing import Any, Callable


class ExtensionDenied(Exception):
    """Raised by ``tool.execute.before`` hooks to deny a tool call."""


class ExtensionContext:
    """Context passed to ``register(ctx)`` (analogous to opencode PluginInput)."""

    def __init__(
        self,
        *,
        directory: str,
        worktree: str,
        session_id: str | None = None,
        model: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.directory = directory
        self.worktree = worktree
        self.session_id = session_id
        self.model = model
        self.settings = settings or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "worktree": self.worktree,
            "sessionID": self.session_id,
            "model": self.model,
            "settings": self.settings,
        }


def find_worktree(directory: str | None = None) -> str:
    """Nearest ancestor containing a ``.git`` dir (falls back to ``directory``)."""
    cur = Path(directory or os.getcwd()).resolve()
    while True:
        if (cur / ".git").exists():
            return str(cur)
        if cur.parent == cur:
            return str(cur)
        cur = cur.parent


_KNOWN_HOOKS = frozenset(
    {
        "tool.execute.before",
        "tool.execute.after",
        "chat.message",
        "experimental.session.compacting",
        "dispose",
    }
)


class ExtensionRuntime:
    """Loads, binds and dispatches extension hooks for a session."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[tuple[str, Callable[..., Any]]]] = {name: [] for name in _KNOWN_HOOKS}
        self._loaded: list[dict[str, Any]] = []
        self._errors: list[dict[str, Any]] = []
        self._context: ExtensionContext | None = None

    @property
    def loaded(self) -> list[dict[str, Any]]:
        return list(self._loaded)

    @property
    def errors(self) -> list[dict[str, Any]]:
        return list(self._errors)

    @property
    def context(self) -> ExtensionContext | None:
        return self._context

    def has_hooks(self, name: str) -> bool:
        return bool(self._hooks.get(name))

    def hooks_for(self, name: str) -> list[tuple[str, Callable[..., Any]]]:
        return list(self._hooks.get(name, []))

    async def bind(self, extensions: list[dict[str, Any]], context: ExtensionContext) -> list[dict[str, Any]]:
        """Load and register discovered extensions. Returns (and stores) errors."""
        self._context = context
        for ext in extensions:
            path = str(ext.get("path", "") or "")
            if not path:
                continue
            try:
                hooks = await self._load(path, context)
            except Exception as e:  # noqa: BLE001 - contract: report, never crash
                self._errors.append(
                    {
                        "path": path,
                        "stage": "load",
                        "error": str(e).strip() or e.__class__.__name__,
                        "errorType": e.__class__.__name__,
                    }
                )
                continue
            bound: list[str] = []
            for name, fn in hooks.items():
                if name not in _KNOWN_HOOKS:
                    self._errors.append(
                        {
                            "path": path,
                            "stage": "bind",
                            "hook": name,
                            "error": f"Unknown hook ignored: {name}",
                            "errorType": "UnknownHook",
                        }
                    )
                    continue
                if not callable(fn):
                    self._errors.append(
                        {
                            "path": path,
                            "stage": "bind",
                            "hook": name,
                            "error": "Hook value is not callable",
                            "errorType": "TypeError",
                        }
                    )
                    continue
                self._hooks[name].append((path, fn))
                bound.append(name)
            self._loaded.append({"path": path, "hooks": sorted(bound)})
        return list(self._errors)

    async def _load(self, path: str, context: ExtensionContext) -> dict[str, Any]:
        p = Path(path)
        module_name = "_one_ext_" + hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load extension module: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        register = getattr(module, "register", None)
        if not callable(register):
            raise TypeError(f"Extension must export a callable `register(ctx)`: {path}")
        result = register(context)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise TypeError(f"register(ctx) must return a dict of hooks (got {type(result).__name__}): {path}")
        return dict(result)

    def _base_input(self) -> dict[str, Any]:
        ctx = self._context
        if ctx is None:
            return {}
        return {
            "directory": ctx.directory,
            "worktree": ctx.worktree,
            "sessionID": ctx.session_id,
        }

    async def call_before_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        emit_error: Callable[[str, str, Exception], None],
    ) -> tuple[dict[str, Any], str | None]:
        """Run ``tool.execute.before`` hooks.

        Returns ``(args, None)`` on success (args may have been mutated or
        replaced by a returned dict) or ``(args, reason)`` when a hook denied
        the call. The first raising hook denies and short-circuits.
        """
        out_args = args
        for path, fn in self.hooks_for("tool.execute.before"):
            input_payload = {**self._base_input(), "tool": tool_name, "args": args}
            output: dict[str, Any] = {"args": args}
            try:
                ret = fn(input_payload, output)
                if inspect.isawaitable(ret):
                    ret = await ret
                if isinstance(ret, dict):
                    out_args = ret
            except Exception as e:  # noqa: BLE001 - opencode parity: any throw denies
                reason = str(e).strip() or e.__class__.__name__
                emit_error(path, "tool.execute.before", e)
                return out_args, f"Extension {Path(path).stem} denied: {reason}"
        return out_args, None

    async def call_after_tool(
        self,
        tool_name: str,
        ok: bool,
        text: str,
        metadata: dict[str, Any] | None,
        emit_error: Callable[[str, str, Exception], None],
    ) -> None:
        """Run ``tool.execute.after`` hooks (info-only)."""
        for path, fn in self.hooks_for("tool.execute.after"):
            input_payload = {**self._base_input(), "tool": tool_name, "ok": ok}
            output: dict[str, Any] = {
                "title": f"{tool_name} {'ok' if ok else 'error'}",
                "output": text,
                "metadata": metadata or {},
            }
            try:
                ret = fn(input_payload, output)
                if inspect.isawaitable(ret):
                    await ret
            except Exception as e:  # noqa: BLE001
                emit_error(path, "tool.execute.after", e)

    async def call_chat_message(
        self,
        message: dict[str, Any],
        emit_error: Callable[[str, str, Exception], None],
    ) -> None:
        """Run ``chat.message`` hooks (info-only)."""
        for path, fn in self.hooks_for("chat.message"):
            input_payload = {**self._base_input(), "message": message}
            try:
                ret = fn(input_payload, {})
                if inspect.isawaitable(ret):
                    await ret
            except Exception as e:  # noqa: BLE001
                emit_error(path, "chat.message", e)

    async def call_compacting(
        self,
        emit_error: Callable[[str, str, Exception], None],
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Run ``experimental.session.compacting`` hooks.

        Returns ``(context_items, prompt_override)`` collected from all hooks.
        """
        items: list[dict[str, Any]] = []
        prompt: str | None = None
        for path, fn in self.hooks_for("experimental.session.compacting"):
            input_payload = self._base_input()
            output: dict[str, Any] = {"context": [], "prompt": None}
            try:
                ret = fn(input_payload, output)
                if inspect.isawaitable(ret):
                    await ret
            except Exception as e:  # noqa: BLE001
                emit_error(path, "experimental.session.compacting", e)
                continue
            ctx_items = output.get("context")
            if isinstance(ctx_items, list):
                items.extend(i for i in ctx_items if isinstance(i, dict) and i.get("content"))
            override = output.get("prompt")
            if isinstance(override, str) and override.strip() and prompt is None:
                prompt = override.strip()
        return items, prompt

    async def call_dispose(self, emit_error: Callable[[str, str, Exception], None]) -> None:
        """Run ``dispose`` hooks (cleanup)."""
        for path, fn in self.hooks_for("dispose"):
            try:
                ret = fn()
                if inspect.isawaitable(ret):
                    await ret
            except Exception as e:  # noqa: BLE001
                emit_error(path, "dispose", e)
