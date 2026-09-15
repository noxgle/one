from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from one.core.model_registry import ModelRegistry
from one.core.oauth import OAuthError
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo
from one.mcp import McpManager
from one.providers.openai_compatible import OpenAICompatibleAdapter
from one.providers.registry import build_provider_registry
from one.resources.extension_runtime import ExtensionContext, ExtensionRuntime, find_worktree
from one.tools.index import all_tools
from one.tools.plan import plan_tool

# Grace period added to the effective tool timeout for the outer asyncio.wait_for backstop.
_TOOL_TIMEOUT_GRACE_SEC = 5

# Fallback context window used when a model has no known context_window
# (dynamic models resolved at runtime). Keeps the ctx gauge/compaction working.
FALLBACK_CONTEXT_WINDOW = 128_000


# ---------------------------------------------------------------------------
# Session exceptions
# ---------------------------------------------------------------------------

class BusySessionError(Exception):
    """Raised when an action is attempted during an active streaming session."""


# ---------------------------------------------------------------------------
# read_image path sanitisation helpers (module-level — used before class defs).
# ---------------------------------------------------------------------------

def _sanitise_read_image_args(args: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *args* with read_image source paths replaced by basenames."""
    out = dict(args)
    for key in ("path", "file"):
        val = out.get(key)
        if isinstance(val, str):
            out[key] = str(Path(val).name) or "image"
    return out


def _sanitise_read_image_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *payload* with read_image args sanitised."""
    out = dict(payload)
    args = out.get("args")
    if isinstance(args, dict):
        out["args"] = _sanitise_read_image_args(args)
    return out


# ---------------------------------------------------------------------------
# Capability-error exceptions (image input gate).
# ---------------------------------------------------------------------------

class _CapabilityError(RuntimeError):
    """Raised when the selected model lacks a required capability (e.g. image input)."""


class _CapabilityErrorCooperative(_CapabilityError):
    """Raised in cooperation mode — the caller must decide what to do.

    In cooperative mode no assistant message is cached because the provider
    call was never attempted; the caller (TUI / RPC / CLI) can render the
    error itself and offer the user a way to switch models.
    """


def _with_fallback_context(model: ModelInfo | None) -> ModelInfo | None:
    """Normalize a model so the ctx gauge/compaction always have a window."""
    if model is not None and not model.context_window:
        return replace(model, context_window=FALLBACK_CONTEXT_WINDOW)
    return model


class _AbortSignal(Exception):
    """Internal: abort() cancelled the in-flight provider request."""


@dataclass
class ModelCycleResult:
    model: ModelInfo
    thinkingLevel: str
    isScoped: bool


class AgentSession:
    _TOOL_RESULT_MAX_CHARS = 12_000

    def __init__(
        self,
        session_manager: SessionManager,
        settings_manager: SettingsManager,
        model_registry: ModelRegistry,
        resource_loader: Any,
        model: ModelInfo | None,
        thinking_level: str,
        scoped_models: list[dict[str, Any]] | None = None,
        tools: list[str] | None = None,
        approval_callback: Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]] | None = None,
        mcp_manager: McpManager | None = None,
        storage_dir: str = "",
    ) -> None:
        self.session_manager = session_manager
        self.settings_manager = settings_manager
        self.model_registry = model_registry
        self.resource_loader = resource_loader
        self.model = _with_fallback_context(model)
        self.thinking_level = thinking_level
        self.scoped_models = scoped_models or []
        self.providers = build_provider_registry()
        self.messages: list[dict[str, Any]] = self.session_manager.build_session_context()["messages"]
        self._plan: str | None = None
        self._plan_just_created = False
        self._restore_plan_from_messages(self.messages)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._is_streaming = False
        self._is_compacting = False
        self._retrying = False
        self._pending_bash_messages: list[dict[str, Any]] = []
        self._steering: list[str] = []
        self._follow_up: list[str] = []
        base = tools or list(all_tools.keys())
        self._base_tools = list(base)
        self._active_tools = list(base)
        self._mcp_manager = mcp_manager
        if mcp_manager is not None:
            mcp_names = [t.name for t in mcp_manager.tools()]
            self._active_tools = list(dict.fromkeys(self._active_tools + mcp_names))
        self._abort_requested = False
        self._session_started_at = time.monotonic()
        # In-memory sessions have no session dir; use a transient temp dir
        # for image blobs so --no-session vision still works without
        # creating a ./blobs directory in the workspace.
        if storage_dir:
            self._storage_dir = storage_dir
            self._owns_temp_dir = False
        else:
            self._storage_dir = tempfile.mkdtemp(prefix="one-blobs-")
            self._owns_temp_dir = True
        self._images: list[dict[str, Any]] | None = None
        self._tool_images: list[dict[str, Any]] = []
        self._active_chat_tasks: set[asyncio.Task] = set()
        self._active_bash_tasks: set[asyncio.Task] = set()
        self._active_tool_tasks: set[asyncio.Task] = set()
        self._extension_ui_pending: dict[str, dict[str, Any]] = {}
        # Nudge instrumentation (Task C4 — measure, don't change behaviour).
        self._nudge_fires: int = 0
        self._nudge_conversions: dict[str, int] = {"true": 0, "false": 0}
        self._extension_ui_history: list[dict[str, Any]] = []
        self._pending_questions: dict[str, dict[str, Any]] = {}
        self._extension_runtime: ExtensionRuntime | None = None
        # Last subagent-timeout diagnostic (Task 4 — inspect-timeout).
        self._last_subagent_timeout: dict[str, Any] | None = None
        self.approval_callback = approval_callback
        self._approval_tools = set(settings_manager.get_tool_approval_tools())

        if not self.messages:
            if self.model:
                self.session_manager.append_model_change(self.model.provider, self.model.id)
            self.session_manager.append_thinking_level_change(self.thinking_level)

    def _restore_plan_from_messages(self, messages: list[dict[str, Any]]) -> None:
        """Restore _plan state from loaded messages and remove plan entries.

        Scans for ``customType == "plan"`` messages; the last one determines
        whether a plan is active (empty/None content → plan cleared).
        All plan messages are removed from the provider-visible message list.
        """
        plan_value: str | None = None
        plan_indices: list[int] = []
        for i, m in enumerate(messages):
            if m.get("customType") == "plan":
                plan_indices.append(i)
                content = m.get("content")
                if content:
                    plan_value = str(content)
        # The last plan message determines whether plan is active.
        # If the last plan message has empty/None content, plan is cleared.
        if plan_indices:
            last_idx = plan_indices[-1]
            last_msg = messages[last_idx] if last_idx < len(messages) else {}
            if not last_msg.get("content"):
                plan_value = None
        # Remove in reverse order so indices stay valid.
        for i in reversed(plan_indices):
            messages.pop(i)
        self._plan = plan_value

    def _assistant_text(self, message: dict[str, Any]) -> str:
        content = message.get("content", "")
        if isinstance(content, list):
            return "".join(x.get("text", "") for x in content if x.get("type") == "text")
        return str(content)

    def _build_runtime_system_prompt(self) -> str:
        getter = getattr(self.resource_loader, "get_system_prompt", None)
        if not callable(getter):
            prompt = "You are an expert coding assistant."
        else:
            try:
                prompt = getter(selected_tools=self._active_tools)
            except TypeError:
                # Backward compatibility with older loaders/mocks.
                prompt = getter()

        mcp_tools: list[Any] = []
        if self._mcp_manager is not None:
            tools_getter = getattr(self._mcp_manager, "tools", None)
            if callable(tools_getter):
                candidate = tools_getter()
                if isinstance(candidate, list):
                    mcp_tools = candidate
        if mcp_tools:
            lines = [
                "\n# MCP Tools",
                "Call these like any other tool (JSON: {\"name\": ..., \"args\": {...}}).",
                "Optional 'timeout' (seconds) in args overrides the per-call timeout.",
            ]
            for t in mcp_tools:
                input_schema = getattr(t, "input_schema", None)
                schema = json.dumps(input_schema, ensure_ascii=False) if input_schema else "{}"
                name = getattr(t, "name", "unknown")
                server = getattr(t, "server", "unknown")
                description = getattr(t, "description", "")
                lines.append(f"- {name} (server: {server}): {description} args={schema}")
            prompt = f"{prompt}\n" + "\n".join(lines)
        if self._plan is not None:
            prompt = f"{prompt}\n\n# Active Plan\n{self._plan}\nFollow this plan; adapt it via the plan tool only when the situation changes materially."
        now = datetime.now().astimezone()
        offset = now.strftime("%z") or "+0000"
        offset_fmt = f"{offset[:3]}:{offset[3:]}"
        tz_name = now.tzname() or "UTC"
        prompt = (
            f"{prompt}\n\n# Current Date\n"
            f"Today is {now:%Y-%m-%d} ({now:%A}), local time ({tz_name}, UTC{offset_fmt})."
        )
        return prompt

    def _try_parse_tool_call(self, text: str) -> dict[str, Any] | None:
        def normalize_tool_args(tool: str, raw_args: Any) -> dict[str, Any] | None:
            if isinstance(raw_args, dict):
                return raw_args
            if isinstance(raw_args, str):
                t = tool.strip().lower()
                if t == "bash":
                    return {"command": raw_args}
                if t in {"read", "write", "edit", "ls"}:
                    return {"path": raw_args}
                if t in {"grep", "find"}:
                    return {"pattern": raw_args}
            return None

        def normalize_jsonish(s: str) -> str:
            # Common LLM output quirks: smart quotes and BOM.
            return (
                s.replace("\ufeff", "")
                .replace("“", '"')
                .replace("”", '"')
                .replace("’", "'")
                .replace("<tool_call|>", "")
                .replace("<|tool_call|>", "")
                .replace("<tool_response>", "")
                .replace("<|tool_response>", "")
                .strip()
            )

        def json_loads_relaxed(raw: str) -> Any:
            try:
                return json.loads(raw)
            except Exception:
                pass

            # Repair invalid control chars (raw newline/tab) inside quoted JSON strings.
            repaired: list[str] = []
            in_string = False
            escaped = False
            for ch in raw:
                if in_string:
                    if escaped:
                        repaired.append(ch)
                        escaped = False
                        continue
                    if ch == "\\":
                        repaired.append(ch)
                        escaped = True
                        continue
                    if ch == '"':
                        repaired.append(ch)
                        in_string = False
                        continue
                    if ch == "\n":
                        repaired.append("\\n")
                        continue
                    if ch == "\r":
                        repaired.append("\\r")
                        continue
                    if ch == "\t":
                        repaired.append("\\t")
                        continue
                    repaired.append(ch)
                    continue

                repaired.append(ch)
                if ch == '"':
                    in_string = True
            return json.loads("".join(repaired))

        def parse_write_pseudo_json(raw: str) -> dict[str, Any] | None:
            s = normalize_jsonish(raw)
            if '"tool":"write"' not in s and '"tool": "write"' not in s and '"name":"write"' not in s and '"name": "write"' not in s:
                return None

            file_match = re.search(r'"(?:path|file)"\s*:\s*"([^"]+)"', s)
            if not file_match:
                return None
            path = file_match.group(1)

            content_key = re.search(r'"content"\s*:\s*"', s)
            if not content_key:
                return None
            content_start = content_key.end()

            # Prefer full closure (`"}}`) but tolerate malformed payloads with one missing brace.
            end_match = re.search(r'"\s*}\s*}\s*$', s)
            if not end_match:
                end_match = re.search(r'"\s*}\s*$', s)
            if not end_match or end_match.start() < content_start:
                return None
            content_raw = s[content_start : end_match.start()]

            # Best-effort unescape; keep raw quotes/newlines when model emitted invalid JSON.
            content = (
                content_raw.replace('\\"', '"')
                .replace("\\n", "\n")
                .replace("\\r", "\r")
                .replace("\\t", "\t")
            )
            return {"tool": "write", "args": {"path": path, "content": content}}

        def balanced_json_objects(s: str) -> list[str]:
            objs: list[str] = []
            depth = 0
            start = -1
            in_string = False
            escaped = False
            for i, ch in enumerate(s):
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                    continue
                if ch == "{":
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == "}":
                    if depth > 0:
                        depth -= 1
                        if depth == 0 and start >= 0:
                            objs.append(s[start : i + 1])
                            start = -1
            return objs

        def parse_from_obj(obj: dict[str, Any]) -> dict[str, Any] | None:
            tool = obj.get("tool") or obj.get("name")
            args = obj.get("args")
            if args is None:
                args = obj.get("input")
            if args is None and "arguments" in obj:
                args = obj.get("arguments")
            if args is None:
                args = {}
            if isinstance(args, str):
                # raw-function-call often encodes arguments as JSON string.
                try:
                    parsed_args = json_loads_relaxed(args)
                    if isinstance(parsed_args, dict):
                        args = parsed_args
                except Exception:
                    pass
            if not args and tool == "finish":
                # Flat form: {"tool":"finish","summary":"...","goal_success":true}
                flat = {k: v for k, v in obj.items() if k not in ("tool", "name", "args", "input", "arguments")}
                if flat:
                    args = flat
            if isinstance(tool, str):
                normalized_args = normalize_tool_args(tool, args)
                if normalized_args is not None:
                    return {"tool": tool, "args": normalized_args}
            return None

        def parse_json_candidates(raw_text: str) -> dict[str, Any] | None:
            candidates: list[str] = []
            stripped = normalize_jsonish(raw_text)
            if stripped.startswith("{") and stripped.endswith("}"):
                candidates.append(stripped)

            for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", raw_text):
                candidates.append(normalize_jsonish(m.group(1)))

            marker = "TOOL_CALL:"
            if marker in raw_text:
                candidates.append(normalize_jsonish(raw_text.split(marker, 1)[1]))

            # Fallback: extract balanced JSON objects from arbitrary text.
            candidates.extend(normalize_jsonish(x) for x in balanced_json_objects(raw_text))

            seen: set[str] = set()
            unique_candidates: list[str] = []
            for c in candidates:
                if c in seen:
                    continue
                seen.add(c)
                unique_candidates.append(c)

            for c in unique_candidates:
                try:
                    obj = json_loads_relaxed(c)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                parsed = parse_from_obj(obj)
                if parsed is not None:
                    return parsed
            return None

        def parse_raw_function_call(raw_text: str) -> dict[str, Any] | None:
            s = normalize_jsonish(raw_text)

            # llama.cpp tokenized raw-call format, e.g.:
            # <|tool_call>call:bash{args:<|"|>echo ok<|"|>}<tool_call|>
            tokenized = re.search(
                r"call:([a-zA-Z0-9_.-]+)\s*\{\s*args\s*:\s*<\|\"?\|>([\s\S]*?)<\|\"?\|>\s*\}",
                s,
            )
            if tokenized:
                tool_name = tokenized.group(1).strip()
                raw_args = tokenized.group(2).strip()
                normalized_args = normalize_tool_args(tool_name, raw_args)
                if normalized_args is not None:
                    return {"tool": tool_name, "args": normalized_args}

            # Try JSON-like objects first (often with name/arguments fields).
            for c in [s, *balanced_json_objects(s)]:
                try:
                    obj = json_loads_relaxed(c)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                parsed = parse_from_obj(obj)
                if parsed is not None and ("arguments" in obj or obj.get("type") in {"raw-function-call", "function_call"}):
                    return parsed

            # Text fallback: name: <tool> arguments: { ... }
            m_name = re.search(r"\bname\s*[:=]\s*\"?([a-zA-Z0-9_.-]+)\"?", s)
            if not m_name:
                return None
            m_args = re.search(r"\barguments\s*[:=]\s*(\{[\s\S]*\})", s)
            if not m_args:
                return None
            try:
                args_obj = json_loads_relaxed(m_args.group(1))
            except Exception:
                return None
            if not isinstance(args_obj, dict):
                return None
            normalized_args = normalize_tool_args(m_name.group(1), args_obj)
            if normalized_args is None:
                return None
            return {"tool": m_name.group(1), "args": normalized_args}

        parser_order = ["json", "raw-function-call"]
        if self.model and self.model.tool_parser:
            parsed_order: list[str] = []
            for p in self.model.tool_parser:
                if isinstance(p, dict):
                    p_type = str(p.get("type", "")).strip().lower()
                    if p_type:
                        parsed_order.append(p_type)
                elif isinstance(p, str):
                    p_type = p.strip().lower()
                    if p_type:
                        parsed_order.append(p_type)
            if parsed_order:
                parser_order = parsed_order

        for parser_type in parser_order:
            if parser_type == "json":
                parsed = parse_json_candidates(text)
                if parsed is not None:
                    return parsed
            elif parser_type in {"raw-function-call", "raw_function_call"}:
                parsed = parse_raw_function_call(text)
                if parsed is not None:
                    return parsed

        # Final fallback: recover common llama.cpp pseudo-JSON write payloads.
        fallback = parse_write_pseudo_json(text)
        if fallback is not None:
            return fallback
        return None

    def _should_tool_nudge(self, assistant_text: str, step: int, tool_results: list[dict[str, Any]]) -> bool:
        if step != 0:
            return False
        if tool_results:
            return False
        if not self._active_tools:
            return False
        t = assistant_text.strip()
        if not t:
            return False
        # Generic (language-agnostic) signal: short/meta first response, often "I'll check..."
        if len(t) > 280:
            return False
        # If it already looks like a substantial answer, do not force a nudge.
        if "\n" in t and len(t) > 140:
            return False
        return True

    @staticmethod
    def _strip_final_answer_prefix(text: str) -> str:
        marker = "FINAL_ANSWER:"
        if text.startswith(marker):
            return text[len(marker) :].strip()
        return text

    def _build_tool_result_message_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        msg_payload: dict[str, Any] = {
            "ok": bool(payload.get("ok", False)),
            "tool": payload.get("tool"),
            "args": payload.get("args", {}),
        }
        # Sanitise read_image path arguments: replace full source paths with
        # just the basename so the absolute path never leaks into JSONL /
        # toolResult messages / summaries / event payloads.
        if msg_payload.get("tool") == "read_image":
            args = msg_payload.setdefault("args", {})
            for key in ("path", "file"):
                val = args.get(key)
                if isinstance(val, str):
                    args[key] = str(Path(val).name) or "image"
        if msg_payload["ok"]:
            text = str(payload.get("result") or payload.get("outputText") or "")
            if len(text) > self._TOOL_RESULT_MAX_CHARS:
                head = int(self._TOOL_RESULT_MAX_CHARS * 0.7)
                tail = self._TOOL_RESULT_MAX_CHARS - head
                text = (
                    text[:head]
                    + "\n\n...[truncated]...\n\n"
                    + text[-tail:]
                    + f"\n\n[Tool output truncated to {self._TOOL_RESULT_MAX_CHARS} chars for model context.]"
                )
            msg_payload["result"] = text
            if payload.get("truncated") is True:
                msg_payload["truncated"] = True
            if payload.get("exitCode") is not None:
                msg_payload["exitCode"] = payload.get("exitCode")
            if payload.get("fullOutputPath"):
                msg_payload["fullOutputPath"] = payload.get("fullOutputPath")
        else:
            msg_payload["error"] = payload.get("error")
            # Sanitise read_image source paths in error diagnostics: replace
            # absolute paths with just the basename so the full path never
            # leaks into JSONL / toolResult messages / events.
            if msg_payload.get("tool") == "read_image" and isinstance(msg_payload["error"], str):
                for key in ("path", "file"):
                    val = payload.get("args", {}).get(key)
                    if isinstance(val, str):
                        basename = str(Path(val).name) or "image"
                        msg_payload["error"] = msg_payload["error"].replace(val, basename)
            # Preserve diagnostic text from the raw payload even when error is
            # None — e.g. a failed subagent carries summary / output / content.
            if msg_payload["error"] is None:
                fallback = (
                    str(payload.get("result") or payload.get("outputText") or "")
                ).strip()
                if fallback:
                    msg_payload["error"] = fallback
                else:
                    msg_payload["error"] = "Tool failed without details"
            if payload.get("errorType"):
                msg_payload["errorType"] = payload.get("errorType")
            if payload.get("timedOut") is True:
                msg_payload["timedOut"] = True
            if payload.get("cancelled") is True:
                msg_payload["cancelled"] = True
            if payload.get("exitCode") is not None:
                msg_payload["exitCode"] = payload.get("exitCode")
            # Keep useful per-child diagnostic fields (spawn_subagent failures).
            if payload.get("summary"):
                msg_payload["summary"] = payload["summary"]
            if payload.get("finished") is not None:
                msg_payload["finished"] = payload["finished"]
            if payload.get("goalSuccess") is not None:
                msg_payload["goalSuccess"] = payload["goalSuccess"]
        return msg_payload

    def _abort_assistant_message(self) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": [{"type": "text", "text": "Request aborted."}],
            "provider": self.model.provider if self.model else None,
            "model": self.model.id if self.model else None,
            "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
            "stopReason": "abort",
            "timestamp": int(time.time() * 1000),
        }

    def _finish_assistant_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("rawResult") or {}
        summary = str(raw.get("summary") or payload.get("result") or "Task complete.").strip()
        goal_success = bool(raw.get("goal_success", payload.get("goal_success", True)))
        return {
            "role": "assistant",
            "content": [{"type": "text", "text": summary}],
            "provider": self.model.provider if self.model else None,
            "model": self.model.id if self.model else None,
            "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
            "stopReason": "completed",
            "goalSuccess": goal_success,
            "timestamp": int(time.time() * 1000),
        }

    async def _execute_tool_by_name(self, tool_name: str, args: dict[str, Any], timeout_sec: int | None = None) -> dict[str, Any]:
        effective_timeout: int | float | None = None
        if tool_name not in self._active_tools:
            raise RuntimeError(f"Tool '{tool_name}' is disabled")
        if tool_name == "finish" and self._plan_just_created:
            return {
                "ok": False,
                "error": "The plan was just created. Execute and verify a planned step before finishing.",
            }
        tool = all_tools.get(tool_name)
        if not tool:
            if self._mcp_manager is not None and self._mcp_manager.has_tool(tool_name):
                return await self._mcp_manager.call_tool(tool_name, args, timeout=args.get("timeout"))
            raise RuntimeError(f"Unknown tool: {tool_name}")

        cwd = self.session_manager.cwd
        fn = tool.fn
        path_arg = args.get("path") or args.get("file")
        # Safe fallback: when the model passes a `/skill:<name>` or
        # `skill:<name>` path to `read`, return a structured diagnostic
        # instead of attempting filesystem access.  This prevents the model
        # from confusing the TUI/interactive `/skill:<name>` command with a
        # `read` tool path (see system-prompt disambiguation).
        _SKILL_INVOCATION_RE = re.compile(r"^/?skill:([a-z0-9]+(-[a-z0-9]+)*)$")
        if tool_name == "read" and isinstance(path_arg, str):
            _m = _SKILL_INVOCATION_RE.match(path_arg)
            if _m:
                _skill_name = _m.group(1)
                get_skill = getattr(self.resource_loader, "get_skill", None)
                if callable(get_skill):
                    _sr: dict[str, Any] = get_skill(_skill_name)  # type: ignore[assignment]
                    if "invalid" in _sr and _sr.get("invalid") is True:
                        return {
                            "ok": False,
                            "error": (
                                f"'{path_arg}' looks like a skill invocation command (/skill:{_skill_name}), not a file path. "
                                f"Skill '{_skill_name}' was found but is invalid: "
                                + "; ".join(_sr.get("diagnostics", []))
                            ),
                            "errorType": "SkillInvocationHint",
                            "skillName": _skill_name,
                            "diagnostics": _sr.get("diagnostics", []),
                        }
                    if "filePath" in _sr and _sr.get("valid", False):
                        return {
                            "ok": False,
                            "error": (
                                f"'{path_arg}' looks like a skill-invocation command (/skill:{_skill_name}), not a file path. "
                                f"To load this skill, use the 'read' tool with the skill's filePath: "
                                f"{_sr['filePath']}\n\n"
                                f"Skill: {_sr.get('name', _skill_name)}\n"
                                f"Description: {_sr.get('fm', {}).get('description', 'N/A')}"
                            ),
                            "errorType": "SkillInvocationHint",
                            "skillName": _skill_name,
                            "filePath": _sr["filePath"],
                        }
                    return {
                        "ok": False,
                        "error": (
                            f"'{path_arg}' looks like a skill invocation command (/skill:{_skill_name}), not a file path. "
                            f"No skill named '{_skill_name}' was found."
                        ),
                        "errorType": "SkillInvocationHint",
                        "skillName": _skill_name,
                    }
                # No resource_loader available — return a plain hint
                return {
                    "ok": False,
                    "error": (
                        f"'{path_arg}' looks like a skill invocation command (/skill:{_skill_name}), not a file path. "
                        f"Use the 'read' tool with the skill's SKILL.md filePath."
                    ),
                    "errorType": "SkillInvocationHint",
                    "skillName": _skill_name,
                }

        if tool_name == "read":
            result = fn(cwd, path_arg or "", args.get("offset"), args.get("limit"))
        elif tool_name == "read_image":
            result = fn(cwd, path_arg or "", self._storage_dir or "")
        elif tool_name == "write":
            content_arg = args.get("content")
            if content_arg is None:
                content_arg = args.get("text", "")
            result = fn(cwd, path_arg or "", content_arg)
        elif tool_name == "edit":
            result = fn(cwd, path_arg or "", args.get("edits", []))
        elif tool_name == "grep":
            result = fn(cwd, args.get("pattern", ""), args.get("path", "."))
        elif tool_name == "find":
            result = fn(cwd, args.get("pattern", "*"), args.get("path", "."))
        elif tool_name == "ls":
            result = fn(cwd, args.get("path", "."))
        elif tool_name == "bash":
            effective_timeout = args.get("timeout") or timeout_sec or self.settings_manager.get_tool_timeout_sec()
            result = fn(cwd, args.get("command", ""), effective_timeout, self.settings_manager.get_shell_command_prefix())
        elif tool_name == "finish":
            result = fn(args.get("summary", ""), bool(args.get("goal_success", True)))
            # Clear plan when finish is called (terminal tool)
            if self._plan:
                self._plan = None
                self._emit({"type": "plan_update", "plan": ""})
                self.session_manager.append_message({"role": "user", "customType": "plan", "content": "", "timestamp": int(time.time() * 1000)})
        elif tool_name == "spawn_subagent":
            # Compute the effective timeout for spawn_subagent (per-call >
            # timeout_sec param > subagents setting) — passed to _spawn_subagent
            # so the inner wait_for uses the same deadline as the outer one.
            _spawn_timeout = args.get("timeout") or timeout_sec
            if _spawn_timeout is None:
                _spawn_timeout = self.settings_manager.get_subagents_timeout_sec()
            effective_timeout = _spawn_timeout
            # Return the coroutine (not awaited here) so the outer timeout
            # wrapping (lines 683-698) can enforce the tool timeout.
            result = self._spawn_subagent(args, timeout_sec=_spawn_timeout)
        elif tool_name == "ask_user":
            # ask_user has its own askUser.timeoutSec semantics; return the
            # coroutine but skip timeout wrapping below.
            result = self._ask_user(args)
        elif tool_name == "plan":
            plan_text = args.get("plan", "")
            result = plan_tool(plan_text)
            self._plan = plan_text
            self._plan_just_created = True
            self._emit({"type": "plan_update", "plan": self._plan})
            self.session_manager.append_message({"role": "user", "customType": "plan", "content": plan_text, "timestamp": int(time.time() * 1000)})
        else:
            raise RuntimeError(f"Unsupported tool: {tool_name}")

        if inspect.isawaitable(result):
            task = asyncio.create_task(result)
            self._active_tool_tasks.add(task)
            try:
                outer_timeout = timeout_sec
                if tool_name == "bash":
                    outer_timeout = (effective_timeout or 0) + _TOOL_TIMEOUT_GRACE_SEC
                elif tool_name == "spawn_subagent":
                    # Use the effective (per-call > subagents setting) timeout for
                    # the outer wait_for wrapper — spawn_subagent still wraps
                    # sub.prompt() internally so the two timeouts are identical.
                    outer_timeout = effective_timeout
                elif self._mcp_manager is not None and self._mcp_manager.has_tool(tool_name):
                    model_timeout = args.get("timeout")
                    outer_timeout = (model_timeout or 0) + _TOOL_TIMEOUT_GRACE_SEC if model_timeout else None
                # ask_user is invoked with timeout_sec=None (line 1671), so
                # outer_timeout stays None → no asyncio.wait_for wrapping.
                if outer_timeout and outer_timeout > 0:
                    try:
                        result = await asyncio.wait_for(task, timeout=outer_timeout)
                    except TimeoutError:
                        # spawn_subagent has its own inner timeout; if the
                        # outer timeout fires (race edge case), ensure we
                        # return a typed SubagentTimeout result with cleanup.
                        if tool_name == "spawn_subagent":
                            # The inner timeout should have already returned a
                            # structured result; just check and discard the
                            # cancelled task.  If it hasn't, do minimal cleanup.
                            if not task.done():
                                task.cancel()
                                try:
                                    await task
                                except (asyncio.CancelledError, Exception):
                                    pass
                            result = {
                                "sessionId": "unknown",
                                "summary": "Subagent timed out",
                                "goalSuccess": False,
                                "finished": False,
                                "ok": False,
                                "output": "Subagent timed out",
                                "content": [{"type": "text", "text": "Subagent timed out"}],
                                "error": "Subagent timed out",
                                "errorType": "SubagentTimeout",
                                "timedOut": True,
                                "externalState": "unknown",
                            }
                        else:
                            raise
                else:
                    result = await task
            finally:
                self._active_tool_tasks.discard(task)
        if not isinstance(result, dict):
            raise RuntimeError(f"Invalid result from tool: {tool_name}")
        return result

    async def _run_tool_call(self, tool_name: str, args: dict[str, Any], timeout_sec: int | None = None) -> dict[str, Any]:
        # A real tool step after a newly created plan permits a later finish.
        # Do not count the guarded finish itself as that step.
        if tool_name not in {"plan", "finish"}:
            self._plan_just_created = False
        if (
            self.approval_callback is not None
            and tool_name != "finish"
            and tool_name in self._approval_tools
        ):
            # Cooperation mode: ask the user before executing a mutating tool.
            decision = self.approval_callback(tool_name, args)
            if inspect.isawaitable(decision):
                decision = await decision
            approved, reason = decision
            if not approved:
                reason = (reason or "").strip() or "No reason given"
                payload: dict[str, Any] = {
                    "ok": False,
                    "tool": tool_name,
                    "args": args,
                    "error": f"User rejected the command: {reason}",
                    "rejected": True,
                    "reason": reason,
                }
                message_payload = self._build_tool_result_message_payload(payload)
                msg = {
                    "role": "toolResult",
                    "content": json.dumps(message_payload, ensure_ascii=False),
                    "timestamp": int(time.time() * 1000),
                }
                self.messages.append(msg)
                self.session_manager.append_message(msg)
                self._emit({"type": "tool_approval_rejected", "tool": tool_name, "args": args, "reason": reason})
                # Sanitise read_image paths in tool_call_end event.
                end_result = payload
                if tool_name == "read_image":
                    end_result = _sanitise_read_image_payload(payload)
                self._emit({"type": "tool_call_end", "tool": tool_name, "ok": False, "result": end_result})
                return payload
        # Extension hooks: tool.execute.before (opencode contract). A raising
        # hook denies the call using the same contract as a user rejection.
        if self._extension_runtime is not None and self._extension_runtime.has_hooks("tool.execute.before"):
            args, denied = await self._invoke_extension_before_tool(tool_name, args)
            if denied:
                payload: dict[str, Any] = {
                    "ok": False,
                    "tool": tool_name,
                    "args": args,
                    "error": denied,
                    "rejected": True,
                    "reason": denied,
                }
                message_payload = self._build_tool_result_message_payload(payload)
                msg = {
                    "role": "toolResult",
                    "content": json.dumps(message_payload, ensure_ascii=False),
                    "timestamp": int(time.time() * 1000),
                }
                self.messages.append(msg)
                self.session_manager.append_message(msg)
                # Sanitise read_image paths in both rejection events.
                rej_args = args
                if tool_name == "read_image":
                    rej_args = _sanitise_read_image_args(args)
                self._emit({"type": "tool_approval_rejected", "tool": tool_name, "args": rej_args, "reason": denied})
                end_result = payload
                if tool_name == "read_image":
                    end_result = _sanitise_read_image_payload(payload)
                self._emit({"type": "tool_call_end", "tool": tool_name, "ok": False, "result": end_result})
                return payload
        # Sanitise read_image path arguments before emitting events — the
        # full source path must not appear in tool_call_start / JSONL / logs.
        emit_args = args
        if tool_name == "read_image":
            emit_args = dict(args)
            for key in ("path", "file"):
                val = emit_args.get(key)
                if isinstance(val, str):
                    emit_args[key] = str(Path(val).name) or "image"
        # Compute the effective timeout for the tool-call-start event so the
        # TUI / RPC renderers can show it (per-call override > passed timeout
        # > global default).  spawn_subagent uses the subagents timeout; all
        # other tools use tools.timeoutSec.  ask_user is invoked with
        # timeout_sec=None and relies on askUser.timeoutSec semantics inside;
        # the TUI would otherwise show a misleading "(timeout 30s)".
        if tool_name == "spawn_subagent":
            effective_timeout = args.get("timeout") or timeout_sec
            if effective_timeout is None:
                effective_timeout = self.settings_manager.get_subagents_timeout_sec()
        else:
            effective_timeout = args.get("timeout") or timeout_sec
            if effective_timeout is None and timeout_sec is not None:
                effective_timeout = self.settings_manager.get_tool_timeout_sec()
        self._emit({"type": "tool_call_start", "tool": tool_name, "args": emit_args, "effectiveTimeout": effective_timeout})
        try:
            result = await self._execute_tool_by_name(tool_name, args, timeout_sec=timeout_sec)
            if result.get("ok", True) is True:
                text = ""
                content = result.get("content")
                if isinstance(content, list):
                    text = "".join(x.get("text", "") for x in content if isinstance(x, dict))
                if not text:
                    text = str(result.get("output", ""))
                payload: dict[str, Any] = {
                    "ok": True,
                    "tool": tool_name,
                    "args": args,
                    "result": text,
                    "outputText": text,
                    "content": result.get("content"),
                    "details": result.get("details"),
                    "exitCode": result.get("exitCode"),
                    "truncated": result.get("truncated"),
                    "fullOutputPath": result.get("fullOutputPath"),
                    "rawResult": result,
                }
                # Only propagate timedOut/cancelled when the raw tool result actually has them.
                if "timedOut" in result:
                    payload["timedOut"] = result.get("timedOut", False)
                if "cancelled" in result:
                    payload["cancelled"] = result.get("cancelled", False)
                # read_image: retain the transient image ref for the current turn.
                if tool_name == "read_image" and isinstance(result, dict) and isinstance(result.get("image"), dict):
                    try:
                        self._remember_tool_image(result.get("image"))
                    except Exception as e:
                        raise RuntimeError(str(e)) from e
            else:
                # Tool returned a structured failure (e.g. bash timeout/cancel).
                text = ""
                content = result.get("content")
                if isinstance(content, list):
                    text = "".join(x.get("text", "") for x in content if isinstance(x, dict))
                if not text:
                    text = str(result.get("output", ""))
                payload = {
                    "ok": False,
                    "tool": tool_name,
                    "args": args,
                    "result": text,
                    "outputText": text,
                    "content": result.get("content"),
                    "details": result.get("details"),
                    "exitCode": result.get("exitCode"),
                    "truncated": result.get("truncated"),
                    "fullOutputPath": result.get("fullOutputPath"),
                    "rawResult": result,
                    "error": result.get("error"),
                    "errorType": result.get("errorType"),
                    "timedOut": result.get("timedOut", False),
                    "cancelled": result.get("cancelled", False),
                }
                # When raw result has cancelled:True → set aborted:True on payload
                # (timeout must have aborted absent/False to stay distinct).
                if result.get("cancelled") is True:
                    payload["aborted"] = True
        except TimeoutError:
            # asyncio.wait_for or a tool's internal wait_for timed out.
            # Convert to a typed, non-CancelledError result so callers
            # (TUI / RPC / parent agent) can distinguish timeout from
            # cancellation (which signals user-initiated abort).
            payload: dict[str, Any] = {
                "ok": False,
                "tool": tool_name,
                "args": args,
                "error": f"timed out after {timeout_sec or effective_timeout or '?'}s",
                "errorType": "TimeoutError",
                "timedOut": True,
            }
        except asyncio.CancelledError:
            payload: dict[str, Any] = {
                "ok": False,
                "tool": tool_name,
                "args": args,
                "error": "aborted",
                "errorType": "CancelledError",
                "aborted": True,
            }
        except Exception as e:
            if isinstance(e, _AbortSignal):
                payload = {
                    "ok": False,
                    "tool": tool_name,
                    "args": args,
                    "error": "aborted",
                    "errorType": "_AbortSignal",
                    "aborted": True,
                }
            else:
                error_text = str(e).strip() or e.__class__.__name__
                payload = {
                    "ok": False,
                    "tool": tool_name,
                    "args": args,
                    "error": error_text,
                    "errorType": e.__class__.__name__,
                }
                # Sanitise read_image paths in tool_call_error event.
                error_event_args = args
                if tool_name == "read_image":
                    error_event_args = dict(args)
                    for k in ("path", "file"):
                        v = error_event_args.get(k)
                        if isinstance(v, str):
                            error_event_args[k] = str(Path(v).name) or "image"
                error_event_text = error_text
                if tool_name == "read_image" and isinstance(error_event_text, str):
                    for k in ("path", "file"):
                        v = args.get(k)
                        if isinstance(v, str):
                            basename = str(Path(v).name) or "image"
                            error_event_text = error_event_text.replace(v, basename)
                self._emit({"type": "tool_call_error", "tool": tool_name, "args": error_event_args, "error": error_event_text})

        message_payload = self._build_tool_result_message_payload(payload)
        msg = {
            "role": "toolResult",
            "content": json.dumps(message_payload, ensure_ascii=False),
            "timestamp": int(time.time() * 1000),
        }
        self.messages.append(msg)
        self.session_manager.append_message(msg)
        await self._invoke_extension_after_tool(
            tool_name,
            bool(payload.get("ok")),
            str(payload.get("result") or payload.get("error") or ""),
            metadata={"exitCode": payload.get("exitCode"), "errorType": payload.get("errorType")},
        )
        # Sanitise read_image path in tool_call_end event (payload still has
        # the raw args — _build_tool_result_message_payload sanitises the
        # message_payload, but the event result dict does not).
        event_result = payload
        if tool_name == "read_image":
            event_result = dict(payload)
            event_args = event_result.get("args", {})
            if isinstance(event_args, dict):
                for key in ("path", "file"):
                    val = event_args.get(key)
                    if isinstance(val, str):
                        event_args[key] = str(Path(val).name) or "image"
        tool_call_end: dict[str, Any] = {"type": "tool_call_end", "tool": tool_name, "ok": payload.get("ok", False), "result": event_result}
        if payload.get("aborted"):
            tool_call_end["aborted"] = True
        self._emit(tool_call_end)
        return payload

    def _subagent_depth(self) -> int:
        """Depth of this session in the subagent tree (0 = top-level)."""
        try:
            return int(self.session_manager.get_header().get("subagentDepth") or 0)
        except Exception:
            return 0

    async def _spawn_subagent(self, args: dict[str, Any], timeout_sec: int | None = None) -> dict[str, Any]:
        if not self.settings_manager.get_subagents_enabled():
            raise RuntimeError("Subagents disabled (enable with /subagents on or 'one config subagents.enabled true')")
        task = str(args.get("task") or "").strip()
        tasks = args.get("tasks")
        if tasks is not None:
            if not isinstance(tasks, list):
                raise RuntimeError("'tasks' must be a list of task strings")
            validated: list[str] = []
            for idx, item in enumerate(tasks):
                if not isinstance(item, str):
                    raise RuntimeError(f"tasks[{idx}] must be a string, got {item.__class__.__name__}")
                stripped = item.strip()
                if stripped:
                    validated.append(stripped)
            tasks = validated
        if task and tasks:
            raise RuntimeError("Provide either 'task' or 'tasks', not both")
        if not task and not tasks:
            raise RuntimeError("spawn_subagent requires 'task' or 'tasks'")

        model_spec = str(args.get("model") or "").strip() or None
        tool_names = args.get("tools")
        if tool_names is not None:
            if not isinstance(tool_names, list) or not all(isinstance(t, str) for t in tool_names):
                raise RuntimeError("'tools' must be a list of tool names")
            bad = [t for t in tool_names if t not in all_tools]
            if bad:
                raise RuntimeError(f"Unknown tools: {', '.join(bad)}")

        max_concurrent = self.settings_manager.get_subagents_max_concurrent()
        max_depth = self.settings_manager.get_subagents_max_depth()
        depth = self._subagent_depth()
        if depth >= max_depth:
            raise RuntimeError(f"Subagent depth limit reached ({max_depth})")
        if tasks and len(tasks) > max_concurrent:
            raise RuntimeError(f"Too many parallel subagents: {len(tasks)} > maxConcurrent {max_concurrent}")

        sub_model = self.model
        if model_spec:
            if "/" in model_spec:
                p, m = model_spec.split("/", 1)
                sub_model = self.model_registry.resolve(p, m, allow_dynamic=True)
            else:
                sub_model = self.model_registry.resolve(self.model.provider, model_spec, allow_dynamic=True)
            if not sub_model:
                raise RuntimeError(f"Unknown model: {model_spec}")

        # Validate and normalise explicit tools.
        sub_tools = tool_names or list(self._active_tools)
        if tool_names is not None:
            # Distinguish omitted (None) from explicit empty list.
            if len(tool_names) == 0:
                raise RuntimeError(
                    "The 'tools' parameter must not be an empty list — "
                    "omit it to inherit the parent's tools, or include at "
                    "least 'finish' to allow the subagent to complete."
                )
            # Ensure the explicit tool set can actually complete — at minimum
            # the 'finish' terminal tool must be present.
            if "finish" not in tool_names:
                raise RuntimeError(
                    "The explicit 'tools' list must include 'finish' to allow "
                    "the subagent to complete; add 'finish' to the list or omit "
                    "'tools' to use the parent's default tools."
                )

        if task:
            return await self._run_subagent(task, sub_model, sub_tools, depth, timeout_sec=timeout_sec)
        results = await asyncio.gather(*(self._run_subagent(t, sub_model, sub_tools, depth, timeout_sec=timeout_sec) for t in tasks))
        # Aggregate: per-child results + top-level ok + error when any fails.
        all_ok = all(r.get("ok") for r in results)
        combined = "\n".join(f"- {r.get('summary', '(no summary)')}" for r in results)
        aggregate_error: str | None = None
        if not all_ok:
            fail_details = [
                f"Child '{r.get('sessionId', '?')}': {r.get('error', r.get('summary', 'unknown error'))}"
                for r in results
                if not r.get("ok")
            ]
            aggregate_error = "; ".join(fail_details) if fail_details else "One or more children failed"
        return {
            "ok": all_ok,
            "goalSuccess": all_ok,
            "results": list(results),
            "output": combined,
            "content": [{"type": "text", "text": combined}],
            "error": aggregate_error,
        }

    def _all_images(self) -> list[dict[str, Any]] | None:
        """Combined prompt + tool-loaded images (transient, never persisted)."""
        combined: list[dict[str, Any]] = []
        if getattr(self, "_images", None):
            combined.extend(self._images or [])
        if getattr(self, "_tool_images", None):
            combined.extend(self._tool_images or [])
        return combined or None

    def _remember_tool_image(self, image: dict[str, Any] | None) -> None:
        """Retain a ``read_image`` result for the current turn."""
        if not isinstance(image, dict):
            return
        if not image.get("blob_hash") and not image.get("blobHash"):
            return
        try:
            from one.core.attachments import _MAX_IMAGES_PER_PROMPT
        except Exception:
            _MAX_IMAGES_PER_PROMPT = 4
        current = len(getattr(self, "_images", None) or []) + len(getattr(self, "_tool_images", None) or [])
        if current >= _MAX_IMAGES_PER_PROMPT:
            raise ValueError(f"Too many images: {current + 1} > {_MAX_IMAGES_PER_PROMPT}")
        # Deduplicate by blob hash.
        new_hash = image.get("blob_hash") or image.get("blobHash")
        for existing in self._tool_images:
            if (existing.get("blob_hash") or existing.get("blobHash")) == new_hash:
                return
        self._tool_images.append(image)

    async def _run_subagent(
        self, task: str, sub_model: Any, sub_tools: list[str], depth: int, *, timeout_sec: int | None = None
    ) -> dict[str, Any]:
        # Use the parent's session_dir so subagent files go to the same directory.
        # Use __init__ directly to bypass SessionManager.create()'s "session_dir or
        # get_default_session_dir()" fallback (empty string would be treated as falsy).
        persist = self.session_manager.session_file is not None
        manager = SessionManager(
            self.session_manager.cwd,
            self.session_manager.session_dir,
            None,  # new session file
            persist,
        )
        header = manager.get_header()
        if self.session_manager.session_file:
            header["parentSession"] = self.session_manager.session_file
        header["subagentDepth"] = depth + 1
        manager._rewrite()  # noqa: SLF001
        sub = AgentSession(
            session_manager=manager,
            settings_manager=self.settings_manager,
            model_registry=self.model_registry,
            resource_loader=self.resource_loader,
            model=sub_model,
            thinking_level=self.thinking_level,
            scoped_models=self.scoped_models,
            tools=sub_tools,
            approval_callback=self.approval_callback,
        )
        sub.providers = self.providers

        def _sub_answer(event: dict[str, Any]) -> None:
            if event.get("type") == "ask_user":
                try:
                    sub.answer_question(event["id"], "(no answer channel in subagent; proceed with best judgment)")
                except ValueError:
                    pass
        sub.subscribe(_sub_answer)

        sub_id = sub.session_id
        task_id = task
        ok = False
        error_text: str | None = None
        error_type: str | None = None
        self._emit({"type": "subagent_start", "sessionId": sub_id, "task": task_id})
        sub_timeout_sec = timeout_sec or self.settings_manager.get_subagents_timeout_sec()
        try:
            try:
                await asyncio.wait_for(sub.prompt(task_id), timeout=sub_timeout_sec)
            except TimeoutError:
                # Graceful shutdown: abort + bounded dispose, then cancel any
                # stragglers.  Never re-raise — the parent already has a
                # wait_for wrapper that would retry.
                ok = False
                error_text = "Subagent timed out"
                error_type = "SubagentTimeout"
                try:
                    await sub.abort()
                except Exception:
                    pass
                try:
                    # Shield the dispose call from any outer cancellation so the
                    # subagent has a bounded window to release resources.
                    await asyncio.wait_for(asyncio.shield(sub.dispose()), timeout=1.0)
                except (TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    pass
                # Cancel/reap any remaining active tasks on the subagent.
                remaining: list[asyncio.Task] = []
                for attr in ("_active_chat_tasks", "_active_bash_tasks", "_active_tool_tasks"):
                    remaining.extend(getattr(sub, attr, set()) or [])
                for t in remaining:
                    if not t.done():
                        t.cancel()
                if remaining:
                    try:
                        await asyncio.wait_for(asyncio.shield(asyncio.gather(*remaining, return_exceptions=True)), timeout=1.0)
                    except (TimeoutError, asyncio.CancelledError):
                        pass
                    except Exception:
                        pass
                # Gather diagnostics from the subagent before it's torn down.
                result = sub.get_last_finish_result()
                assistant_text = sub.get_last_assistant_text() or ""
                summary = result.get("summary") or assistant_text or "(no output)"
                elapsed = time.monotonic() - sub._session_started_at
                # Redact secrets in diagnostics.
                error_text = re.sub(
                    r"(sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9\._\-~+/=]+)",
                    "REDACTED",
                    error_text,
                )
                error_text = error_text[:512]
                summary = f"{error_text} (elapsed {elapsed:.1f}s)" if not summary.startswith(("Subagent ", "Incompl")) else summary
                # Extract last event type and last tool name from messages.
                last_event = "unknown"
                last_tool_name = "unknown"
                for msg in reversed(sub.messages):
                    ct = msg.get("customType")
                    if ct:
                        last_event = ct
                        break
                    role = msg.get("role")
                    if role == "assistant":
                        last_event = "message"
                        break
                    if role == "toolResult":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            try:
                                parsed = json.loads(content)
                                last_tool_name = parsed.get("tool", "unknown")
                            except (json.JSONDecodeError, TypeError):
                                pass
                        break
                diagnostic = {
                    "operation": "timed out",
                    "errorType": "SubagentTimeout",
                    "externalState": "unknown",
                    "sessionId": sub_id,
                    "elapsedSec": round(elapsed, 2),
                    "lastTool": last_tool_name,
                    "lastEvent": last_event,
                    "error": error_text,
                    "summary": summary,
                    "lastAssistantText": assistant_text,
                    "actionableHint": (
                        "Subagent exceeded the configured timeout. "
                        "Check: (1) the subagent's task complexity, "
                        "(2) bash/ask_user calls blocking indefinitely, "
                        "(3) provider connectivity — increase `subagents.timeoutSec` or reduce task scope."
                    ),
                }
                self._last_subagent_timeout = diagnostic
                # Redact secrets in assistant text (same mechanism as error_text).
                redacted_assistant = re.sub(
                    r"(sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9\._\-~+/=]+)",
                    "REDACTED",
                    assistant_text,
                )
                return {
                    "sessionId": sub_id,
                    "summary": summary,
                    "goalSuccess": bool(result.get("goalSuccess")),
                    "finished": False,
                    "ok": False,
                    "output": summary,
                    "content": [{"type": "text", "text": summary}],
                    "error": error_text,
                    "errorType": error_type,
                    "elapsedSec": elapsed,
                    "externalState": "unknown",
                    "lastEvent": last_event,
                    "lastTool": last_tool_name,
                    "lastAssistantText": redacted_assistant,
                }
            result = sub.get_last_finish_result()
            assistant_text = sub.get_last_assistant_text() or ""
            summary = result.get("summary") or assistant_text or "(no output)"
            ok = bool(result.get("finished") and result.get("goalSuccess"))
            # When ok is False the subagent didn't complete successfully.
            # Add a diagnostic so the parent never sees {ok:false, error:null}.
            if not ok:
                if not result.get("finished"):
                    error_text = "Subagent did not complete (no finish call)"
                    error_type = "IncompleteExecution"
                    # Enrich with the subagent's last assistant text (may contain
                    # error context from a crashed provider or step-limit message).
                    enriched = assistant_text.strip()
                    if enriched:
                        error_text = f"{error_text}: {enriched}"
                else:
                    error_text = "Subagent finished but goal was not successful"
                    error_type = "GoalNotSatisfied"
                # Sanitise: strip anything that looks like an API key / secret.
                error_text = re.sub(
                    r"(sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9\._\-~+/=]+)",
                    "REDACTED",
                    error_text,
                )
                error_text = error_text[:512]
                summary = f"{error_text}" if not summary.startswith(("Subagent ", "Incompl")) else summary
            return {
                "sessionId": sub_id,
                "summary": summary,
                "goalSuccess": bool(result.get("goalSuccess")),
                "finished": bool(result.get("finished")),
                "ok": ok,
                "output": summary,
                "content": [{"type": "text", "text": summary}],
                "error": error_text,
                "errorType": error_type,
                "externalState": "unknown",
            }
        except Exception as e:
            error_text = str(e).strip() or e.__class__.__name__
            error_type = e.__class__.__name__
            # Sanitise: strip anything that looks like an API key / secret to avoid
            # leaking credentials into the session jsonl.
            error_text = re.sub(r"(sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9\._\-~+/=]+)", "REDACTED", error_text)
            error_text = error_text[:512]  # cap diagnostic text
            return {
                "sessionId": sub_id,
                "summary": f"Subagent error: {error_text}",
                "goalSuccess": False,
                "finished": False,
                "ok": False,
                "output": f"Subagent error: {error_text}",
                "content": [{"type": "text", "text": f"Subagent error: {error_text}"}],
                "error": error_text,
                "errorType": error_type,
                "externalState": "unknown",
            }
        finally:
            try:
                await sub.dispose()
            except Exception:
                pass
            self._emit({"type": "subagent_end", "sessionId": sub_id, "ok": ok, "error": error_text, "errorType": error_type})

    def _ask_user_timeout_sec(self, args: dict[str, Any]) -> int:
        try:
            return max(0, int(args.get("timeoutSec") or self.settings_manager.get_ask_user_timeout_sec() or 0))
        except (TypeError, ValueError):
            return 0

    async def _ask_user(self, args: dict[str, Any]) -> dict[str, Any]:
        """Escalation tool: pause the session and ask the human a question.

        Emits an `ask_user` event; the UI channel answers via `answer_question`.
        Returns the answer as the tool result so it lands in the model context.
        """
        question = str(args.get("question") or "").strip()
        if not question:
            raise RuntimeError("ask_user requires a non-empty 'question'")
        qid = uuid.uuid4().hex[:12]
        entry: dict[str, Any] = {"question": question, "event": asyncio.Event(), "answer": None}
        self._pending_questions[qid] = entry
        self._emit({"type": "ask_user", "id": qid, "question": question})
        timeout = self._ask_user_timeout_sec(args)
        # Check abort before waiting (in case abort was called already).
        if self._abort_requested:
            self._pending_questions.pop(qid, None)
            raise _AbortSignal()
        try:
            if timeout > 0:
                await asyncio.wait_for(entry["event"].wait(), timeout=timeout)
            else:
                await entry["event"].wait()
        except TimeoutError:
            self._pending_questions.pop(qid, None)
            raise RuntimeError(f"No answer received within timeout ({timeout}s)") from None
        # Check abort after receiving answer.
        if self._abort_requested:
            self._pending_questions.pop(qid, None)
            raise _AbortSignal()
        answer = entry["answer"] or ""
        self._pending_questions.pop(qid, None)
        return {"output": answer, "content": [{"type": "text", "text": answer}]}

    def answer_question(self, question_id: str, answer: str) -> None:
        """Answer a pending ask_user question (called by UI channels)."""
        entry = self._pending_questions.get(question_id)
        if entry is None:
            raise ValueError(f"No pending question with id {question_id}")
        entry["answer"] = str(answer)
        entry["event"].set()
        self._emit({"type": "ask_user_answered", "id": question_id, "answer": str(answer)})

    def get_pending_questions(self) -> list[dict[str, Any]]:
        return [{"id": qid, "question": e["question"]} for qid, e in self._pending_questions.items()]

    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def off() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return off

    def _emit(self, event: dict[str, Any]) -> None:
        for l in list(self._listeners):
            l(event)

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    @property
    def is_compacting(self) -> bool:
        return self._is_compacting

    @property
    def session_file(self) -> str | None:
        return self.session_manager.session_file

    @property
    def session_id(self) -> str:
        return self.session_manager.session_id

    @property
    def session_name(self) -> str | None:
        return self.session_manager.get_session_name()

    @property
    def pending_message_count(self) -> int:
        return len(self._steering) + len(self._follow_up)

    @property
    def active_tools(self) -> list[str]:
        return list(self._active_tools)

    def sync_mcp_tools(self) -> None:
        """Recompute _active_tools from base tools + current MCP tool list."""
        if self._mcp_manager is None:
            return
        mcp_names = [t.name for t in self._mcp_manager.tools()]
        self._active_tools = list(dict.fromkeys(self._base_tools + mcp_names))

    @property
    def steering_mode(self) -> str:
        return self.settings_manager.get_steering_mode()

    @property
    def follow_up_mode(self) -> str:
        return self.settings_manager.get_follow_up_mode()

    @property
    def auto_compaction_enabled(self) -> bool:
        return bool(self.settings_manager.merged().get("compaction", {}).get("enabled", True))

    @property
    def auto_retry_enabled(self) -> bool:
        return self.settings_manager.get_retry_enabled()

    def set_auto_retry_enabled(self, enabled: bool) -> None:
        self.settings_manager.set_retry_enabled(enabled)

    def set_auto_compaction_enabled(self, enabled: bool) -> None:
        merged = self.settings_manager.get_global_settings()
        merged.setdefault("compaction", {})["enabled"] = enabled

    def set_steering_mode(self, mode: str) -> None:
        self.settings_manager.set_steering_mode(mode)

    def set_follow_up_mode(self, mode: str) -> None:
        self.settings_manager.set_follow_up_mode(mode)

    def set_session_name(self, name: str) -> None:
        self.session_manager.append_session_info(name)

    async def set_model(self, model: ModelInfo) -> None:
        self.model = _with_fallback_context(model)
        self.session_manager.append_model_change(model.provider, model.id)

    def set_thinking_level(self, level: str) -> None:
        self.thinking_level = level
        self.session_manager.append_thinking_level_change(level)

    def cycle_thinking_level(self) -> str:
        levels = ["off", "minimal", "low", "medium", "high", "xhigh"]
        idx = levels.index(self.thinking_level) if self.thinking_level in levels else 0
        next_level = levels[(idx + 1) % len(levels)]
        self.set_thinking_level(next_level)
        return next_level

    async def cycle_model(self) -> ModelCycleResult | None:
        if self.scoped_models:
            current = next((i for i, m in enumerate(self.scoped_models) if self.model and m["model"].id == self.model.id and m["model"].provider == self.model.provider), -1)
            nxt = self.scoped_models[(current + 1) % len(self.scoped_models)]
            await self.set_model(nxt["model"])
            if nxt.get("thinkingLevel"):
                self.set_thinking_level(nxt["thinkingLevel"])
            return ModelCycleResult(model=nxt["model"], thinkingLevel=self.thinking_level, isScoped=True)

        available = self.model_registry.get_available()
        if not available:
            return None
        current = next((i for i, m in enumerate(available) if self.model and m.id == self.model.id and m.provider == self.model.provider), -1)
        nxt = available[(current + 1) % len(available)]
        await self.set_model(nxt)
        return ModelCycleResult(model=nxt, thinkingLevel=self.thinking_level, isScoped=False)

    async def _invoke_provider(
        self,
        messages: list[dict[str, Any]],
        *,
        allow_live_stream: bool = True,
    ) -> dict[str, Any]:
        if not self.model:
            raise RuntimeError("No model selected")
        provider = self.providers.get(self.model.provider)
        if not provider:
            raise RuntimeError(f"Unsupported provider: {self.model.provider}")
        if self.model.base_url and isinstance(provider, OpenAICompatibleAdapter):
            provider = provider.with_base_url(self.model.base_url)

        # Subscription OAuth (Phase 18): refresh the token when nearing expiry.
        # getattr-guarded so injected test registries without OAuth support work.
        refresher: Any = getattr(self.model_registry, "ensure_oauth_fresh", None)
        if callable(refresher) and self.model.provider:
            try:
                await refresher(self.model.provider)
            except OAuthError as e:
                raise RuntimeError(f"OAuth token refresh failed: {e}") from e

        auth = self.model_registry.get_api_key_and_headers(self.model)
        if not auth.get("ok"):
            raise RuntimeError(auth.get("error", "Authentication failed"))

        # Stream deltas live when possible, but suppress tool-call shaped payloads.
        streamed_started = False
        streamed_buffer = ""
        streamed_suppressed = False
        streamed_msg = {
            "role": "assistant",
            "content": [],
            "provider": self.model.provider,
            "model": self.model.id,
            "timestamp": int(time.time() * 1000),
        }

        def _on_delta(delta: str) -> None:
            nonlocal streamed_started, streamed_buffer, streamed_suppressed
            if not delta:
                return
            streamed_buffer += delta
            if streamed_suppressed:
                return
            if not streamed_started:
                sample = streamed_buffer.lstrip()
                # Delay rendering until we are reasonably sure it's user-facing prose, not tool JSON.
                if len(sample) < 16:
                    return
                if sample.startswith("{") or sample.startswith("```") or sample.startswith("TOOL_CALL:"):
                    toolish = ('"tool"' in sample) or ('"args"' in sample) or ('"name"' in sample) or ("call:" in sample)
                    if toolish:
                        streamed_suppressed = True
                        return
                    if len(sample) < 220:
                        return
                streamed_started = True
                self._emit({"type": "message_start", "message": streamed_msg})
                if streamed_buffer:
                    self._emit(
                        {
                            "type": "message_update",
                            "assistantMessageEvent": {"type": "text_delta", "delta": streamed_buffer},
                        }
                    )
                    streamed_buffer = ""
                return

            self._emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": delta},
                }
            )

        def _on_thinking_delta(delta: str) -> None:
            if not delta:  # only skip None or exact empty ""; whitespace-only passes through
                return
            # Emit exact original delta — no whitespace stripping.
            self._emit({"type": "thinking_delta", "delta": delta})

        chat_kwargs = {
            "api_key": auth["apiKey"],
            "model": self.model.id,
            "messages": messages,
            "thinking_level": self.thinking_level,
            "headers": auth.get("headers"),
        }
        # Images are transient — stored on the session, not in message history.
        # Combines explicit prompt images and `read_image` tool results.
        images = self._all_images()
        sig = inspect.signature(provider.chat)
        if images and "images" not in sig.parameters:
            # Adapter cannot accept images — raise explicit capability error
            # instead of silently dropping them.
            raise _CapabilityError(
                f"Model '{self.model.id}' does not support image input"
            )
        if images and "images" in sig.parameters:
            chat_kwargs["images"] = images
        # Pass storage_dir so providers can resolve blobs without leaking paths.
        if self._storage_dir and "storage_dir" in sig.parameters:
            chat_kwargs["storage_dir"] = self._storage_dir
        if allow_live_stream and "on_delta" in sig.parameters:
            chat_kwargs["on_delta"] = _on_delta
        if "on_thinking_delta" in sig.parameters:
            chat_kwargs["on_thinking_delta"] = _on_thinking_delta

        task = asyncio.create_task(provider.chat(**chat_kwargs))
        self._active_chat_tasks.add(task)
        try:
            provider_timeout = self.settings_manager.get_provider_timeout_sec()
            res = await asyncio.wait_for(task, timeout=provider_timeout)
        except TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise RuntimeError(
                f"provider timed out after {provider_timeout}s — "
                "the request did not resolve within the configured deadline"
            ) from None
        except asyncio.CancelledError:
            if self._abort_requested:
                raise _AbortSignal() from None
            raise
        finally:
            self._active_chat_tasks.discard(task)

        return {
            "role": "assistant",
            "content": [{"type": "text", "text": res.text}],
            "provider": self.model.provider,
            "model": self.model.id,
            "usage": {
                "input": res.usage.get("prompt_tokens") or res.usage.get("input_tokens") or 0,
                "output": res.usage.get("completion_tokens") or res.usage.get("output_tokens") or 0,
                "cacheRead": res.usage.get("cache_read_input_tokens") or 0,  # Anthropic-specific; other providers fall through to 0
                "cacheWrite": res.usage.get("cache_creation_input_tokens") or 0,  # Anthropic-specific; other providers fall through to 0
                "cost": {"total": 0},
            },
            "stopReason": res.stop_reason,
            "timestamp": int(time.time() * 1000),
            "_streamedStart": streamed_started,
            "_streamedMessage": streamed_msg,
            "_streamedSuppressed": streamed_suppressed,
        }

    @staticmethod
    def _approx_message_tokens(message: dict[str, Any]) -> int:
        content = message.get("content", "")
        if isinstance(content, list):
            text = "".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
        else:
            text = str(content)
        return max(1, len(text) // 4)

    def _flatten_conversation(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role in {"custom", "toolResult", "bashExecution"}:
                role = "user"
            if role not in {"system", "user", "assistant"}:
                role = "user"
            content = m.get("content", "")
            if isinstance(content, list):
                text = "".join(x.get("text", "") for x in content if x.get("type") == "text")
            else:
                text = str(content)
            out.append({"role": role, "content": text})
        return out

    def _flatten_messages_for_provider(self) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": self._build_runtime_system_prompt()},
            *self._flatten_conversation(self.messages),
        ]

    def _estimate_request_tokens(self, messages: list[dict[str, Any]] | None = None) -> int:
        """Estimate the complete provider request, including fixed prompt overhead.

        The old context gauge counted only persisted message bodies.  Providers
        also receive the runtime system prompt (including tool schemas), and a
        character-based estimate is optimistic across tokenizers.  Include the
        actual flattened request plus a conservative 15% tokenizer/serialization
        margin so auto-compaction happens before the provider rejects it.
        """
        conversation = self.messages if messages is None else messages
        request = [
            {"role": "system", "content": self._build_runtime_system_prompt()},
            *self._flatten_conversation(conversation),
        ]
        raw_tokens = sum(self._approx_message_tokens(message) for message in request)
        return max(1, int(raw_tokens * 1.15) + 256)

    async def _preflight_compact(self) -> None:
        """Compact before a provider call when the full request nears its limit."""
        if not self.auto_compaction_enabled or self._is_compacting:
            return
        usage = self.get_context_usage()
        if usage and usage["percent"] >= self.settings_manager.get_compaction_threshold_percent():
            await self.compact(reason="auto_preflight", allow_during_prompt=True)

    async def prompt(
        self,
        text: str,
        options: dict[str, Any] | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> None:
        options = options or {}
        if self._is_streaming:
            # Resolve the delivery mode for input received while the model is
            # still streaming a response.  *steeringMode* is the primary signal
            # (interrupt = allow interruption, follow_up / queue = defer);
            # *followUpMode* is a secondary hint used only when steeringMode
            # is interrupt (backwards-compatible with /steer, /follow callers).
            behavior = options.get("streamingBehavior")
            if not behavior:
                sm = self.steering_mode
                if sm == "follow_up":
                    behavior = "followUp"
                elif sm == "queue":
                    behavior = "steer"  # queue as steer
                else:
                    # Default: steeringMode=interrupt → use followUpMode for
                    # the follow-up vs steer decision.
                    behavior = "followUp" if self.follow_up_mode == "follow_up" else "steer"
            if behavior == "steer":
                await self.steer(text, images=images)
                return
            if behavior == "followUp":
                await self.follow_up(text, images=images)
                return
            raise RuntimeError(f"Invalid streamingBehavior: {behavior}")

        self._is_streaming = True
        self._abort_requested = False
        self._emit({"type": "agent_start"})
        # Images are transient — stored on the session, never persisted
        # to JSONL or emitted in events (no internal refs / paths leak).
        self._images = images
        self._tool_images = []
        user_msg = {"role": "user", "content": text, "timestamp": int(time.time() * 1000)}
        self.messages.append(user_msg)
        self.session_manager.append_message(user_msg)
        self._emit({"type": "message_start", "message": user_msg})
        self._emit({"type": "message_end", "message": user_msg})
        if self._extension_runtime is not None and self._extension_runtime.has_hooks("chat.message"):
            await self._invoke_extension_chat_message(user_msg)

        # Fail-closed outer wrapper: if anything unexpected escapes the retry
        # loop (provider timeouts, unexpected exceptions, etc.), emit terminal
        # events, reset streaming/retry flags, and re-raise.  This prevents the
        # session from being stuck in _is_streaming=True forever when a
        # provider call hangs or SSE keep-alives reset httpx timers.
        finished_with_tool = False
        try:
            retry_cfg = self.settings_manager.get_retry_settings()
            attempt = 0
            while True:
                    try:
                        self._emit({"type": "turn_start", "attempt": attempt + 1})
                        # Nudge counters accumulate across turns (reported in get_session_stats);
                        # fireCount in nudge events reflects per-turn fires (always 1 since _should_tool_nudge fires only at step 0).
                        # self._nudge_fires and self._nudge_conversions are NOT reset here — they persist for session stats.
                        tool_results: list[dict[str, Any]] = []
                        tool_max = self.settings_manager.get_tool_max_steps()
                        is_unlimited = tool_max == 0
                        tool_timeout_sec = self.settings_manager.get_tool_timeout_sec()
                        final_assistant: dict[str, Any] | None = None
                        # Unlimited mode (maxSteps=0): infinite iterator → loop only
                        # exits via break (abort, budget, finish, no-tool-call).
                        # Bounded mode (maxSteps>0): finite range → exits when exhausted.
                        step_iter: Any = (
                            itertools.count() if is_unlimited else range(max(1, tool_max))
                        )
                        for step in step_iter:
                            if self._abort_requested:
                                final_assistant = self._abort_assistant_message()
                                break
                            budget_hit = self._budget_exceeded()
                            if budget_hit is not None:
                                kind, message = budget_hit
                                self._emit({"type": "budget_exceeded", "kind": kind, "message": message})
                                final_assistant = {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": message}],
                                    "provider": self.model.provider if self.model else None,
                                    "model": self.model.id if self.model else None,
                                    "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
                                    "stopReason": "budget_exceeded",
                                    "timestamp": int(time.time() * 1000),
                                }
                                break
                            try:
                                # Capability gate: if images are present but the model does not
                                # support image input, fail immediately (no provider call).
                                images = self._all_images()
                                if images and self.model and not self.model.input_image:
                                    if self.approval_callback is not None:
                                        raise _CapabilityErrorCooperative(
                                            f"Model '{self.model.id}' does not support image input"
                                        )
                                    raise _CapabilityError(
                                        f"Model '{self.model.id}' does not support image input"
                                    )
                                await self._preflight_compact()
                                assistant = await self._invoke_provider(self._flatten_messages_for_provider(), allow_live_stream=True)
                            except _AbortSignal:
                                self._abort_requested = True
                                final_assistant = self._abort_assistant_message()
                                break
                            if self._abort_requested:
                                final_assistant = self._abort_assistant_message()
                                break
                            self.messages.append(assistant)
                            assistant_text = self._assistant_text(assistant)
                            # Consume tool-loaded images after the first provider call
                            # in the tool loop — they were attached to the prompt and
                            # should not be re-sent on subsequent provider calls.
                            self._tool_images = []
                            tool_call = self._try_parse_tool_call(assistant_text)
                            if tool_call is None and self._should_tool_nudge(assistant_text, step=step, tool_results=tool_results):
                                self._nudge_fires += 1
                                # fireCount is per-turn; currently always 1 since _should_tool_nudge fires only at step 0.
                                self._emit({"type": "tool_call_nudge_start", "fireCount": self._nudge_fires})
                                try:
                                    await self._preflight_compact()
                                    nudged = await self._invoke_provider(
                                        self._flatten_messages_for_provider()
                                        + [
                                            {
                                                "role": "user",
                                                "content": (
                                                    "Decide now: if tools are required, respond ONLY with JSON "
                                                    '{"tool":"<name>","args":{...}} and no extra text. '
                                                    "If tools are not required, respond with FINAL_ANSWER:<text>."
                                                ),
                                            }
                                        ],
                                        allow_live_stream=False,
                                    )
                                except _AbortSignal:
                                    self._abort_requested = True
                                    final_assistant = self._abort_assistant_message()
                                    break
                                nudged_text = self._assistant_text(nudged)
                                nudged_tool_call = self._try_parse_tool_call(nudged_text)
                                if nudged_tool_call:
                                    tool_call = nudged_tool_call
                                    assistant = nudged
                                    assistant_text = nudged_text
                                    self._nudge_conversions["true"] += 1
                                    self._emit(
                                        {"type": "tool_call_nudge_end", "used": True, "fireCount": self._nudge_fires}
                                    )
                                else:
                                    nudged_text = self._strip_final_answer_prefix(nudged_text)
                                    nudged["content"] = [{"type": "text", "text": nudged_text}]
                                    assistant = nudged
                                    assistant_text = nudged_text
                                    self._nudge_conversions["false"] += 1
                                    self._emit(
                                        {"type": "tool_call_nudge_end", "used": False, "fireCount": self._nudge_fires}
                                    )
                            if tool_call is None:
                                toolish = (
                                    '"tool"' in assistant_text
                                    and '"args"' in assistant_text
                                    and (
                                        assistant_text.strip().startswith("{")
                                        or "```" in assistant_text
                                        or "TOOL_CALL:" in assistant_text
                                    )
                                )
                                if toolish:
                                    self._emit(
                                        {
                                            "type": "tool_call_parse_failed",
                                            "sample": assistant_text[:500],
                                        }
                                    )

                            if tool_call:
                                sub_timeout = None if tool_call["tool"] == "ask_user" else tool_timeout_sec
                                tool_payload = await self._run_tool_call(tool_call["tool"], tool_call["args"], timeout_sec=sub_timeout)
                                tool_results.append(tool_payload)
                                if tool_call["tool"] == "finish" and tool_payload.get("ok"):
                                    # Terminal tool: end the turn with the summary as the
                                    # final assistant message; no further provider calls.
                                    # (Plan cleanup is handled inside _execute_tool_by_name.)
                                    finished_with_tool = True
                                    final_assistant = self._finish_assistant_message(tool_payload)
                                    break
                                if self._abort_requested:
                                    final_assistant = self._abort_assistant_message()
                                    break
                                continue

                            final_assistant = assistant
                            break

                        if final_assistant is None and not is_unlimited:
                            final_assistant = {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "I ran tools but reached the step limit. Please refine your next instruction.",
                                    }
                                ],
                                "provider": self.model.provider if self.model else None,
                                "model": self.model.id if self.model else None,
                                "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
                                "stopReason": "tool_step_limit",
                                "timestamp": int(time.time() * 1000),
                            }
                        else:
                            assistant_text = self._assistant_text(final_assistant).strip()
                            if not assistant_text:
                                final_assistant["content"] = [
                                    {
                                        "type": "text",
                                        "text": "The model returned an empty response. Please try again or change the model.",
                                    }
                                ]
                                final_assistant["stopReason"] = final_assistant.get("stopReason") or "empty_response"

                        if final_assistant not in self.messages:
                            self.messages.append(final_assistant)
                        self.session_manager.append_message(final_assistant)
                        streamed_started = bool(final_assistant.pop("_streamedStart", False))
                        streamed_suppressed = bool(final_assistant.pop("_streamedSuppressed", False))
                        streamed_msg = final_assistant.pop("_streamedMessage", None)
                        if streamed_started and isinstance(streamed_msg, dict):
                            event: dict[str, Any] = {"type": "message_end", "message": final_assistant}
                            if streamed_suppressed:
                                event["suppressed"] = True
                            self._emit(event)
                        elif not streamed_suppressed:
                            # Only emit message events when content was NOT suppressed.
                            # Suppressed content (tool JSON detected early) is rendered
                            # via tool_call_start / tool_call_end, not as assistant text.
                            self._emit({"type": "message_start", "message": final_assistant})
                            for chunk in final_assistant.get("content", []):
                                if chunk.get("type") == "text":
                                    self._emit(
                                        {
                                            "type": "message_update",
                                            "assistantMessageEvent": {"type": "text_delta", "delta": chunk.get("text", "")},
                                        }
                                    )
                            self._emit({"type": "message_end", "message": final_assistant})
                        else:
                            # Suppressed — emit only message_end with suppressed=True so
                            # the TUI knows to skip creating an assistant block.
                            self._emit({"type": "message_end", "message": final_assistant, "suppressed": True})
                        self._emit(
                            {
                                "type": "turn_end",
                                "ok": True,
                                "attempt": attempt + 1,
                                "aborted": final_assistant.get("stopReason") == "abort",
                                "reason": final_assistant.get("stopReason") or "completed",
                                "message": final_assistant,
                                "toolResults": tool_results,
                            }
                        )
                        self._emit({"type": "agent_end", "messages": [user_msg, final_assistant]})
                        break
                    except _CapabilityError as e:
                        # Capability errors: emit turn_end, then either raise
                        # _CapabilityErrorCooperative (cooperation) or emit a
                        # controlled assistant message (autonomous).  No retry.
                        self._emit(
                            {
                                "type": "turn_end",
                                "ok": False,
                                "attempt": attempt + 1,
                                "reason": "error",
                                "error": str(e),
                                "willRetry": False,
                            }
                        )
                        if self.approval_callback is not None:
                            # Cooperation mode: do NOT cache an assistant error message.
                            # The caller (TUI / RPC / CLI) renders the error and offers
                            # the user a way to switch models.
                            # Reset streaming state and clear transient image refs so
                            # the session is ready for the next prompt.
                            self._is_streaming = False
                            self._images = None
                            self._tool_images = []
                            self._emit({"type": "agent_end", "messages": [user_msg]})
                            raise _CapabilityErrorCooperative(str(e)) from e
                        # Autonomous mode: emit a controlled assistant message.
                        error_msg = {
                            "role": "assistant",
                            "content": [{"type": "text", "text": str(e)}],
                            "provider": self.model.provider if self.model else None,
                            "model": self.model.id if self.model else None,
                            "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
                            "stopReason": "unsupported_vision",
                            "timestamp": int(time.time() * 1000),
                        }
                        self.messages.append(error_msg)
                        self.session_manager.append_message(error_msg)
                        self._emit({"type": "message_start", "message": error_msg})
                        self._emit({"type": "message_end", "message": error_msg})
                        self._emit({"type": "agent_end", "messages": [user_msg, error_msg]})
                        break
                    except Exception as e:
                        retries_enabled = bool(retry_cfg.get("enabled", True))
                        max_retries = int(retry_cfg.get("maxRetries", 3))
                        error_text = str(e).strip() or e.__class__.__name__
                        is_ctx_limit = self._is_context_limit_error(error_text)
                        will_retry = retries_enabled and attempt < max_retries
                        self._emit(
                            {
                                "type": "turn_end",
                                "ok": False,
                                "attempt": attempt + 1,
                                "reason": "error",
                                "error": error_text,
                                "willRetry": will_retry,
                            }
                        )
                        if not will_retry:
                            error_msg = {
                                "role": "assistant",
                                "content": [{"type": "text", "text": error_text}],
                                "provider": self.model.provider if self.model else None,
                                "model": self.model.id if self.model else None,
                                "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
                                "stopReason": "error",
                                "timestamp": int(time.time() * 1000),
                            }
                            self.messages.append(error_msg)
                            self.session_manager.append_message(error_msg)
                            self._emit({"type": "message_start", "message": error_msg})
                            self._emit(
                                {
                                    "type": "message_update",
                                    "assistantMessageEvent": {"type": "text_delta", "delta": error_text},
                                }
                            )
                            self._emit({"type": "message_end", "message": error_msg})
                            self._emit({"type": "agent_end", "messages": [user_msg, error_msg]})
                            break
                        # Context-limit error: compact *before* retrying so the same
                        # logical turn/retry uses a smaller context without duplicating
                        # the user message (user_msg was already added at prompt start).
                        if is_ctx_limit:
                            try:
                                await self.compact(reason="context_limit_retry", allow_during_prompt=True)
                            except Exception:
                                pass  # compaction failure → fall through to normal retry
                        attempt += 1
                        delay_ms = min(int(retry_cfg.get("baseDelayMs", 1500)) * (2 ** (attempt - 1)), int(retry_cfg.get("maxDelayMs", 20000)))
                        self._retrying = True
                        self._emit(
                            {
                                "type": "auto_retry_start",
                                "attempt": attempt,
                                "maxAttempts": max_retries,
                                "delayMs": delay_ms,
                                "errorMessage": error_text,
                            }
                        )
                        if self._abort_requested:
                            abort_msg = self._abort_assistant_message()
                            self.messages.append(abort_msg)
                            self.session_manager.append_message(abort_msg)
                            self._emit({"type": "message_start", "message": abort_msg})
                            self._emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Request aborted."}})
                            self._emit({"type": "message_end", "message": abort_msg})
                            self._emit(
                                {
                                    "type": "turn_end",
                                    "ok": True,
                                    "attempt": attempt,
                                    "aborted": True,
                                    "reason": "abort",
                                    "message": abort_msg,
                                    "toolResults": [],
                                }
                            )
                            self._emit({"type": "agent_end", "messages": [user_msg, abort_msg]})
                            self._emit({"type": "auto_retry_end", "attempt": attempt, "willRetry": False, "aborted": True})
                            self._retrying = False
                            break
                        # Interruptible backoff: abort() during the delay must not
                        # block the session for up to maxDelayMs.
                        for _ in range(max(1, delay_ms // 25)):
                            if self._abort_requested:
                                break
                            await asyncio.sleep(0.025)
                        if self._abort_requested:
                            abort_msg = self._abort_assistant_message()
                            self.messages.append(abort_msg)
                            self.session_manager.append_message(abort_msg)
                            self._emit({"type": "message_start", "message": abort_msg})
                            self._emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Request aborted."}})
                            self._emit({"type": "message_end", "message": abort_msg})
                            self._emit(
                                {
                                    "type": "turn_end",
                                    "ok": True,
                                    "attempt": attempt,
                                    "aborted": True,
                                    "reason": "abort",
                                    "message": abort_msg,
                                    "toolResults": [],
                                }
                            )
                            self._emit({"type": "agent_end", "messages": [user_msg, abort_msg]})
                            self._emit({"type": "auto_retry_end", "attempt": attempt, "willRetry": False, "aborted": True})
                            self._retrying = False
                            break
                        self._emit({"type": "auto_retry_end", "attempt": attempt, "willRetry": True, "aborted": False})
                        self._retrying = False

        except BaseException as e:
            # Fail-closed: if anything escapes the inner exception handlers
            # (SystemExit, KeyboardInterrupt, etc.), reset state and emit
            # terminal events so the session never stays stuck in
            # _is_streaming=True.  Skip re-emitting agent_end for
            # _CapabilityErrorCooperative since the inner handler already
            # emitted it before re-raising.
            self._is_streaming = False
            self._retrying = False
            self._abort_requested = False
            if not isinstance(e, _CapabilityErrorCooperative):
                self._emit(
                    {
                        "type": "turn_end",
                        "ok": False,
                        "attempt": 1,
                        "reason": "error",
                        "error": "session error — provider call or tool loop failed unexpectedly",
                        "willRetry": False,
                    }
                )
                self._emit({"type": "agent_end", "messages": [user_msg]})
            raise

        self._is_streaming = False

        if self._abort_requested:
            self._abort_requested = False
            return

        # A successful finish is an explicit terminal boundary. Messages
        # queued while the task was running belong to a new user decision and
        # must not be executed automatically after the agent has reported
        # completion (especially when they refer to resources just created).
        if finished_with_tool:
            return

        # Auto-compaction: shrink the context between turns when it grows past
        # the configured threshold. Runs before draining queued messages so they
        # get a freshly compacted context. Emits the standard compaction events.
        if self.auto_compaction_enabled and not self._is_compacting:
            usage = self.get_context_usage()
            if usage and usage["percent"] >= self.settings_manager.get_compaction_threshold_percent():
                await self.compact(reason="auto")

        # Do not drain queued steering / follow-up messages when:
        #   - steeringMode is "queue"  (user opted out of auto-steer),
        #   - steeringMode is "follow_up"  (follow-up queue, not steer),
        #   - abort was requested  (terminal state; user should inspect queues).
        # When steeringMode is "interrupt" (default) the queue is drained
        # normally so queued steer/follow-up messages continue the loop.
        if self.steering_mode != "interrupt":
            return

        # After compaction (or not), check abort before draining queued
        # steering / follow-up messages — prevents processing steering
        # that arrived while compaction was in flight.
        if self._abort_requested:
            self._abort_requested = False
            return

        if self._steering:
            msg = self._steering.pop(0)
            self._emit({"type": "queue_update", "steering": list(self._steering), "followUp": list(self._follow_up)})
            await self.prompt(msg)
        elif self._follow_up:
            msg = self._follow_up.pop(0)
            self._emit({"type": "queue_update", "steering": list(self._steering), "followUp": list(self._follow_up)})
            await self.prompt(msg)

    async def steer(self, text: str, images: list[dict[str, Any]] | None = None) -> None:
        """Queue a steering message. Text-only; images are not supported."""
        if images:
            raise ValueError("steer does not support image attachments")
        self._steering.append(text)
        self._emit({"type": "queue_update", "steering": list(self._steering), "followUp": list(self._follow_up)})

    async def follow_up(self, text: str, images: list[dict[str, Any]] | None = None) -> None:
        """Queue a follow-up message. Text-only; images are not supported."""
        if images:
            raise ValueError("follow_up does not support image attachments")
        self._follow_up.append(text)
        self._emit({"type": "queue_update", "steering": list(self._steering), "followUp": list(self._follow_up)})

    def get_pending_queues(self) -> dict[str, list[str]]:
        return {
            "steering": list(self._steering),
            "followUp": list(self._follow_up),
        }

    def clear_pending_queues(self, target: str = "all") -> dict[str, list[str]]:
        norm = (target or "all").strip().lower()
        if norm in {"all", "both"}:
            self._steering.clear()
            self._follow_up.clear()
        elif norm in {"steering", "steer", "s"}:
            self._steering.clear()
        elif norm in {"follow", "followup", "follow_up", "f"}:
            self._follow_up.clear()
        else:
            raise ValueError("target must be one of: all, steering, follow")
        snapshot = {"steering": list(self._steering), "followUp": list(self._follow_up)}
        self._emit({"type": "queue_update", **snapshot})
        return snapshot

    async def compact(
        self,
        custom_instructions: str | None = None,
        reason: str = "manual",
        *,
        allow_during_prompt: bool = False,
    ) -> dict[str, Any]:
        # Guard: refuse to compact while a prompt/stream is in flight to avoid
        # mutating messages concurrently with the provider loop.
        # The `allow_during_prompt` flag bypasses this guard for internal
        # error-recovery paths (e.g. context-limit) where the provider call has
        # already failed and the prompt loop is paused — safe to compact.
        if self._is_streaming and not allow_during_prompt:
            return {
                "aborted": False, "summary": "", "tokensBefore": 0,
                "kept": 0, "skipped": True, "busy": True,
            }
        self._is_compacting = True
        self._emit({"type": "compaction_start", "reason": reason})
        try:
            if not self.messages:
                result = {"aborted": False, "summary": "", "tokensBefore": 0, "kept": 0, "skipped": True}
                self._emit({"type": "compaction_end", "reason": reason, "result": result, "aborted": False, "willRetry": False})
                return result

            total = len(self.messages)
            min_kept = max(1, self.settings_manager.get_compaction_min_kept_messages())
            budget = self.settings_manager.get_compaction_recent_tokens()

            # Raw window: keep the most recent messages that fit within the token
            # budget, but always keep at least `min_kept` messages (never drop the last one).
            max_keep_start = max(0, total - min_kept)
            keep_start = 0
            acc = 0
            for i in range(total - 1, -1, -1):
                acc += self._approx_message_tokens(self.messages[i])
                if acc > budget and (total - i) >= min_kept:
                    keep_start = min(i + 1, max_keep_start)
                    break

            if keep_start == 0:
                # Nothing to drop: there is no meaningful compaction to perform.
                result = {"aborted": False, "summary": "", "tokensBefore": 0, "kept": total, "skipped": True}
                self._emit({"type": "compaction_end", "reason": reason, "result": result, "aborted": False, "willRetry": False})
                return result

            dropped = self.messages[:keep_start]
            tokens_before = sum(self._approx_message_tokens(m) for m in dropped)

            # Extension hooks: experimental.session.compacting (opencode contract).
            context_items: list[dict[str, Any]] = []
            if self._extension_runtime is not None and self._extension_runtime.has_hooks("experimental.session.compacting"):
                context_items, prompt_override = await self._invoke_extension_compacting()
                if prompt_override and not custom_instructions:
                    custom_instructions = prompt_override

            if custom_instructions:
                summary_text = custom_instructions.strip() or f"Compacted previous {len(dropped)} messages."
            elif self.settings_manager.get_compaction_summarize_with_model():
                summary_text = await self._summarize_context(dropped)
                if not summary_text:
                    summary_text = f"Compacted previous {len(dropped)} messages."
            else:
                summary_text = f"Compacted previous {len(dropped)} messages."

            if context_items:
                extra = [str(c.get("content", "")).strip() for c in context_items if c.get("content")]
                if extra:
                    joined = "\n".join(e for e in extra if e)
                    summary_text = f"{summary_text}\n{joined}".strip() if summary_text else joined

            # Map the first kept message back to its session-tree entry id. Message
            # indexes in self.messages are aligned with message-producing entries
            # except for the synthetic compaction summary (at most one, at index 0).
            entry_ids = self.session_manager.get_message_entry_ids()
            offset = len(entry_ids) - total
            first_kept_id = "root"
            idx = keep_start + offset
            if 0 <= idx < len(entry_ids):
                first_kept_id = entry_ids[idx]
            elif entry_ids:
                first_kept_id = entry_ids[0]

            self.session_manager.append_compaction(summary_text, first_kept_id, tokens_before=tokens_before)
            self.messages = self.messages[keep_start:]
            # Keep the summary in the live context (rolling two-tier schema):
            # new_summary + last ~recentTokens raw messages.
            self.messages.insert(
                0,
                {
                    "role": "custom",
                    "customType": "compaction_summary",
                    "content": summary_text,
                    "tokensBefore": tokens_before,
                    "timestamp": int(time.time() * 1000),
                },
            )
            result = {
                "aborted": False,
                "summary": summary_text,
                "tokensBefore": tokens_before,
                "kept": len(self.messages),
                "skipped": False,
            }
            self._emit({"type": "compaction_end", "reason": reason, "result": result, "aborted": False, "willRetry": False})
            return result
        finally:
            self._is_compacting = False

    async def _summarize_context(self, dropped: list[dict[str, Any]]) -> str | None:
        """Rolling summary: previous summary + context since last compaction -> new summary."""
        if not self.model:
            return None
        prev = self.session_manager.get_last_compaction()
        prev_summary = str((prev or {}).get("summary") or "").strip()

        # The previous compaction summary already lives as a synthetic message in
        # `dropped`; the model input gets it once via prev_summary.
        history = [m for m in dropped if not (m.get("role") == "custom" and m.get("customType") in {"compaction_summary", "plan"})]
        context = "\n".join(
            f"{m.get('role', '?')}: {m.get('content', '')}"
            for m in self._flatten_conversation(history)
            if str(m.get("content", "")).strip()
        )
        max_input_tokens = self.settings_manager.get_compaction_max_summary_input_tokens()
        max_chars = max_input_tokens * 4
        if len(context) > max_chars:
            head = int(max_chars * 0.7)
            context = context[:head] + "\n\n...[truncated]...\n\n" + context[-(max_chars - head):]

        prompt_text = (
            "Summarize the conversation history below into one compact paragraph. "
            "Preserve decisions, file paths, tool outcomes, unresolved tasks and the user's goals. "
            "Do not invent new information.\n\n"
            f"Previous summary:\n{prev_summary or '(none)'}\n\n"
            "History since last compaction:\n"
            f"{context}"
        )
        msgs = [
            {"role": "system", "content": "You are a conversation summarizer."},
            {"role": "user", "content": prompt_text},
        ]
        try:
            res = await self._invoke_provider(msgs, allow_live_stream=False)
            text = self._assistant_text(res).strip()
            return text or None
        except Exception:
            return None

    def abort_compaction(self) -> None:
        self._is_compacting = False

    async def execute_bash(self, command: str) -> dict[str, Any]:
        from one.tools.bash import bash_tool

        async def _run() -> dict[str, Any]:
            result = await bash_tool(
                self.session_manager.cwd,
                command,
                timeout=self.settings_manager.get_tool_timeout_sec(),
                command_prefix=self.settings_manager.get_shell_command_prefix(),
            )
            msg = {
                "role": "bashExecution",
                "command": command,
                "output": result.get("output", ""),
                "exitCode": result.get("exitCode"),
                "cancelled": result.get("cancelled", False),
                "truncated": result.get("truncated", False),
                "fullOutputPath": result.get("fullOutputPath"),
                "timestamp": int(time.time() * 1000),
            }
            self.messages.append(msg)
            self.session_manager.append_message(msg)
            return result

        task = asyncio.create_task(_run())
        self._active_bash_tasks.add(task)
        try:
            return await task
        finally:
            self._active_bash_tasks.discard(task)

    def abort_bash(self) -> None:
        for task in list(self._active_bash_tasks):
            if not task.done():
                task.cancel()

    async def wait_for_idle(self) -> None:
        while self._is_streaming or self._is_compacting:
            await asyncio.sleep(0.02)

    async def abort(self) -> None:
        self._abort_requested = True
        self.abort_bash()
        for task in list(self._active_tool_tasks):
            if not task.done():
                task.cancel()
        for task in list(self._active_chat_tasks):
            if not task.done():
                task.cancel()
        # Wake up any pending ask_user questions so they can detect _abort_requested.
        for entry in self._pending_questions.values():
            entry["event"].set()

    async def navigate_tree(self, target_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        if self.session_manager.get_leaf_id() == target_id:
            return {"cancelled": False}
        target = self.session_manager.get_entry(target_id)
        if not target:
            raise ValueError(f"Entry {target_id} not found")

        summarize = bool(options.get("summarize", False))
        summary_entry = None
        old_leaf = self.session_manager.get_leaf_id()
        if summarize and old_leaf:
            summary_text = options.get("customInstructions") or "Branch summary"
            summary_id = self.session_manager.branch_with_summary(target.get("parentId") if target.get("type") == "message" and target.get("message", {}).get("role") == "user" else target_id, summary_text)
            summary_entry = self.session_manager.get_entry(summary_id)
        else:
            if target.get("type") == "message" and target.get("message", {}).get("role") == "user":
                if target.get("parentId") is None:
                    self.session_manager.reset_leaf()
                else:
                    self.session_manager.branch(target["parentId"])
            else:
                self.session_manager.branch(target_id)

        if options.get("label"):
            if summary_entry:
                self.session_manager.append_label_change(summary_entry["id"], options["label"])
            else:
                self.session_manager.append_label_change(target_id, options["label"])

        self.messages = self.session_manager.build_session_context()["messages"]
        self._restore_plan_from_messages(self.messages)
        self._emit({"type": "session_tree", "newLeafId": self.session_manager.get_leaf_id(), "oldLeafId": old_leaf, "summaryEntry": summary_entry})
        editor_text = None
        if target.get("type") == "message" and target.get("message", {}).get("role") == "user":
            c = target.get("message", {}).get("content", "")
            editor_text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c if x.get("type") == "text")
        return {"cancelled": False, "editorText": editor_text, "summaryEntry": summary_entry}

    def get_user_messages_for_forking(self) -> list[dict[str, str]]:
        out = []
        for e in self.session_manager.get_entries():
            if e.get("type") != "message":
                continue
            m = e.get("message", {})
            if m.get("role") != "user":
                continue
            c = m.get("content", "")
            text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c if x.get("type") == "text")
            if text:
                out.append({"entryId": e["id"], "text": text})
        return out

    def get_last_assistant_text(self) -> str | None:
        for m in reversed(self.messages):
            if m.get("role") != "assistant":
                continue
            content = m.get("content", "")
            if isinstance(content, list):
                text = "".join(x.get("text", "") for x in content if x.get("type") == "text")
            else:
                text = str(content)
            if text.strip():
                return text.strip()
        return None

    def get_last_finish_result(self) -> dict[str, Any]:
        """Result of the last `finish` tool call, if any.

        Returns {"finished": bool, "goalSuccess": bool, "summary": str}.
        """
        for m in reversed(self.messages):
            if m.get("role") == "assistant" and "goalSuccess" in m:
                return {
                    "finished": True,
                    "goalSuccess": bool(m.get("goalSuccess")),
                    "summary": self._assistant_text(m).strip(),
                }
        return {"finished": False, "goalSuccess": False, "summary": ""}

    def get_last_user_text(self) -> str | None:
        """Last non-empty user message text (used by `one run --resume`)."""
        for m in reversed(self.messages):
            if m.get("role") != "user":
                continue
            content = m.get("content", "")
            if isinstance(content, list):
                text = "".join(x.get("text", "") for x in content if x.get("type") == "text")
            else:
                text = str(content)
            if text.strip():
                return text.strip()
        return None

    def get_context_usage(self) -> dict[str, Any] | None:
        if not self.model or not self.model.context_window:
            return None
        approx_tokens = self._estimate_request_tokens()
        percent = (approx_tokens / self.model.context_window) * 100
        return {"tokens": approx_tokens, "contextWindow": self.model.context_window, "percent": percent}

    def _budget_exceeded(self) -> tuple[str, str] | None:
        """Return (kind, message) when a configured budget limit is exceeded."""
        max_tokens = self.settings_manager.get_budget_max_tokens()
        max_time = self.settings_manager.get_budget_max_time_sec()
        if max_tokens > 0:
            stats = self.get_session_stats()
            total = int((stats.get("tokens") or {}).get("total") or 0)
            if total >= max_tokens:
                return ("token_budget", f"Token budget reached ({total}/{max_tokens}).")
        if max_time > 0:
            elapsed = int(time.monotonic() - self._session_started_at)
            if elapsed >= max_time:
                return ("time_budget", f"Time budget reached ({elapsed}s/{max_time}s).")
        return None

    @staticmethod
    def _is_context_limit_error(error_text: str) -> bool:
        """Detect common context-length / token-limit errors from any provider.

        Provider adapters wrap HTTP errors as ``RuntimeError("name API error
        N: body")`` – the body is provider JSON.  We use generic case-insensitive
        patterns that cover OpenAI, Anthropic, OpenRouter, Gemini, llama.cpp,
        and other OpenAI-compatible back-ends.
        """
        lower = error_text.lower()
        return (
            "context" in lower and ("length" in lower or "limit" in lower or "window" in lower)
            or "maximum" in lower and "context" in lower
            or "too many" in lower and "token" in lower
        )

    def get_session_stats(self) -> dict[str, Any]:
        user_messages = sum(1 for m in self.messages if m.get("role") == "user")
        assistant_messages = sum(1 for m in self.messages if m.get("role") == "assistant")
        tool_results = sum(1 for m in self.messages if m.get("role") == "toolResult")
        tool_calls = tool_results
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        cache_write = 0
        total_cost = 0

        for m in self.messages:
            if m.get("role") == "assistant":
                usage = m.get("usage", {})
                input_tokens += int(usage.get("input", 0))
                output_tokens += int(usage.get("output", 0))
                cache_read += int(usage.get("cacheRead", 0))
                cache_write += int(usage.get("cacheWrite", 0))
                total_cost += float((usage.get("cost") or {}).get("total", 0))

        return {
            "sessionFile": self.session_file,
            "sessionId": self.session_id,
            "userMessages": user_messages,
            "assistantMessages": assistant_messages,
            "toolCalls": tool_calls,
            "toolResults": tool_results,
            "totalMessages": len(self.messages),
            "tokens": {
                "input": input_tokens,
                "output": output_tokens,
                "cacheRead": cache_read,
                "cacheWrite": cache_write,
                "total": input_tokens + output_tokens + cache_read + cache_write,
            },
            "cost": total_cost,
            "contextUsage": self.get_context_usage(),
            "nudge": {
                "fires": self._nudge_fires,
                "conversions": dict(self._nudge_conversions),
            },
        }

    async def export_to_html(self, output_path: str | None = None) -> str:
        import html as html_mod
        from pathlib import Path

        p = Path(output_path or f"session-{self.session_id}.html").resolve()
        p.parent.mkdir(parents=True, exist_ok=True)

        header = self.session_manager.get_header()
        created = header.get("timestamp", "")
        model_label = f"{self.model.provider}/{self.model.id}" if self.model else "—"
        name = self.session_name
        title = f"session {name or self.session_id}"

        css = """
        body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 24px; color: #1c1e21; background: #fff; }
        h1 { font-size: 18px; }
        .meta { color: #667; font-size: 13px; margin-bottom: 24px; }
        .meta div { margin: 2px 0; }
        .msg { border: 1px solid #e4e6e8; border-radius: 6px; margin: 12px 0; padding: 10px 14px; }
        .msg-head { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; color: #889; margin-bottom: 6px; }
        .msg pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 13.5px; line-height: 1.45; }
        .msg-user { background: #f0f7ff; border-color: #bcd8f5; }
        .msg-assistant { background: #f6f8fa; }
        .msg-toolResult { background: #fbf6ee; border-color: #e8dcc2; }
        .msg-toolResult .msg-head.ok { color: #1a7f37; }
        .msg-toolResult .msg-head.err { color: #b42318; }
        .msg-system, .msg-custom { background: #f4f4f6; border-style: dashed; }
        """
        out = [
            "<!doctype html><html><head><meta charset='utf-8'>",
            f"<title>{html_mod.escape(title)}</title><style>{css}</style></head><body>",
            f"<h1>{html_mod.escape(title)}</h1>",
            "<div class='meta'>",
            f"<div>session id: {html_mod.escape(str(self.session_id))}</div>",
            f"<div>cwd: {html_mod.escape(self.session_manager.cwd)}</div>",
            f"<div>model: {html_mod.escape(model_label)}</div>",
            f"<div>created: {html_mod.escape(str(created))}</div>",
            f"<div>messages: {len(self.messages)}</div>",
            "</div>",
        ]

        for m in self.messages:
            role = str(m.get("role", "?")).replace("_", " ")
            custom_type = m.get("customType")
            label = f"{role}:{custom_type}" if custom_type else role
            cls = f"msg msg-{role}" if role in {"user", "assistant", "toolResult", "system", "custom"} else "msg"
            content = m.get("content", "")
            if isinstance(content, list):
                text = "".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
            else:
                text = str(content)
            badge = ""
            if role == "toolresult":
                try:
                    payload = json.loads(text)
                    badge = "ok" if payload.get("ok") else "err"
                    text = json.dumps(payload, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            head_cls = f" msg-head {badge}" if badge else ""
            out.append(
                f"<div class='{cls}'><div class='msg-head{head_cls}'>{html_mod.escape(label)}</div>"
                f"<pre>{html_mod.escape(text)}</pre></div>"
            )

        out.append("</body></html>")
        p.write_text("".join(out), encoding="utf-8")
        return str(p)

    def export_to_jsonl(self, output_path: str | None = None) -> str:
        return self.session_manager.export_to_jsonl(output_path)

    def _ensure_extension_runtime(self) -> ExtensionRuntime:
        if self._extension_runtime is None:
            self._extension_runtime = ExtensionRuntime()
        return self._extension_runtime

    def _extension_context(self) -> ExtensionContext:
        cwd = getattr(self.resource_loader, "cwd", None) or os.getcwd()
        return ExtensionContext(
            directory=str(cwd),
            worktree=find_worktree(str(cwd)),
            session_id=self.session_id,
            model=f"{self.model.provider}/{self.model.id}" if self.model else None,
            settings=self.settings_manager.get_global_settings(),
        )

    def _emit_extension_error(self, path: str, hook: str, error: Exception) -> None:
        self._emit(
            {
                "type": "extension_load_error",
                "path": path,
                "stage": "hook",
                "hook": hook,
                "error": str(error).strip() or error.__class__.__name__,
                "errorType": error.__class__.__name__,
            }
        )

    async def _invoke_extension_before_tool(self, tool_name: str, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        assert self._extension_runtime is not None
        return await self._extension_runtime.call_before_tool(tool_name, args, self._emit_extension_error)

    async def _invoke_extension_after_tool(
        self,
        tool_name: str,
        ok: bool,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._extension_runtime is None or not self._extension_runtime.has_hooks("tool.execute.after"):
            return
        await self._extension_runtime.call_after_tool(tool_name, ok, text, metadata, self._emit_extension_error)

    async def _invoke_extension_chat_message(self, message: dict[str, Any]) -> None:
        assert self._extension_runtime is not None
        await self._extension_runtime.call_chat_message(message, self._emit_extension_error)

    async def _invoke_extension_compacting(self) -> tuple[list[dict[str, Any]], str | None]:
        assert self._extension_runtime is not None
        return await self._extension_runtime.call_compacting(self._emit_extension_error)

    async def bind_extensions(self, bindings: dict[str, Any] | None = None) -> None:
        """Load discovered extensions into the session (opencode-style hooks).

        Each discovered ``.py`` extension must export ``register(ctx) -> hooks``.
        ``bindings`` is accepted for API parity with the reference
        implementation; the extensions discovered by the ResourceLoader are
        what actually gets bound. Load/bind errors are emitted as
        ``extension_load_error`` events.
        """
        runtime = self._ensure_extension_runtime()
        extensions: list[dict[str, Any]] = []
        getter: Any = getattr(self.resource_loader, "get_extensions", None)
        if callable(getter):
            try:
                data = getter()
                if isinstance(data, dict):
                    extensions = list(data.get("extensions", []))
            except Exception:
                extensions = []
        errors = await runtime.bind(extensions, self._extension_context())
        for err in errors:
            self._emit({"type": "extension_load_error", **err})

    async def reload(self) -> dict[str, Any]:
        """Reload resources and rebind extensions safely.

        Returns a diagnostics dict with ``skills``, ``prompts``, ``themes``,
        ``extensions``, and ``diagnostics`` metadata so callers can display
        progress.
        """
        await self.resource_loader.reload()
        # Dispose the old extension runtime (calls dispose hooks), then
        # create a fresh one so old hooks cannot leak after reload.
        if self._extension_runtime is not None:
            try:
                await self._extension_runtime.call_dispose(self._emit_extension_error)
            except Exception:
                pass
            self._extension_runtime = None
        await self.bind_extensions()

        skills_info = self.resource_loader.get_skills()
        extensions_info = self.resource_loader.get_extensions()
        prompts_info = self.resource_loader.get_prompts()
        themes_info = self.resource_loader.get_themes()

        return {
            "skills": skills_info.get("skills", []),
            "diagnostics": skills_info.get("diagnostics", []),
            "extensions": {
                "count": len(extensions_info.get("extensions", [])),
                "errors": extensions_info.get("errors", []),
            },
            "prompts": {
                "count": len(prompts_info.get("prompts", [])),
                "diagnostics": prompts_info.get("diagnostics", []),
            },
            "themes": {
                "count": len(themes_info.get("themes", [])),
                "diagnostics": themes_info.get("diagnostics", []),
            },
        }

    async def invoke_skill(self, name: str, args_text: str = "") -> dict[str, Any]:
        """Load a skill body and trigger a prompt with the skill instructions.

        Loads the full SKILL.md via the resource loader, assembles the
        invocation text (skill body + optional *args_text*), and delegates
        to ``prompt()`` so the content is sent to the provider.  Returns a
        result dict with ``ok``, ``name``, ``bodyLength``, ``baseDir``, and
        optionally ``trustWarning``, ``allowedTools``, and
        ``disableModelInvocation``.

        When the session is currently streaming (a provider call is in flight),
        this method fails fast with ``ok=False`` / ``errorType=BusySessionError``
        rather than silently queuing the body — explicit skill invocation should
        not disappear into a queue without the caller knowing.
        """
        # Fail-fast: explicit skill invocation is not deferred during streaming.
        if self._is_streaming:
            return {
                "ok": False,
                "errorType": "BusySessionError",
                "error": (
                    "Session is busy — the model is still processing a previous "
                    "call. Use ``/follow`` to queue a message, or wait until the "
                    "agent becomes idle and retry."
                ),
            }

        skill = self.resource_loader.get_skill(name)
        if "error" in skill:
            return {
                "ok": False,
                "error": skill["error"],
                "errorType": "SkillError",
                "invalid": skill.get("invalid", False),
                "diagnostics": skill.get("diagnostics", []),
            }

        body = skill.get("body", "")
        skill_name = skill.get("name", name)
        base_dir = skill.get("baseDir", "")

        # Build the user message: skill body + any additional arguments.
        invocation = f"## Invoked skill: {skill_name}\n\n{body}"
        if args_text.strip():
            invocation += f"\n\n## User arguments\n\n{args_text.strip()}"

        # Delegate to prompt() — this handles message creation, event
        # emission, and the full provider call (turn loop).
        await self.prompt(invocation)

        result: dict[str, Any] = {
            "ok": True,
            "name": skill_name,
            "bodyLength": len(body),
            "baseDir": base_dir,
            "trustWarning": (
                "Skill instructions are loaded as user input. "
                "Review the skill before granting cooperation approval for file operations."
            ),
        }
        # Include skill metadata when available.
        fm = skill.get("fm", {})
        if fm:
            if "allowed-tools" in fm:
                result["allowedTools"] = fm["allowed-tools"]
            if "disable-model-invocation" in fm:
                result["disableModelInvocation"] = fm["disable-model-invocation"]
        return result

    async def dispose(self) -> None:
        if self._extension_runtime is not None:
            await self._extension_runtime.call_dispose(self._emit_extension_error)

        # Blob storage lifecycle: clean up transient blobs for in-memory
        # sessions, and run orphan cleanup for persistent sessions.
        storage = self._storage_dir
        if storage:
            # In-memory / transient sessions own a temp dir created by
            # tempfile.mkdtemp.  Remove the entire temp directory so no
            # blob files linger after the CLI exits.
            if getattr(self, "_owns_temp_dir", False):
                try:
                    import shutil
                    shutil.rmtree(storage, ignore_errors=True)
                except Exception:
                    pass
            else:
                # Persistent session: defer orphan cleanup.
                #
                # Image refs are stored transiently in ``_images`` /
                # ``_tool_images`` and are NEVER persisted to the JSONL
                # session file.  Because of this, we cannot reliably
                # reconstruct the set of blob hashes still referenced by
                # earlier turns — so running ``orphan_cleanup`` here would
                # delete valid blobs from prior prompts.
                #
                # Orphan cleanup is instead triggered by the next
                # ``prompt(images=...)`` call which knows the full set of
                # currently-referenced hashes.
                pass

    def request_extension_ui(
        self,
        extension: str,
        ui_type: str,
        payload: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        kind = (ui_type or "").strip().lower()
        if kind not in {"widget", "overlay"}:
            raise ValueError("uiType must be one of: widget, overlay")
        req = {
            "id": uuid.uuid4().hex[:12],
            "extension": extension.strip() or "unknown",
            "uiType": kind,
            "title": title or "",
            "payload": payload or {},
            "status": "pending",
            "createdAt": int(time.time() * 1000),
        }
        self._extension_ui_pending[req["id"]] = req
        self._emit({"type": "extension_ui_request", **req})
        return req

    def respond_extension_ui(
        self,
        request_id: str,
        payload: dict[str, Any] | None = None,
        cancelled: bool = False,
    ) -> dict[str, Any]:
        req = self._extension_ui_pending.pop(request_id, None)
        if not req:
            raise ValueError(f"Extension UI request not found: {request_id}")
        resp = {
            "requestId": request_id,
            "extension": req.get("extension"),
            "uiType": req.get("uiType"),
            "payload": payload or {},
            "cancelled": bool(cancelled),
            "createdAt": req.get("createdAt"),
            "respondedAt": int(time.time() * 1000),
        }
        self._extension_ui_history.append(resp)
        if len(self._extension_ui_history) > 100:
            self._extension_ui_history = self._extension_ui_history[-100:]
        self._emit({"type": "extension_ui_response", **resp})
        return resp

    def get_extension_ui_state(self) -> dict[str, Any]:
        pending = sorted(self._extension_ui_pending.values(), key=lambda x: int(x.get("createdAt", 0)))
        return {
            "pending": pending,
            "history": list(self._extension_ui_history),
        }

    def clear_extension_ui_history(self) -> None:
        self._extension_ui_history = []

    def inspect_subagent_timeout(self) -> dict[str, Any]:
        """Return the last subagent-timeout diagnostic, or an empty dict if none.

        Read-only, never mutates state.  Structured for machine consumption
        (RPC/get_state) and human display (CLI/TUI slash command).
        """
        if self._last_subagent_timeout is not None:
            return dict(self._last_subagent_timeout)
        return {}
