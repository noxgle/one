from __future__ import annotations

import atexit
import json
from pathlib import Path
from typing import Any

from one.core.types import ModelInfo
from one.config import get_agent_dir

try:
    import readline  # type: ignore
except Exception:  # pragma: no cover
    readline = None


class InteractiveMode:
    _THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh"]

    def __init__(self, runtime_host: Any, options: dict[str, Any] | None = None) -> None:
        self.runtime_host = runtime_host
        self.options = options or {}
        self._history_initialized = False

    def _setup_readline(self) -> None:
        if self._history_initialized:
            return
        self._history_initialized = True
        if readline is None:
            return
        try:
            history_path = Path(get_agent_dir()) / "interactive.history"
            history_path.parent.mkdir(parents=True, exist_ok=True)
            if history_path.exists():
                readline.read_history_file(str(history_path))
            readline.parse_and_bind("set editing-mode emacs")
            readline.parse_and_bind("tab: complete")
            readline.parse_and_bind('"\\C-l": clear-screen')
            def _save_history() -> None:
                try:
                    readline.write_history_file(str(history_path))
                except Exception:
                    return

            atexit.register(_save_history)
        except Exception:
            # History/key bindings are best-effort only.
            return

    async def run(self) -> None:
        self._setup_readline()
        session = self.runtime_host.session
        assistant_streamed = False
        retry_state = "idle"
        printed_banner = False

        def on_event(event: dict) -> None:
            nonlocal assistant_streamed, retry_state
            et = event.get("type")
            if event.get("type") == "message_start":
                msg = event.get("message", {})
                if msg.get("role") == "assistant":
                    assistant_streamed = False
            if event.get("type") == "message_update":
                ae = event.get("assistantMessageEvent", {})
                if ae.get("type") == "text_delta":
                    assistant_streamed = True
                    print(ae.get("delta", ""), end="", flush=True)
            if event.get("type") == "message_end":
                msg = event.get("message", {})
                if msg.get("role") != "assistant":
                    return
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = "".join(x.get("text", "") for x in content if x.get("type") == "text")
                else:
                    text = str(content)
                text = text.strip()
                if not assistant_streamed and text:
                    print(text, end="", flush=True)
                if text:
                    print("", flush=True)
                elif not assistant_streamed:
                    print("[Brak treści odpowiedzi modelu]", flush=True)
            if et == "turn_end" and event.get("ok") is False:
                err = (event.get("error") or "Unknown error").strip()
                print(f"[Błąd] {err}", flush=True)
            if et == "tool_call_start":
                print(f"[Tool] {event.get('tool')} {json.dumps(event.get('args', {}), ensure_ascii=False)}", flush=True)
            if et == "tool_call_end":
                ok = bool(event.get("ok"))
                status = "OK" if ok else "ERR"
                print(f"[Tool:{status}] {event.get('tool')}", flush=True)
            if et == "tool_call_parse_failed":
                print("[Tool] Parse failed for tool-call candidate, continuing with assistant output.", flush=True)
            if et == "tool_call_nudge_start":
                print("[Tool] Requesting tool-call nudge...", flush=True)
            if et == "tool_call_nudge_end":
                print(f"[Tool] Nudge used: {bool(event.get('used'))}", flush=True)
            if et == "auto_retry_start":
                retry_state = f"retry-{event.get('attempt')}"
                print(
                    f"[Retry] próba {event.get('attempt')}/{event.get('maxAttempts')} za {event.get('delayMs')}ms: {event.get('errorMessage')}",
                    flush=True,
                )
            if et == "auto_retry_end":
                retry_state = "idle"
            if et == "turn_end":
                retry_state = "idle"
            if et == "queue_update":
                q = event
                print(f"[Queue] s={len(q.get('steering', []))} f={len(q.get('followUp', []))}", flush=True)

        session.subscribe(on_event)
        while True:
            if not printed_banner:
                print("Interactive mode. Type /exit to quit. Use /help for commands.")
                print("Shortcuts: Ctrl+C abort/exit | Ctrl+L clear | Ctrl+R history search | Up/Down history")
                printed_banner = True
            model_label = f"{session.model.provider}/{session.model.id}" if session.model else "no-model"
            usage = session.get_context_usage()
            usage_text = ""
            if usage:
                usage_text = f" | ctx:{usage['percent']:.1f}%"
            stats = session.get_session_stats()
            tokens_total = int(((stats.get("tokens") or {}).get("total")) or 0)
            queues = session.get_pending_queues()
            cwd_label = session.session_manager.cwd
            print(
                f"[{model_label} | thinking:{session.thinking_level}{usage_text} | tokens:{tokens_total} | retry:{retry_state} | cwd:{cwd_label} | queue:s{len(queues['steering'])}/f{len(queues['followUp'])}]"
            )
            print("[/help | /status | /queue [clear] | /model [provider/model] | /thinking [level] | /abort | /exit]")
            try:
                line = input("\n> ")
            except EOFError:
                print("")
                break
            except KeyboardInterrupt:
                if session.is_streaming:
                    await session.abort()
                    print("\n[Abort requested]", flush=True)
                    continue
                print("")
                break
            if not line.strip():
                continue
            aliases = {
                "/q": "/exit",
                "/h": "/help",
                "/s": "/status",
                "/st": "/status",
                "/m": "/model",
                "/t": "/thinking",
                "/c": "/clear",
                "/qq": "/queue clear",
                "/mc": "/model-cycle",
                "/tc": "/thinking-cycle",
            }
            line = aliases.get(line.strip(), line)
            if line.strip() in {"/exit", "/quit"}:
                break
            if line.strip() == "/help":
                print(
                    "/exit /quit | /help | /stats | /state /status | /queue | /tools | /clear | /abort\n"
                    "/model [provider/model] | /model-cycle | /thinking [level] | /thinking-cycle\n"
                    "/steer <text> | /follow <text> | /compact [instructions] | /login [status|provider [apiKey] [model]] | /logout <provider>\n"
                    "/retry <on|off> | /config [key] [value]\n"
                    "/bash <command>"
                )
                continue
            if line.strip() == "/stats":
                print(json.dumps(session.get_session_stats(), ensure_ascii=False, indent=2))
                continue
            if line.strip() in {"/state", "/status"}:
                print(
                    json.dumps(
                        {
                            "model": {"provider": session.model.provider, "id": session.model.id} if session.model else None,
                            "thinkingLevel": session.thinking_level,
                            "isStreaming": session.is_streaming,
                            "pendingMessageCount": session.pending_message_count,
                            "pendingQueues": session.get_pending_queues(),
                            "activeTools": session.active_tools,
                            "sessionId": session.session_id,
                            "sessionFile": session.session_file,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                continue
            if line.strip() == "/queue":
                print(json.dumps(session.get_pending_queues(), ensure_ascii=False, indent=2))
                continue
            if line.startswith("/queue "):
                mode = line[len("/queue ") :].strip().lower()
                if mode.startswith("clear"):
                    target = "all"
                    parts = mode.split(maxsplit=1)
                    if len(parts) == 2:
                        target = parts[1]
                    try:
                        cleared = session.clear_pending_queues(target)
                    except ValueError:
                        print("Usage: /queue clear [all|steering|follow]")
                        continue
                    print(json.dumps(cleared, ensure_ascii=False, indent=2))
                    continue
                print("Usage: /queue or /queue clear [all|steering|follow]")
                continue
            if line.strip() == "/tools":
                print(json.dumps({"tools": session.active_tools}, ensure_ascii=False, indent=2))
                continue
            if line.strip() == "/clear":
                print("\033[2J\033[H", end="")
                continue
            if line.strip() == "/model":
                current = f"{session.model.provider}/{session.model.id}" if session.model else "none"
                providers = session.model_registry.providers()
                print(
                    json.dumps(
                        {
                            "current": current,
                            "providers": providers,
                            "usage": "/model <provider>/<model-id>",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                continue
            if line.strip() == "/abort":
                await session.abort()
                print("Abort requested.")
                continue
            if line.startswith("/model "):
                val = line[len("/model ") :].strip()
                if "/" not in val:
                    print("Usage: /model <provider>/<model-id>")
                    continue
                provider, model_id = val.split("/", 1)
                model = session.model_registry.resolve(provider, model_id, allow_dynamic=True)
                if not model:
                    print(f"Model not found: {provider}/{model_id}")
                    continue
                await session.set_model(model)
                if session.model_registry.find(provider, model_id) is None:
                    print(f"Model set to {provider}/{model_id} (dynamic)")
                else:
                    print(f"Model set to {provider}/{model_id}")
                continue
            if line.strip() == "/model-cycle":
                result = await session.cycle_model()
                if not result:
                    print("No available models to cycle.")
                else:
                    print(f"Model cycled to {result.model.provider}/{result.model.id}")
                continue
            if line.startswith("/thinking "):
                level = line[len("/thinking ") :].strip()
                session.set_thinking_level(level)
                print(f"Thinking level set to {level}")
                continue
            if line.strip() == "/thinking":
                print(
                    json.dumps(
                        {
                            "current": session.thinking_level,
                            "levels": list(self._THINKING_LEVELS),
                            "usage": "/thinking <off|minimal|low|medium|high|xhigh>",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                continue
            if line.strip() == "/thinking-cycle":
                level = session.cycle_thinking_level()
                print(f"Thinking level cycled to {level}")
                continue
            if line.startswith("/steer "):
                await session.steer(line[len("/steer ") :].strip())
                print("Queued steering message.")
                continue
            if line.startswith("/follow "):
                await session.follow_up(line[len("/follow ") :].strip())
                print("Queued follow-up message.")
                continue
            if line.startswith("/compact"):
                instructions = line[len("/compact") :].strip() or None
                result = await session.compact(instructions)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                continue
            if line.startswith("/login"):
                rest = line[len("/login") :].strip()
                parts = rest.split() if rest else []
                if parts and parts[0] == "status":
                    if len(parts) == 2:
                        print(
                            json.dumps(
                                session.model_registry.get_provider_auth_status(parts[1]),
                                ensure_ascii=False,
                                indent=2,
                            )
                        )
                    else:
                        providers = session.model_registry.providers()
                        statuses = [session.model_registry.get_provider_auth_status(p) for p in providers]
                        print(json.dumps({"providers": statuses}, ensure_ascii=False, indent=2))
                    continue
                provider = parts[0] if len(parts) >= 1 else input("Provider: ").strip()
                if not provider:
                    print("Provider is required.")
                    continue
                requires_api_key = session.model_registry.requires_api_key(provider)
                api_key = parts[1] if len(parts) >= 2 else (input("API key: ").strip() if requires_api_key else "")
                if requires_api_key and not api_key:
                    env_var = session.model_registry.get_provider_auth_status(provider).get("envVar")
                    hint = f" or set {env_var}" if env_var else ""
                    print(f"API key is required for {provider}{hint}.")
                    continue
                if len(parts) >= 3:
                    model_input = parts[2]
                elif parts:
                    model_input = ""
                else:
                    model_input = input("Default model (optional): ").strip()
                selected_model = None
                if model_input:
                    selected_model = session.model_registry.resolve(provider, model_input, allow_dynamic=True)
                    if not selected_model:
                        print(f"Model not found for {provider}: {model_input}")
                        continue
                if api_key:
                    session.model_registry.set_stored_api_key(provider, api_key)
                session.settings_manager.set_default_provider(provider)
                if selected_model:
                    session.settings_manager.set_default_model(selected_model.id)
                    await session.set_model(ModelInfo(provider=selected_model.provider, id=selected_model.id, reasoning=selected_model.reasoning, context_window=selected_model.context_window))
                print(
                    (f"Stored key for {provider}." if api_key else f"Configured provider {provider}.")
                    + (f" Default model set to {selected_model.id}." if selected_model else "")
                )
                continue
            if line.startswith("/logout "):
                provider = line[len("/logout ") :].strip()
                if not provider:
                    print("Usage: /logout <provider>")
                    continue
                session.model_registry.remove_stored_api_key(provider)
                print(f"Removed stored key for {provider}.")
                continue
            if line.startswith("/retry "):
                mode = line[len("/retry ") :].strip().lower()
                if mode not in {"on", "off"}:
                    print("Usage: /retry <on|off>")
                    continue
                session.set_auto_retry_enabled(mode == "on")
                print(f"Auto-retry set to {mode}.")
                continue
            if line.startswith("/config"):
                rest = line[len("/config") :].strip()
                if not rest:
                    print(json.dumps(session.settings_manager.get_global_settings(), ensure_ascii=False, indent=2))
                    continue
                parts = rest.split(maxsplit=1)
                if len(parts) == 1:
                    key = parts[0]
                    cur = session.settings_manager.merged()
                    for p in key.split("."):
                        if isinstance(cur, dict):
                            cur = cur.get(p)
                        else:
                            cur = None
                    print(json.dumps({"key": key, "value": cur}, ensure_ascii=False, indent=2))
                    continue
                key, raw = parts
                val: Any = raw
                try:
                    val = json.loads(raw)
                except Exception:
                    pass
                session.settings_manager.set_config_value(key, val)
                print(f"Updated {key}.")
                continue
            if line.startswith("/bash "):
                command = line[len("/bash ") :].strip()
                if not command:
                    print("Usage: /bash <command>")
                    continue
                try:
                    result = await session.execute_bash(command)
                    print((result.get("output") or "").rstrip())
                except Exception as e:
                    print(str(e))
                continue
            if line.startswith("/"):
                print(f"Unknown command: {line.strip()}. Use /help.")
                continue
            await session.prompt(line)

    async def init(self) -> None:
        return

    def stop(self) -> None:
        return
