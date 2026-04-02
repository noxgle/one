from __future__ import annotations

import json
from typing import Any


class InteractiveMode:
    def __init__(self, runtime_host: Any, options: dict[str, Any] | None = None) -> None:
        self.runtime_host = runtime_host
        self.options = options or {}

    async def run(self) -> None:
        session = self.runtime_host.session
        assistant_streamed = False

        def on_event(event: dict) -> None:
            nonlocal assistant_streamed
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

        session.subscribe(on_event)
        print("Interactive mode. Type /exit to quit. Use /help for commands.")
        while True:
            model_label = f"{session.model.provider}/{session.model.id}" if session.model else "no-model"
            usage = session.get_context_usage()
            usage_text = ""
            if usage:
                usage_text = f" | ctx:{usage['percent']:.1f}%"
            print(f"[{model_label} | thinking:{session.thinking_level}{usage_text}]")
            line = input("\n> ")
            if line.strip() in {"/exit", "/quit"}:
                break
            if line.strip() == "/help":
                print(
                    "/exit /quit | /help | /stats | /state | /model <provider/model> | /thinking <level>\n"
                    "/steer <text> | /follow <text> | /compact [instructions] | /login | /config [key] [value]\n"
                    "/bash <command>"
                )
                continue
            if line.strip() == "/stats":
                print(json.dumps(session.get_session_stats(), ensure_ascii=False, indent=2))
                continue
            if line.strip() == "/state":
                print(
                    json.dumps(
                        {
                            "model": {"provider": session.model.provider, "id": session.model.id} if session.model else None,
                            "thinkingLevel": session.thinking_level,
                            "isStreaming": session.is_streaming,
                            "pendingMessageCount": session.pending_message_count,
                            "sessionId": session.session_id,
                            "sessionFile": session.session_file,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                continue
            if line.startswith("/model "):
                val = line[len("/model ") :].strip()
                if "/" not in val:
                    print("Usage: /model <provider>/<model-id>")
                    continue
                provider, model_id = val.split("/", 1)
                model = session.model_registry.find(provider, model_id)
                if not model:
                    print(f"Model not found: {provider}/{model_id}")
                    continue
                await session.set_model(model)
                print(f"Model set to {provider}/{model_id}")
                continue
            if line.startswith("/thinking "):
                level = line[len("/thinking ") :].strip()
                session.set_thinking_level(level)
                print(f"Thinking level set to {level}")
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
            if line.strip() == "/login":
                provider = input("Provider (e.g. openai/openrouter/ollama-cloud): ").strip()
                api_key = input("API key: ").strip()
                if not provider or not api_key:
                    print("Provider and API key are required.")
                    continue
                session.model_registry.set_stored_api_key(provider, api_key)
                session.settings_manager.set_default_provider(provider)
                print(f"Stored key and set default provider to {provider}.")
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
            await session.prompt(line)

    async def init(self) -> None:
        return

    def stop(self) -> None:
        return
