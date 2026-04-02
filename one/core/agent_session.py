from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.types import ModelInfo
from one.providers.registry import build_provider_registry
from one.tools.index import all_tools


@dataclass
class ModelCycleResult:
    model: ModelInfo
    thinkingLevel: str
    isScoped: bool


class AgentSession:
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
    ) -> None:
        self.session_manager = session_manager
        self.settings_manager = settings_manager
        self.model_registry = model_registry
        self.resource_loader = resource_loader
        self.model = model
        self.thinking_level = thinking_level
        self.scoped_models = scoped_models or []
        self.providers = build_provider_registry()
        self.messages: list[dict[str, Any]] = self.session_manager.build_session_context()["messages"]
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._is_streaming = False
        self._is_compacting = False
        self._retrying = False
        self._pending_bash_messages: list[dict[str, Any]] = []
        self._steering: list[str] = []
        self._follow_up: list[str] = []
        self._active_tools = tools or ["read", "bash", "edit", "write"]

        if not self.messages:
            if self.model:
                self.session_manager.append_model_change(self.model.provider, self.model.id)
            self.session_manager.append_thinking_level_change(self.thinking_level)

    def _assistant_text(self, message: dict[str, Any]) -> str:
        content = message.get("content", "")
        if isinstance(content, list):
            return "".join(x.get("text", "") for x in content if x.get("type") == "text")
        return str(content)

    def _tool_system_prompt(self) -> str:
        names = ", ".join(self._active_tools) if self._active_tools else "none"
        return (
            "You can use local tools.\n"
            f"Available tools: {names}.\n"
            "When a tool is needed, respond ONLY with JSON in this exact shape:\n"
            '{"tool":"<name>","args":{...}}\n'
            "Do not add markdown, code fences, or extra text in tool-call responses.\n"
            "When enough information is available, respond normally in plain text."
        )

    def _try_parse_tool_call(self, text: str) -> dict[str, Any] | None:
        candidates: list[str] = []
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            candidates.append(stripped)

        for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text):
            candidates.append(m.group(1).strip())

        marker = "TOOL_CALL:"
        if marker in text:
            candidates.append(text.split(marker, 1)[1].strip())

        for c in candidates:
            try:
                obj = json.loads(c)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            tool = obj.get("tool") or obj.get("name")
            args = obj.get("args") or obj.get("input") or {}
            if isinstance(tool, str) and isinstance(args, dict):
                return {"tool": tool, "args": args}
        return None

    async def _execute_tool_by_name(self, tool_name: str, args: dict[str, Any], timeout_sec: int | None = None) -> dict[str, Any]:
        if tool_name not in self._active_tools:
            raise RuntimeError(f"Tool '{tool_name}' is disabled")
        tool = all_tools.get(tool_name)
        if not tool:
            raise RuntimeError(f"Unknown tool: {tool_name}")

        cwd = self.session_manager.cwd
        fn = tool.fn
        if tool_name == "read":
            result = fn(cwd, args.get("path", ""), args.get("offset"), args.get("limit"))
        elif tool_name == "write":
            result = fn(cwd, args.get("path", ""), args.get("content", ""))
        elif tool_name == "edit":
            result = fn(cwd, args.get("path", ""), args.get("edits", []))
        elif tool_name == "grep":
            result = fn(cwd, args.get("pattern", ""), args.get("path", "."))
        elif tool_name == "find":
            result = fn(cwd, args.get("pattern", "*"), args.get("path", "."))
        elif tool_name == "ls":
            result = fn(cwd, args.get("path", "."))
        elif tool_name == "bash":
            result = fn(cwd, args.get("command", ""), args.get("timeout"), self.settings_manager.get_shell_command_prefix())
        else:
            raise RuntimeError(f"Unsupported tool: {tool_name}")

        if inspect.isawaitable(result):
            if timeout_sec and timeout_sec > 0:
                result = await asyncio.wait_for(result, timeout=timeout_sec)
            else:
                result = await result
        if not isinstance(result, dict):
            raise RuntimeError(f"Invalid result from tool: {tool_name}")
        return result

    async def _run_tool_call(self, tool_name: str, args: dict[str, Any], timeout_sec: int | None = None) -> dict[str, Any]:
        self._emit({"type": "tool_call_start", "tool": tool_name, "args": args})
        try:
            result = await self._execute_tool_by_name(tool_name, args, timeout_sec=timeout_sec)
            text = ""
            content = result.get("content")
            if isinstance(content, list):
                text = "".join(x.get("text", "") for x in content if isinstance(x, dict))
            if not text:
                text = str(result.get("output", ""))
            payload = {
                "ok": True,
                "tool": tool_name,
                "args": args,
                "result": text,
                "rawResult": result,
            }
        except Exception as e:
            error_text = str(e).strip() or e.__class__.__name__
            payload = {
                "ok": False,
                "tool": tool_name,
                "args": args,
                "error": error_text,
            }

        msg = {
            "role": "toolResult",
            "content": json.dumps(payload, ensure_ascii=False),
            "timestamp": int(time.time() * 1000),
        }
        self.messages.append(msg)
        self.session_manager.append_message(msg)
        self._emit({"type": "tool_call_end", "tool": tool_name, "result": payload})
        return payload

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
        self.model = model
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

    async def _invoke_provider(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.model:
            raise RuntimeError("No model selected")
        provider = self.providers.get(self.model.provider)
        if not provider:
            raise RuntimeError(f"Unsupported provider: {self.model.provider}")

        auth = self.model_registry.get_api_key_and_headers(self.model)
        if not auth.get("ok"):
            raise RuntimeError(auth.get("error", "Authentication failed"))

        res = await provider.chat(
            api_key=auth["apiKey"],
            model=self.model.id,
            messages=messages,
            thinking_level=self.thinking_level,
            headers=auth.get("headers"),
        )

        return {
            "role": "assistant",
            "content": [{"type": "text", "text": res.text}],
            "provider": self.model.provider,
            "model": self.model.id,
            "usage": {
                "input": res.usage.get("prompt_tokens") or res.usage.get("input_tokens") or 0,
                "output": res.usage.get("completion_tokens") or res.usage.get("output_tokens") or 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "cost": {"total": 0},
            },
            "stopReason": res.stop_reason,
            "timestamp": int(time.time() * 1000),
        }

    def _flatten_messages_for_provider(self) -> list[dict[str, Any]]:
        msgs = [
            {"role": "system", "content": self.resource_loader.get_system_prompt()},
            {"role": "system", "content": self._tool_system_prompt()},
        ]
        for m in self.messages:
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
            msgs.append({"role": role, "content": text})
        return msgs

    async def prompt(
        self,
        text: str,
        options: dict[str, Any] | None = None,
    ) -> None:
        options = options or {}
        if self._is_streaming:
            behavior = options.get("streamingBehavior")
            if not behavior:
                raise RuntimeError("streamingBehavior is required while streaming")
            if behavior == "steer":
                await self.steer(text)
                return
            if behavior == "followUp":
                await self.follow_up(text)
                return
            raise RuntimeError("Invalid streamingBehavior")

        self._is_streaming = True
        self._emit({"type": "agent_start"})
        user_msg = {"role": "user", "content": text, "timestamp": int(time.time() * 1000)}
        self.messages.append(user_msg)
        self.session_manager.append_message(user_msg)
        self._emit({"type": "message_start", "message": user_msg})
        self._emit({"type": "message_end", "message": user_msg})

        retry_cfg = self.settings_manager.get_retry_settings()
        attempt = 0
        while True:
            try:
                self._emit({"type": "turn_start"})
                tool_results: list[dict[str, Any]] = []
                max_tool_steps = max(1, self.settings_manager.get_tool_max_steps())
                tool_timeout_sec = self.settings_manager.get_tool_timeout_sec()
                final_assistant: dict[str, Any] | None = None
                for _ in range(max_tool_steps):
                    assistant = await self._invoke_provider(self._flatten_messages_for_provider())
                    assistant_text = self._assistant_text(assistant)
                    tool_call = self._try_parse_tool_call(assistant_text)

                    if tool_call:
                        tool_payload = await self._run_tool_call(tool_call["tool"], tool_call["args"], timeout_sec=tool_timeout_sec)
                        tool_results.append(tool_payload)
                        continue

                    final_assistant = assistant
                    break

                if final_assistant is None:
                    final_assistant = {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "Wykonałem narzędzia, ale osiągnięto limit kroków. Doprecyzuj proszę kolejne polecenie.",
                            }
                        ],
                        "provider": self.model.provider if self.model else None,
                        "model": self.model.id if self.model else None,
                        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
                        "stopReason": "tool_step_limit",
                        "timestamp": int(time.time() * 1000),
                    }

                self.messages.append(final_assistant)
                self.session_manager.append_message(final_assistant)
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
                self._emit({"type": "turn_end", "message": final_assistant, "toolResults": tool_results})
                self._emit({"type": "agent_end", "messages": [user_msg, final_assistant]})
                break
            except Exception as e:
                if not retry_cfg.get("enabled", True) or attempt >= int(retry_cfg.get("maxRetries", 3)):
                    error_msg = {
                        "role": "assistant",
                        "content": [{"type": "text", "text": str(e)}],
                        "provider": self.model.provider if self.model else None,
                        "model": self.model.id if self.model else None,
                        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
                        "stopReason": "error",
                        "timestamp": int(time.time() * 1000),
                    }
                    self.messages.append(error_msg)
                    self.session_manager.append_message(error_msg)
                    self._emit({"type": "message_end", "message": error_msg})
                    self._emit({"type": "agent_end", "messages": [user_msg, error_msg]})
                    break
                attempt += 1
                delay_ms = min(int(retry_cfg.get("baseDelayMs", 1500)) * (2 ** (attempt - 1)), int(retry_cfg.get("maxDelayMs", 20000)))
                self._retrying = True
                self._emit(
                    {
                        "type": "auto_retry_start",
                        "attempt": attempt,
                        "maxAttempts": int(retry_cfg.get("maxRetries", 3)),
                        "delayMs": delay_ms,
                        "errorMessage": str(e),
                    }
                )
                await asyncio.sleep(delay_ms / 1000)
                self._emit({"type": "auto_retry_end", "success": attempt <= int(retry_cfg.get("maxRetries", 3)), "attempt": attempt})
                self._retrying = False

        self._is_streaming = False

        if self._steering:
            msg = self._steering.pop(0)
            await self.prompt(msg)
        elif self._follow_up:
            msg = self._follow_up.pop(0)
            await self.prompt(msg)

    async def steer(self, text: str, images: list[dict[str, Any]] | None = None) -> None:
        self._steering.append(text)
        self._emit({"type": "queue_update", "steering": list(self._steering), "followUp": list(self._follow_up)})

    async def follow_up(self, text: str, images: list[dict[str, Any]] | None = None) -> None:
        self._follow_up.append(text)
        self._emit({"type": "queue_update", "steering": list(self._steering), "followUp": list(self._follow_up)})

    async def compact(self, custom_instructions: str | None = None) -> dict[str, Any]:
        self._is_compacting = True
        self._emit({"type": "compaction_start", "reason": "manual"})
        if not self.messages:
            self._is_compacting = False
            result = {"aborted": False, "summary": "", "tokensBefore": 0}
            self._emit({"type": "compaction_end", "reason": "manual", "result": result, "aborted": False, "willRetry": False})
            return result

        keep_start = max(0, len(self.messages) - 20)
        old_count = len(self.messages)
        summary_text = custom_instructions or f"Compacted previous {keep_start} messages."

        entries = self.session_manager.get_branch()
        first_keep_id = entries[keep_start]["id"] if entries and keep_start < len(entries) else entries[0]["id"] if entries else "root"
        self.session_manager.append_compaction(summary_text, first_keep_id, tokens_before=old_count)

        self.messages = self.messages[keep_start:]
        self._is_compacting = False
        result = {"aborted": False, "summary": summary_text, "tokensBefore": old_count, "kept": len(self.messages)}
        self._emit({"type": "compaction_end", "reason": "manual", "result": result, "aborted": False, "willRetry": False})
        return result

    def abort_compaction(self) -> None:
        self._is_compacting = False

    async def execute_bash(self, command: str) -> dict[str, Any]:
        from one.tools.bash import bash_tool

        result = await bash_tool(
            self.session_manager.cwd,
            command,
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

    def abort_bash(self) -> None:
        return

    async def wait_for_idle(self) -> None:
        while self._is_streaming:
            await asyncio.sleep(0.02)

    async def abort(self) -> None:
        self._is_streaming = False

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

    def get_context_usage(self) -> dict[str, Any] | None:
        if not self.model or not self.model.context_window:
            return None
        approx_tokens = sum(max(1, len(str(m.get("content", ""))) // 4) for m in self.messages)
        percent = (approx_tokens / self.model.context_window) * 100
        return {"tokens": approx_tokens, "contextWindow": self.model.context_window, "percent": percent}

    def get_session_stats(self) -> dict[str, Any]:
        user_messages = sum(1 for m in self.messages if m.get("role") == "user")
        assistant_messages = sum(1 for m in self.messages if m.get("role") == "assistant")
        tool_results = sum(1 for m in self.messages if m.get("role") == "toolResult")
        tool_calls = 0
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
        }

    async def export_to_html(self, output_path: str | None = None) -> str:
        from pathlib import Path

        p = Path(output_path or f"session-{self.session_id}.html").resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        html = ["<html><body><pre>"]
        for m in self.messages:
            html.append(f"[{m.get('role')}] {m.get('content')}\n")
        html.append("</pre></body></html>")
        p.write_text("".join(html), encoding="utf-8")
        return str(p)

    def export_to_jsonl(self, output_path: str | None = None) -> str:
        return self.session_manager.export_to_jsonl(output_path)

    async def bind_extensions(self, bindings: dict[str, Any] | None = None) -> None:
        return

    async def reload(self) -> None:
        await self.resource_loader.reload()

    async def dispose(self) -> None:
        return
