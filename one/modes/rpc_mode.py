from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from .rpc_types import RpcResponse


async def run_rpc_mode(runtime_host: Any) -> None:
    session = runtime_host.session

    def output(obj: dict[str, Any]) -> None:
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    def success(req_id: str | None, command: str, data: Any = None) -> RpcResponse:
        out: RpcResponse = {"id": req_id, "type": "response", "command": command, "success": True}
        if data is not None:
            out["data"] = data
        return out

    def error(req_id: str | None, command: str, message: str) -> RpcResponse:
        return {"id": req_id, "type": "response", "command": command, "success": False, "error": message}

    async def rebind() -> None:
        nonlocal session
        session = runtime_host.session

        def on_event(event: dict[str, Any]) -> None:
            output(event)

        session.subscribe(on_event)

    await rebind()

    while True:
        line = await asyncio.to_thread(input)
        if not line:
            continue

        try:
            cmd = json.loads(line)
        except Exception as e:
            output(error(None, "parse", f"Failed to parse command: {e}"))
            continue

        ctype = cmd.get("type")
        cid = cmd.get("id")
        try:
            if ctype == "prompt":
                if session.is_streaming and not cmd.get("streamingBehavior"):
                    output(error(cid, ctype, "streamingBehavior is required while streaming"))
                    continue
                asyncio.create_task(session.prompt(cmd.get("message", ""), {"streamingBehavior": cmd.get("streamingBehavior")}))
                output(success(cid, ctype))
            elif ctype == "steer":
                await session.steer(cmd.get("message", ""))
                output(success(cid, ctype))
            elif ctype == "follow_up":
                await session.follow_up(cmd.get("message", ""))
                output(success(cid, ctype))
            elif ctype == "abort":
                await session.abort()
                output(success(cid, ctype))
            elif ctype == "new_session":
                result = await runtime_host.new_session({"parentSession": cmd.get("parentSession")})
                await rebind()
                output(success(cid, ctype, result))
            elif ctype == "get_state":
                output(
                    success(
                        cid,
                        ctype,
                        {
                            "model": {"provider": session.model.provider, "id": session.model.id} if session.model else None,
                            "thinkingLevel": session.thinking_level,
                            "isStreaming": session.is_streaming,
                            "isCompacting": session.is_compacting,
                            "steeringMode": session.steering_mode,
                            "followUpMode": session.follow_up_mode,
                            "sessionFile": session.session_file,
                            "sessionId": session.session_id,
                            "sessionName": session.session_name,
                            "autoCompactionEnabled": session.auto_compaction_enabled,
                            "messageCount": len(session.messages),
                            "pendingMessageCount": session.pending_message_count,
                            "pendingQueues": session.get_pending_queues(),
                            "activeTools": session.active_tools,
                            "autoRetryEnabled": session.auto_retry_enabled,
                        },
                    )
                )
            elif ctype == "set_model":
                model = session.model_registry.find(cmd.get("provider"), cmd.get("modelId"))
                if not model:
                    output(error(cid, ctype, f"Model not found: {cmd.get('provider')}/{cmd.get('modelId')}"))
                else:
                    await session.set_model(model)
                    output(success(cid, ctype, {"provider": model.provider, "id": model.id}))
            elif ctype == "cycle_model":
                result = await session.cycle_model()
                output(success(cid, ctype, None if not result else {"model": {"provider": result.model.provider, "id": result.model.id}, "thinkingLevel": result.thinkingLevel, "isScoped": result.isScoped}))
            elif ctype == "get_available_models":
                models = [{"provider": m.provider, "id": m.id, "reasoning": m.reasoning, "contextWindow": m.context_window} for m in session.model_registry.get_available()]
                output(success(cid, ctype, {"models": models}))
            elif ctype == "set_thinking_level":
                session.set_thinking_level(cmd.get("level", "medium"))
                output(success(cid, ctype))
            elif ctype == "cycle_thinking_level":
                level = session.cycle_thinking_level()
                output(success(cid, ctype, {"level": level}))
            elif ctype == "set_steering_mode":
                session.set_steering_mode(cmd.get("mode", "interrupt"))
                output(success(cid, ctype))
            elif ctype == "set_follow_up_mode":
                session.set_follow_up_mode(cmd.get("mode", "queue"))
                output(success(cid, ctype))
            elif ctype == "compact":
                result = await session.compact(cmd.get("customInstructions"))
                output(success(cid, ctype, result))
            elif ctype == "set_auto_compaction":
                session.set_auto_compaction_enabled(bool(cmd.get("enabled", True)))
                output(success(cid, ctype))
            elif ctype == "set_auto_retry":
                session.set_auto_retry_enabled(bool(cmd.get("enabled", True)))
                output(success(cid, ctype))
            elif ctype == "abort_retry":
                output(success(cid, ctype))
            elif ctype == "bash":
                result = await session.execute_bash(cmd.get("command", ""))
                output(success(cid, ctype, result))
            elif ctype == "abort_bash":
                session.abort_bash()
                output(success(cid, ctype))
            elif ctype == "get_session_stats":
                output(success(cid, ctype, session.get_session_stats()))
            elif ctype == "export_html":
                path = await session.export_to_html(cmd.get("outputPath"))
                output(success(cid, ctype, {"path": path}))
            elif ctype == "export_jsonl":
                path = session.export_to_jsonl(cmd.get("outputPath"))
                output(success(cid, ctype, {"path": path}))
            elif ctype == "import_session":
                result = await runtime_host.import_from_jsonl(cmd.get("sessionPath"))
                await rebind()
                output(success(cid, ctype, result))
            elif ctype == "switch_session":
                result = await runtime_host.switch_session(cmd.get("sessionPath"))
                await rebind()
                output(success(cid, ctype, result))
            elif ctype == "fork":
                result = await runtime_host.fork(cmd.get("entryId"))
                await rebind()
                output(success(cid, ctype, {"text": result.get("selectedText"), "cancelled": result.get("cancelled", False)}))
            elif ctype == "get_fork_messages":
                output(success(cid, ctype, {"messages": session.get_user_messages_for_forking()}))
            elif ctype == "get_last_assistant_text":
                output(success(cid, ctype, {"text": session.get_last_assistant_text()}))
            elif ctype == "set_session_name":
                session.set_session_name((cmd.get("name") or "").strip())
                output(success(cid, ctype))
            elif ctype == "get_tree":
                output(success(cid, ctype, {"tree": session.session_manager.get_tree()}))
            elif ctype == "get_branch":
                output(success(cid, ctype, {"branch": session.session_manager.get_branch()}))
            elif ctype == "navigate":
                result = await session.navigate_tree(
                    cmd.get("entryId", ""),
                    {
                        "summarize": bool(cmd.get("summarize", False)),
                        "customInstructions": cmd.get("customInstructions"),
                        "label": cmd.get("label"),
                    },
                )
                output(success(cid, ctype, result))
            elif ctype == "get_messages":
                output(success(cid, ctype, {"messages": session.messages}))
            elif ctype == "get_context_usage":
                output(success(cid, ctype, {"contextUsage": session.get_context_usage()}))
            elif ctype == "wait_for_idle":
                await session.wait_for_idle()
                output(success(cid, ctype))
            elif ctype == "reload_resources":
                await session.reload()
                output(success(cid, ctype))
            elif ctype == "get_queue":
                output(success(cid, ctype, session.get_pending_queues()))
            elif ctype == "get_tools":
                output(success(cid, ctype, {"tools": session.active_tools}))
            elif ctype == "get_commands":
                skills = session.resource_loader.get_skills().get("skills", [])
                prompts = session.resource_loader.get_prompts().get("prompts", [])
                commands = []
                for p in prompts:
                    commands.append({"name": p.get("name"), "description": p.get("description", ""), "source": "prompt", "sourceInfo": p.get("source")})
                for s in skills:
                    commands.append({"name": f"skill:{s.get('name')}", "description": "", "source": "skill", "sourceInfo": s.get("filePath")})
                output(success(cid, ctype, {"commands": commands}))
            elif ctype == "get_extension_ui":
                output(success(cid, ctype, session.get_extension_ui_state()))
            elif ctype == "request_extension_ui":
                req = session.request_extension_ui(
                    extension=cmd.get("extension", "unknown"),
                    ui_type=cmd.get("uiType", "widget"),
                    payload=cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {},
                    title=cmd.get("title"),
                )
                output(success(cid, ctype, req))
            elif ctype == "respond_extension_ui":
                payload = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}
                resp = session.respond_extension_ui(
                    request_id=cmd.get("requestId", ""),
                    payload=payload,
                    cancelled=bool(cmd.get("cancelled", False)),
                )
                output(success(cid, ctype, resp))
            elif ctype == "clear_extension_ui":
                session.clear_extension_ui_history()
                output(success(cid, ctype))
            elif ctype == "get_theme":
                output(success(cid, ctype, {"theme": session.settings_manager.get_theme()}))
            elif ctype == "set_theme":
                session.settings_manager.set_theme(cmd.get("theme", ""))
                output(success(cid, ctype, {"theme": session.settings_manager.get_theme()}))
            elif ctype == "get_settings":
                output(success(cid, ctype, session.settings_manager.get_global_settings()))
            elif ctype == "set_config_value":
                try:
                    session.settings_manager.set_config_value(cmd.get("key", ""), cmd.get("value"))
                except ValueError as e:
                    output(error(cid, ctype, str(e)))
                else:
                    output(success(cid, ctype))
            elif ctype == "get_retry_settings":
                output(success(cid, ctype, session.settings_manager.get_retry_settings()))
            elif ctype == "get_tool_approval":
                output(
                    success(
                        cid,
                        ctype,
                        {
                            "enabled": session.settings_manager.get_tool_approval(),
                            "tools": session.settings_manager.get_tool_approval_tools(),
                        },
                    )
                )
            elif ctype == "login":
                provider = (cmd.get("provider") or "").strip()
                api_key = cmd.get("apiKey") or ""
                if not provider:
                    output(error(cid, ctype, "provider is required"))
                elif not api_key:
                    output(error(cid, ctype, "apiKey is required"))
                else:
                    session.model_registry.set_stored_api_key(provider, api_key)
                    output(
                        success(
                            cid,
                            ctype,
                            {"provider": provider, "status": session.model_registry.get_provider_auth_status(provider)},
                        )
                    )
            elif ctype == "logout":
                provider = (cmd.get("provider") or "").strip()
                if not provider:
                    output(error(cid, ctype, "provider is required"))
                else:
                    session.model_registry.remove_stored_api_key(provider)
                    output(
                        success(
                            cid,
                            ctype,
                            {"provider": provider, "status": session.model_registry.get_provider_auth_status(provider)},
                        )
                    )
            elif ctype == "get_extensions":
                output(success(cid, ctype, session.resource_loader.get_extensions()))
            elif ctype == "get_skills":
                output(success(cid, ctype, session.resource_loader.get_skills()))
            elif ctype == "get_prompts":
                output(success(cid, ctype, session.resource_loader.get_prompts()))
            elif ctype == "get_themes":
                output(success(cid, ctype, session.resource_loader.get_themes()))
            elif ctype == "get_agents_files":
                output(success(cid, ctype, session.resource_loader.get_agents_files()))
            else:
                output(error(cid, ctype or "unknown", f"Unknown command: {ctype}"))
        except EOFError:
            break
        except Exception as e:
            output(error(cid, ctype or "unknown", str(e)))
