from __future__ import annotations

import atexit
import json
import os
import select
import sys
from pathlib import Path
from typing import Any, Callable

from one.core.types import ModelInfo
from one.config import get_agent_dir

try:
    import readline  # type: ignore
except Exception:  # pragma: no cover
    readline = None


def _read_input_line(prompt: str, on_ctrl_a: Callable[[], None]) -> str:
    """Read one input line; Ctrl+A fires on_ctrl_a instantly (no Enter).

    Three paths, tried in order:

    1. GNU readline callback interface — full readline editing and an instant
       Ctrl+A hook. Only exists on builds compiled with HAVE_RL_CALLBACK
       (typical distro Python).
    2. A minimal raw-mode (termios) line editor — used on builds that lack the
       callback API (e.g. conda-forge Python), where readline cannot give us
       per-keystroke control. Still gives instant Ctrl+A, backspace, arrows,
       Home/End, Ctrl+U/K/L and history.
    3. Non-tty stdin (pipes, tests): plain input(); the caller then receives a
       raw "\\x01" line and handles the toggle itself.
    """
    if (
        readline is not None
        and sys.stdin.isatty()
        and hasattr(readline, "callback_handler_install")
    ):
        result: dict[str, str] = {}

        def _handler(line: str) -> None:
            result["line"] = line

        try:
            readline.callback_handler_install(prompt, _handler)
            while "line" not in result:
                r, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not r:
                    continue
                readline.callback_read_char()
                buf = readline.get_line_buffer()
                if "\x01" in buf:
                    readline.replace_line(buf.replace("\x01", ""))
                    on_ctrl_a()
                    readline.redisplay()
        finally:
            try:
                readline.callback_handler_remove()
            except Exception:
                pass
        return result.get("line", "")
    if sys.stdin.isatty():
        try:
            return _raw_readline(prompt, on_ctrl_a)
        except Exception:
            # Raw mode failed to set up (non-posix, weird tty) — fall back to
            # the readline-backed input().
            return input(prompt)
    return input(prompt)


def _raw_readline(prompt: str, on_ctrl_a: Callable[[], None]) -> str:
    """Minimal line editor for terminals without the readline callback API.

    Reads one char at a time in raw mode so Ctrl+A (\\x01) fires on_ctrl_a the
    moment the key is pressed. Implements a useful editing subset: printable
    (UTF-8) chars, Backspace, Delete, left/right arrows, Home/End, Ctrl+U
    (clear), Ctrl+K (kill to end), Ctrl+W (kill word), Ctrl+L (clear screen)
    and up/down arrow history. History is seeded from and persisted through the
    readline module (the same interactive.history file).
    """
    fd = sys.stdin.fileno()
    old_attr = None
    try:
        import termios
        import tty

        old_attr = termios.tcgetattr(fd)
        # TCSANOW (not the default TCSAFLUSH) so input typed ahead of the
        # prompt is preserved instead of being discarded.
        tty.setraw(fd, termios.TCSANOW)
    except Exception:
        raise

    prefix, _, prompt_body = prompt.rpartition("\n")

    history: list[str] = []
    if readline is not None:
        try:
            history = [
                readline.get_history_item(i)
                for i in range(1, readline.get_current_history_length() + 1)
            ]
        except Exception:
            history = []

    buf: list[str] = []
    cursor = 0
    hist_pos = len(history)  # len(history) == editing live input
    hist_draft = ""

    def emit(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def redraw() -> None:
        emit("\r" + prompt_body + "".join(buf))
        emit("\x1b[K")  # clear to end of line
        pos = len(prompt_body) + len("".join(buf[:cursor]))
        emit(f"\r\x1b[{pos + 1}G")  # absolute column (1-based)

    def commit_history(line: str) -> None:
        if readline is not None and line.strip():
            try:
                readline.add_history(line)
            except Exception:
                pass

    try:
        emit(prefix + prompt_body)
        while True:
            b0 = os.read(fd, 1)
            if not b0:
                break  # EOF on the fd itself
            code = b0[0]
            if code == 0x01:  # Ctrl+A — toggle cooperation instantly
                on_ctrl_a()
                redraw()
                continue
            if code == 0x03:  # Ctrl+C
                raise KeyboardInterrupt
            if code == 0x04:  # Ctrl+D
                if not buf:
                    raise EOFError
                if cursor < len(buf):
                    del buf[cursor]
                    redraw()
                continue
            if code in (0x0D, 0x0A):  # Enter
                break
            if code in (0x7F, 0x08):  # Backspace
                if cursor > 0:
                    del buf[cursor - 1]
                    cursor -= 1
                    redraw()
                continue
            if code == 0x15:  # Ctrl+U — clear the whole line
                buf = []
                cursor = 0
                redraw()
                continue
            if code == 0x0B:  # Ctrl+K — kill to end of line
                del buf[cursor:]
                redraw()
                continue
            if code == 0x0C:  # Ctrl+L — clear screen
                emit("\x1b[2J\x1b[H")
                redraw()
                continue
            if code == 0x02:  # Ctrl+B — cursor left
                cursor = max(0, cursor - 1)
                redraw()
                continue
            if code == 0x06:  # Ctrl+F — cursor right
                cursor = min(len(buf), cursor + 1)
                redraw()
                continue
            if code == 0x05:  # Ctrl+E — end of line
                cursor = len(buf)
                redraw()
                continue
            if code == 0x17:  # Ctrl+W — kill word before cursor
                start = cursor
                while start > 0 and buf[start - 1] == " ":
                    start -= 1
                while start > 0 and buf[start - 1] != " ":
                    start -= 1
                del buf[start:cursor]
                cursor = start
                redraw()
                continue
            if code == 0x1B:  # Escape sequences: arrows / Home / End / Delete
                r, _, _ = select.select([fd], [], [], 0.05)
                if not r:
                    continue  # bare Esc
                nxt = os.read(fd, 1)
                if nxt == b"[":
                    r, _, _ = select.select([fd], [], [], 0.05)
                    if not r:
                        continue
                    third = os.read(fd, 1)
                    if third == b"A":  # Up — previous history entry
                        if hist_pos > 0:
                            if hist_pos == len(history):
                                hist_draft = "".join(buf)
                            hist_pos -= 1
                            buf = list(history[hist_pos])
                            cursor = len(buf)
                            redraw()
                    elif third == b"B":  # Down — next history entry
                        if hist_pos < len(history):
                            hist_pos += 1
                            if hist_pos == len(history):
                                buf = list(hist_draft)
                            else:
                                buf = list(history[hist_pos])
                            cursor = len(buf)
                            redraw()
                    elif third == b"C":  # Right
                        cursor = min(len(buf), cursor + 1)
                        redraw()
                    elif third == b"D":  # Left
                        cursor = max(0, cursor - 1)
                        redraw()
                    elif third == b"H":  # Home
                        cursor = 0
                        redraw()
                    elif third == b"F":  # End
                        cursor = len(buf)
                        redraw()
                    elif third == b"3":  # Delete key (ESC [ 3 ~)
                        r, _, _ = select.select([fd], [], [], 0.05)
                        if r:
                            os.read(fd, 1)  # consume "~"
                        if cursor < len(buf):
                            del buf[cursor]
                            redraw()
                continue
            if code >= 0x20:  # Printable (possibly part of a UTF-8 char)
                if code >= 0x80:
                    n = 2 if code & 0xE0 == 0xC0 else 3 if code & 0xF0 == 0xE0 else 4
                    raw = b0
                    for _ in range(n - 1):
                        r, _, _ = select.select([fd], [], [], 0.05)
                        if not r:
                            break
                        raw += os.read(fd, 1)
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                else:
                    text = chr(code)
                buf.insert(cursor, text)
                cursor += 1
                redraw()
    finally:
        try:
            import termios

            if old_attr is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
        except Exception:
            pass
        sys.stdout.write("\n")
        sys.stdout.flush()

    line = "".join(buf)
    commit_history(line)
    return line


class InteractiveMode:
    _THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh"]

    def __init__(self, runtime_host: Any, options: dict[str, Any] | None = None) -> None:
        self.runtime_host = runtime_host
        self.options = options or {}
        self._history_initialized = False

    # Max history entries kept in memory and written back to disk. Without a
    # cap, a large interactive.history file is re-read and re-written by every
    # session (and each test), growing unboundedly and exhausting memory.
    _HISTORY_MAX_LINES = 2000
    _HISTORY_MAX_FILE_BYTES = 5 * 1024 * 1024

    def _setup_readline(self) -> None:
        if self._history_initialized:
            return
        self._history_initialized = True
        if readline is None:
            return
        try:
            history_path = Path(get_agent_dir()) / "interactive.history"
            history_path.parent.mkdir(parents=True, exist_ok=True)
            readline.set_history_length(self._HISTORY_MAX_LINES)
            if (
                history_path.exists()
                and history_path.stat().st_size <= self._HISTORY_MAX_FILE_BYTES
            ):
                readline.read_history_file(str(history_path))
            readline.parse_and_bind("set editing-mode emacs")
            readline.parse_and_bind("tab: complete")
            readline.parse_and_bind('"\\C-l": clear-screen')
            # Let Ctrl+A reach Python so the main loop can toggle cooperation
            # mode. Without this, readline consumes it as beginning-of-line.
            readline.parse_and_bind('"\\C-a": self-insert')
            def _save_history() -> None:
                try:
                    # set_history_length caps this to the last _HISTORY_MAX_LINES.
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
        if bool(self.options.get("cooperation")) or getattr(session.settings_manager, "get_tool_approval", lambda: False)():
            session.approval_callback = self._prompt_approval
            print("[Cooperation] tool approval enabled (mutating tools ask before running)", flush=True)
        assistant_streamed = False
        retry_state = "idle"
        printed_banner = False
        cooperation_state = "on" if session.approval_callback is not None else "off"

        def _toggle_cooperation() -> None:
            nonlocal cooperation_state
            if session.approval_callback is None:
                session.approval_callback = self._prompt_approval
                cooperation_state = "on"
                print("[Cooperation] enabled: mutating tools (bash/write/edit) ask first", flush=True)
            else:
                session.approval_callback = None
                cooperation_state = "off"
                print("[Cooperation] disabled: all tools run freely", flush=True)

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
                    if not assistant_streamed:
                        # Blank line separates the answer from the tool log.
                        print("", flush=True)
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
                    print("", flush=True)
                    print(text, end="", flush=True)
                if text:
                    print("", flush=True)
                elif not assistant_streamed:
                    print("", flush=True)
                    print("[Brak treści odpowiedzi modelu]", flush=True)
            if et == "turn_end" and event.get("ok") is False:
                err = (event.get("error") or "Unknown error").strip()
                print(f"[Błąd] {err}", flush=True)
            if et == "tool_call_start":
                print(f"[Tool] {event.get('tool')} {json.dumps(event.get('args', {}), ensure_ascii=False)}", flush=True)
            if et == "tool_approval_rejected":
                print(f"[Rejected] {event.get('tool')}: {event.get('reason', '')}", flush=True)
            if et == "tool_call_end":
                ok = bool(event.get("ok"))
                status = "OK" if ok else "ERR"
                print(f"[Tool:{status}] {event.get('tool')}", flush=True)
                if event.get("tool") != "finish" and getattr(session.settings_manager, "get_bash_show_output", lambda: True)():
                    payload = event.get("result") or {}
                    if ok:
                        out = str(payload.get("outputText") or payload.get("result") or "").rstrip()
                    else:
                        out = str(payload.get("error") or "").rstrip()
                    if out:
                        print(out, flush=True)
            if et == "ask_user":
                print(f"\n[Agent pyta] {event.get('question')}", flush=True)
                try:
                    answer = input("Odpowiedź: ").strip()
                except (EOFError, KeyboardInterrupt):
                    answer = "(no answer)"
                try:
                    session.answer_question(str(event.get("id") or ""), answer)
                except ValueError:
                    pass
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
            if et == "extension_ui_request":
                print(
                    f"[ExtUI] request {event.get('id')} {event.get('extension')} {event.get('uiType')}",
                    flush=True,
                )
            if et == "extension_ui_response":
                print(
                    f"[ExtUI] response {event.get('requestId')} cancelled={bool(event.get('cancelled'))}",
                    flush=True,
                )

        session.subscribe(on_event)
        while True:
            if printed_banner:
                # Blank line between turns keeps the output readable.
                print("", flush=True)
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
                f"[{model_label} | thinking:{session.thinking_level}{usage_text} | tokens:{tokens_total} | retry:{retry_state} | coop:{cooperation_state} | cwd:{cwd_label} | queue:s{len(queues['steering'])}/f{len(queues['followUp'])}]"
            )
            print("[/help | /status | /queue [clear] | /model [provider/model] | /thinking [level] | /theme [name] | /abort | /exit]")
            print("Ctrl+A toggles cooperation mode (ask before running mutating tools: bash/write/edit).")
            try:
                line = _read_input_line("\n> ", _toggle_cooperation)
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
            if line == "\x01":
                # Fallback path (non-tty stdin): Ctrl+A arrives as a raw byte.
                _toggle_cooperation()
                continue
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
                "/ns": "/new",
            }
            line = aliases.get(line.strip(), line)
            if line.strip() in {"/exit", "/quit"}:
                break
            if line.strip() == "/help":
                print(
                    "/exit /quit | /help | /stats | /state /status | /queue | /tools | /clear | /abort | /new\n"
                    "/model [provider/model] | /model-cycle | /thinking [level] | /thinking-cycle | /theme [name]\n"
                    "/steer <text> | /follow <text> | /compact [instructions] | /tree | /navigate <id> [--summary <text>] | /fork <id> | /login [status|provider [apiKey] [model]] | /logout <provider>\n"
                    "/retry <on|off> | /config [key] [value] | /extui <list|request|respond|cancel|clear>\n"
                    "/cooperation [on|off] | /subagents [on|off] | /bash-show [on|off] | /mcp [list|enable|disable] | /bash <command>\n"
                    "Ctrl+A toggles cooperation mode (bash/write/edit ask first)"
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
            if line.strip() == "/theme":
                current_theme = session.settings_manager.get_theme()
                print(
                    json.dumps(
                        {
                            "current": current_theme,
                            "usage": "/theme <name>",
                            "builtins": ["default", "light", "hacker", "solarized"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                continue
            if line.startswith("/theme "):
                theme_name = line[len("/theme ") :].strip()
                if not theme_name:
                    print("Usage: /theme <name>")
                    continue
                session.settings_manager.set_theme(theme_name)
                print(f"Theme set to {theme_name}")
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
                    # Provider-only: auto-pick the provider's first registered model.
                    provider = val
                    models = session.model_registry.models_for_provider(provider)
                    if not models:
                        print(f"Provider not found or has no models: {provider}")
                        continue
                    model = models[0]
                    provider, model_id = model.provider, model.id
                else:
                    provider, model_id = val.split("/", 1)
                    model = session.model_registry.resolve(provider, model_id, allow_dynamic=True)
                if not model:
                    print(f"Model not found: {provider}/{model_id}")
                    continue
                await session.set_model(model)
                # Persist model choice as new default for next sessions.
                session.settings_manager.set_default_provider(model.provider)
                session.settings_manager.set_default_model(model.id)
                if session.model_registry.find(provider, model_id) is None:
                    print(f"Model set to {provider}/{model_id} (dynamic, saved as default)")
                else:
                    print(f"Model set to {provider}/{model_id} (saved as default)")
                continue
            if line.strip() == "/model-cycle":
                result = await session.cycle_model()
                if not result:
                    print("No available models to cycle.")
                else:
                    session.settings_manager.set_default_provider(result.model.provider)
                    session.settings_manager.set_default_model(result.model.id)
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
            if line.strip() == "/tree":
                tree = session.session_manager.get_tree()
                leaf_id = session.session_manager.get_leaf_id()
                rendered: list[str] = []

                def render_tree(nodes: list[dict[str, Any]], depth: int = 0) -> None:
                    for node in nodes:
                        e = node["entry"]
                        label = e.get("type")
                        if e.get("type") == "message":
                            role = str((e.get("message") or {}).get("role", "?"))
                            label = f"message:{role}"
                        marker = "*" if e.get("id") == leaf_id else " "
                        rendered.append(f"{'  ' * depth}{marker} {label} {e.get('id')}")
                        render_tree(node.get("children", []), depth + 1)

                render_tree(tree)
                print("\n".join(rendered) if rendered else "(empty session)")
                continue
            if line.startswith("/navigate"):
                parts = line.split()
                if len(parts) < 2:
                    print("Usage: /navigate <entryId> [--summary <text>]")
                    continue
                entry_id = parts[1]
                summarize = "--summary" in parts
                custom = None
                if summarize:
                    idx = parts.index("--summary")
                    custom = " ".join(parts[idx + 1 :]) or None
                try:
                    result = await session.navigate_tree(
                        entry_id,
                        {"summarize": summarize, "customInstructions": custom},
                    )
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                except ValueError as e:
                    print(str(e))
                continue
            if line.startswith("/fork "):
                entry_id = line[len("/fork ") :].strip()
                try:
                    result = await self.runtime_host.fork(entry_id)
                    session = self.runtime_host.session
                    session.subscribe(on_event)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                except ValueError as e:
                    print(str(e))
                continue
            if line.strip() == "/new":
                result = await self.runtime_host.new_session({})
                session = self.runtime_host.session
                session.subscribe(on_event)
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
            if line.strip() == "/subagents":
                state = session.settings_manager.get_subagents_enabled()
                print(f"Subagents: {'on' if state else 'off'}")
                print("Usage: /subagents <on|off>")
                continue
            if line.startswith("/subagents "):
                mode = line[len("/subagents ") :].strip().lower()
                if mode not in {"on", "off"}:
                    print("Usage: /subagents <on|off>")
                    continue
                session.settings_manager.set_subagents_enabled(mode == "on")
                print(f"Subagents set to {mode}.")
                continue
            if line.strip() == "/bash-show":
                state = session.settings_manager.get_bash_show_output()
                print(f"Bash output: {'on' if state else 'off'}")
                print("Usage: /bash-show <on|off>")
                continue
            if line.startswith("/bash-show "):
                mode = line[len("/bash-show ") :].strip().lower()
                if mode not in {"on", "off"}:
                    print("Usage: /bash-show <on|off>")
                    continue
                session.settings_manager.set_bash_show_output(mode == "on")
                print(f"Bash output set to {mode}.")
                continue
            if line.strip() == "/mcp":
                manager = getattr(session, "_mcp_manager", None)
                if manager is None:
                    print("MCP not available (started with --no-mcp).")
                    continue
                statuses = manager.server_status()
                if not statuses:
                    print("No MCP servers configured. Add mcpServers to settings.json (see README).")
                    continue
                for s in statuses:
                    if s["error"]:
                        state = f"error: {s['error']}"
                    elif not s.get("enabled", True):
                        state = "disabled"
                    elif s["running"]:
                        state = "running"
                    else:
                        state = "configured (not started)"
                    tools_list = ", ".join(s["tools"]) if s["tools"] else "(none)"
                    print(f"- {s['name']}: {state} ({s.get('transport', 'stdio')}) [{tools_list}]")
                print("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>")
                continue
            if line.startswith("/mcp "):
                rest = line[len("/mcp ") :].strip()
                parts = rest.split(maxsplit=1)
                sub = parts[0].lower() if parts else ""
                if sub == "list":
                    manager = getattr(session, "_mcp_manager", None)
                    if manager is None:
                        print("MCP not available (started with --no-mcp).")
                        continue
                    statuses = manager.server_status()
                    if not statuses:
                        print("No MCP servers configured. Add mcpServers to settings.json (see README).")
                        continue
                    for s in statuses:
                        if s["error"]:
                            state = f"error: {s['error']}"
                        elif not s.get("enabled", True):
                            state = "disabled"
                        elif s["running"]:
                            state = "running"
                        else:
                            state = "configured (not started)"
                        tools_list = ", ".join(s["tools"]) if s["tools"] else "(none)"
                        print(f"- {s['name']}: {state} ({s.get('transport', 'stdio')}) [{tools_list}]")
                    print("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>")
                    continue
                if sub == "enable":
                    if not parts or len(parts) < 2:
                        print("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>")
                        continue
                    server_name = parts[1]
                    cfg = session.settings_manager.get_mcp_servers().get(server_name)
                    if not cfg or (not cfg.get("command") and not cfg.get("url")):
                        print(f"No MCP config for '{server_name}'. Add mcpServers.<name> to settings.json (see README).")
                        continue
                    manager = getattr(session, "_mcp_manager", None)
                    if manager is None:
                        print("MCP not available (started with --no-mcp).")
                        continue
                    try:
                        added = await manager.enable_server(
                            server_name,
                            str(cfg.get("command", "")),
                            cfg.get("args") or [],
                            cfg.get("env") or {},
                            url=cfg.get("url"),
                        )
                        session.settings_manager.set_mcp_server_enabled(server_name, True)
                        session.sync_mcp_tools()
                        if added:
                            print(f"Server '{server_name}' enabled. Tools: {', '.join(added)}")
                        else:
                            print(f"Server '{server_name}' already running.")
                    except RuntimeError as e:
                        print(str(e))
                elif sub == "disable":
                    if len(parts) < 2:
                        print("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>")
                        continue
                    server_name = parts[1]
                    manager = getattr(session, "_mcp_manager", None)
                    if manager is None:
                        print("MCP not available (started with --no-mcp).")
                        continue
                    try:
                        removed = await manager.disable_server(server_name)
                        if removed:
                            print(f"Server '{server_name}' disabled. Removed tools: {', '.join(removed)}")
                            session.sync_mcp_tools()
                            session.settings_manager.set_mcp_server_enabled(server_name, False)
                        else:
                            print(f"No running server named '{server_name}'.")
                    except RuntimeError as e:
                        print(str(e))
                else:
                    print("Usage: /mcp list | /mcp enable <name> | /mcp disable <name>")
                continue
            if line.startswith("/bash "):
                command = line[len("/bash ") :].strip()
                if not command:
                    print("Usage: /bash <command>")
                    continue
                try:
                    result = await session.execute_bash(command)
                    output = (result.get("output") or "").rstrip()
                    if getattr(session.settings_manager, "get_bash_show_output", lambda: True)() and output:
                        print(output)
                    else:
                        print(f"[bash] exitCode={result.get('exitCode')}")
                except Exception as e:
                    print(str(e))
                continue
            if line.strip() in {"/extui", "/ext-ui"}:
                print("Usage: /extui <list|request|respond|cancel|clear> ...")
                continue
            if line.startswith("/extui ") or line.startswith("/ext-ui "):
                rest = line.split(" ", 1)[1].strip()
                parts = rest.split(maxsplit=3)
                if not parts:
                    print("Usage: /extui <list|request|respond|cancel|clear> ...")
                    continue
                sub = parts[0].lower()
                if sub == "list":
                    print(json.dumps(session.get_extension_ui_state(), ensure_ascii=False, indent=2))
                    continue
                if sub == "clear":
                    session.clear_extension_ui_history()
                    print("Extension UI history cleared.")
                    continue
                if sub == "request":
                    # /extui request <extension> <widget|overlay> [jsonPayload]
                    if len(parts) < 3:
                        print("Usage: /extui request <extension> <widget|overlay> [jsonPayload]")
                        continue
                    extension = parts[1]
                    ui_type = parts[2]
                    payload: dict[str, Any] = {}
                    if len(parts) >= 4 and parts[3].strip():
                        try:
                            parsed = json.loads(parts[3])
                            payload = parsed if isinstance(parsed, dict) else {"value": parsed}
                        except Exception:
                            print("Invalid JSON payload for /extui request")
                            continue
                    try:
                        req = session.request_extension_ui(extension=extension, ui_type=ui_type, payload=payload)
                    except ValueError as e:
                        print(str(e))
                        continue
                    print(json.dumps(req, ensure_ascii=False, indent=2))
                    continue
                if sub in {"respond", "cancel"}:
                    # /extui respond <requestId> [jsonPayload]
                    if len(parts) < 2:
                        print("Usage: /extui respond <requestId> [jsonPayload]")
                        continue
                    request_id = parts[1]
                    payload: dict[str, Any] = {}
                    if sub == "respond" and len(parts) >= 3 and parts[2].strip():
                        raw_payload = rest.split(maxsplit=2)[2]
                        try:
                            parsed = json.loads(raw_payload)
                            payload = parsed if isinstance(parsed, dict) else {"value": parsed}
                        except Exception:
                            print("Invalid JSON payload for /extui respond")
                            continue
                    try:
                        resp = session.respond_extension_ui(
                            request_id=request_id,
                            payload=payload,
                            cancelled=sub == "cancel",
                        )
                    except ValueError as e:
                        print(str(e))
                        continue
                    print(json.dumps(resp, ensure_ascii=False, indent=2))
                    continue
                print("Usage: /extui <list|request|respond|cancel|clear> ...")
                continue
            if line.strip() == "/cooperation":
                enabled = session.approval_callback is not None
                print(
                    json.dumps(
                        {"enabled": enabled, "tools": sorted(session._approval_tools)},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                continue
            if line.startswith("/cooperation "):
                mode = line[len("/cooperation ") :].strip().lower()
                if mode in {"on", "enable", "yes", "1", "true"}:
                    session.approval_callback = self._prompt_approval
                    cooperation_state = "on"
                    print("[Cooperation] enabled: mutating tools (bash/write/edit) ask first", flush=True)
                elif mode in {"off", "disable", "no", "0", "false"}:
                    session.approval_callback = None
                    cooperation_state = "off"
                    print("[Cooperation] disabled: all tools run freely", flush=True)
                else:
                    print("Usage: /cooperation [on|off]")
                continue
            if line.startswith("/"):
                print(f"Unknown command: {line.strip()}. Use /help.")
                continue
            if session.is_streaming:
                await session.prompt(line, {"streamingBehavior": "followUp"})
                print("Queued follow-up message.")
            else:
                await session.prompt(line)

    async def _prompt_approval(self, tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
        """Cooperation mode: ask the user before running a mutating tool.

        Accept (Enter / 'y') -> (True, ""). Reject ('n') -> a required reason
        is prompted -> (False, reason). Ctrl+C / EOF -> (False, "aborted by user").
        """
        print(f"[Approve] {tool_name} {json.dumps(args, ensure_ascii=False)}", flush=True)
        try:
            while True:
                answer = input("Run? [Y]/n: ").strip().lower()
                if answer in {"", "y", "yes"}:
                    return True, ""
                if answer in {"n", "no"}:
                    reason = ""
                    while not reason.strip():
                        reason = input("Reason (required): ").strip()
                    return False, reason
                print("Please answer 'y' (run) or 'n' (reject).")
        except (KeyboardInterrupt, EOFError):
            print("\n[Approval aborted by user]", flush=True)
            return False, "aborted by user"

    async def init(self) -> None:
        return

    def stop(self) -> None:
        return
