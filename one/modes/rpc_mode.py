from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from one.core.provider_login import _redact_credentials, validate_and_fetch

from .rpc_types import RpcResponse


async def run_rpc_mode(runtime_host: Any, initial_images: list[dict[str, Any]] | None = None) -> None:
    session = runtime_host.session
    # Store initial images on the session for the first prompt.
    if initial_images:
        session._images = initial_images  # noqa: SLF001
        session._tool_images = []  # noqa: SLF001

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
                # Attachments: import image paths into content-addressed blobs.
                # Atomic: if any import fails, roll back previously imported blobs.
                image_refs: list[dict[str, Any]] | None = None
                attachment_paths: list[str] | None = cmd.get("attachments")
                if attachment_paths:
                    storage_dir = session._storage_dir if hasattr(session, "_storage_dir") else ""
                    # Track ONLY blobs we CREATED during this transaction so
                    # rollback never touches pre-existing deduplicated blobs.
                    newly_created: list[str] = []
                    try:
                        from one.core.attachments import (
                            AttachmentInput,
                            AttachmentValidationError,
                            count_attachments,
                            has_blob,
                            import_image,
                            validate_attachment_input,
                            validate_image_count,
                        )
                        # Count check before importing any.
                        count_attachments([AttachmentInput(path=p) for p in attachment_paths])
                        validate_image_count(len(attachment_paths))
                        image_refs = []
                        for p in attachment_paths:
                            # Check existence BEFORE import by computing the
                            # hash from validated bytes (double-read is
                            # intentional — correctness over performance).
                            validated = validate_attachment_input(AttachmentInput(path=str(p)))
                            blob_hash = validated[0]  # raw bytes
                            computed_hash = hashlib.sha256(blob_hash).hexdigest()
                            if not has_blob(storage_dir, computed_hash):
                                # Blob does not exist yet; import will create it.
                                ref = import_image(storage_dir, str(p))
                                newly_created.append(ref.blob_hash)
                            else:
                                # Blob already exists (dedup); import is safe
                                # and must NOT be removed on rollback.
                                ref = import_image(storage_dir, str(p))
                            image_refs.append(ref.__dict__)
                    except AttachmentValidationError as e:
                        # Rollback: remove only the blobs we created.
                        if storage_dir and newly_created:
                            try:
                                from one.core.attachments import remove_blob as _rb
                                for h in newly_created:
                                    _rb(storage_dir, h)
                            except Exception:
                                pass
                        output(error(cid, ctype, f"Invalid attachment: {e}"))
                        continue
                    except Exception as e:  # noqa: BLE001
                        # Rollback: remove only the blobs we created.
                        if storage_dir and newly_created:
                            try:
                                from one.core.attachments import remove_blob as _rb
                                for h in newly_created:
                                    _rb(storage_dir, h)
                            except Exception:
                                pass
                        output(error(cid, ctype, f"Attachment error: {e}"))
                        continue
                asyncio.create_task(session.prompt(cmd.get("message", ""), {
                    "streamingBehavior": cmd.get("streamingBehavior"),
                    # The command id is an optional, additive correlation key.
                    "requestId": cid,
                }, images=image_refs))
                output(success(cid, ctype))
            elif ctype == "steer":
                steer_images = cmd.get("images")
                if steer_images:
                    output(error(cid, ctype, "steer does not support image attachments"))
                else:
                    await session.steer(cmd.get("message", ""))
                    output(success(cid, ctype))
            elif ctype == "follow_up":
                fu_images = cmd.get("images")
                if fu_images:
                    output(error(cid, ctype, "follow_up does not support image attachments"))
                else:
                    await session.follow_up(cmd.get("message", ""))
                    output(success(cid, ctype))
            elif ctype == "abort":
                await session.abort()
                output(success(cid, ctype))
            elif ctype == "new_session":
                result = await runtime_host.new_session({"parentSession": cmd.get("parentSession")})
                await rebind()
                output(success(cid, ctype, result))
            elif ctype == "inspect_subagent_timeout":
                # Read-only diagnostic: structured timeout info (not get_state).
                diag = session.inspect_subagent_timeout()
                if not diag:
                    output(success(cid, ctype, {"available": False, "diagnostic": {}}))
                else:
                    output(success(cid, ctype, {"available": True, "diagnostic": diag}))
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
                            "lastSubagentTimeout": session.inspect_subagent_timeout(),
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
                # Backward-compatible: if "enabled" is provided, use it.
                # Otherwise respect "mode" field (off/on/unlimited).
                if "enabled" in cmd:
                    session.set_auto_retry_enabled(bool(cmd["enabled"]))
                elif "mode" in cmd:
                    mode = str(cmd["mode"])
                    session.settings_manager.set_retry_mode(mode)
                    session.set_auto_retry_enabled(mode in ("on", "unlimited"))
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
                info = await session.reload()
                output(success(cid, ctype, info))
            elif ctype == "get_queue":
                output(success(cid, ctype, session.get_pending_queues()))
            elif ctype == "get_tools":
                output(success(cid, ctype, {
                    "tools": session.active_tools,
                    "capabilities": {
                        "inputImage": bool(session.model and session.model.input_image),
                    },
                }))
            elif ctype == "get_commands":
                skills = session.resource_loader.get_skills().get("skills", [])
                prompts = session.resource_loader.get_prompts().get("prompts", [])
                commands = []
                for p in prompts:
                    commands.append({"name": p.get("name"), "description": p.get("description", ""), "source": "prompt", "sourceInfo": p.get("source")})
                for s in skills:
                    commands.append({"name": f"skill:{s.get('name')}", "description": s.get("description", ""), "source": "skill", "sourceInfo": s.get("filePath")})
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
            elif ctype == "answer_question":
                try:
                    session.answer_question(cmd.get("id", ""), cmd.get("answer", ""))
                except ValueError as e:
                    output(error(cid, ctype, str(e)))
                else:
                    output(success(cid, ctype))
            elif ctype == "get_pending_questions":
                output(success(cid, ctype, {"questions": session.get_pending_questions()}))
            elif ctype == "login":
                provider = (cmd.get("provider") or "").strip()
                api_key = cmd.get("apiKey") or ""
                model_id = cmd.get("modelId") or cmd.get("model") or ""
                if not provider:
                    output(error(cid, ctype, "provider is required"))
                elif session.providers.get(provider) is None:
                    output(error(cid, ctype, f"unknown provider: {provider}"))
                elif session.model_registry.requires_api_key(provider) and not api_key:
                    output(error(cid, ctype, "apiKey is required"))
                else:
                    # Phase 30.5: validate BEFORE storing, then register/persist models.
                    adapter = session.providers[provider]
                    try:
                        ok, error_msg, fetched = await validate_and_fetch(
                            adapter, api_key, provider, model_id or None,
                        )
                    except Exception as exc:  # noqa: BLE001 - sanitize the raw exception
                        output(error(cid, ctype, _redact_credentials(str(exc), api_key)))
                        continue
                    if not ok:
                        # error_msg already classified by validate_and_fetch
                        # (e.g. "Authorization failed: …", "Validation failed: …");
                        # do NOT prepend another prefix — preserve the stable message.
                        output(error(cid, ctype, error_msg or "Authorization failed"))
                        continue
                    # Register + persist models on success (sanitize if persist raises).
                    if fetched:
                        try:
                            session.model_registry.register_models(provider, fetched)
                            session.model_registry.persist_models(provider, fetched)
                        except Exception as exc:  # noqa: BLE001
                            output(error(cid, ctype, _redact_credentials(str(exc), api_key)))
                            continue
                    # Store the key only after validation succeeds.
                    if api_key:
                        try:
                            session.model_registry.set_stored_api_key(provider, api_key)
                        except Exception as exc:  # noqa: BLE001
                            output(error(cid, ctype, _redact_credentials(str(exc), api_key)))
                            continue
                    # Success response: never echo keys/tokens.
                    resp_data: dict[str, Any] = {
                        "provider": provider,
                        "status": session.model_registry.get_provider_auth_status(provider),
                    }
                    if error_msg:
                        resp_data["warning"] = error_msg
                    output(success(cid, ctype, resp_data))
            elif ctype == "logout":
                provider = (cmd.get("provider") or "").strip()
                if not provider:
                    output(error(cid, ctype, "provider is required"))
                else:
                    session.model_registry.remove_provider_credentials(provider)
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
            elif ctype == "invoke_skill":
                name = cmd.get("name", "")
                args_text = cmd.get("arguments", "") if isinstance(cmd.get("arguments"), str) else ""
                if not name:
                    output(error(cid, ctype, "name is required"))
                else:
                    result = await session.invoke_skill(name, args_text)
                    if result.get("ok"):
                        resp: dict[str, Any] = {
                            "name": result["name"],
                            "bodyLength": result["bodyLength"],
                            "baseDir": result["baseDir"],
                            "trustWarning": (
                                "Skill instructions are loaded as user input. "
                                "Review the skill before granting cooperation approval for file operations."
                            ),
                        }
                        # Forward skill metadata when available.
                        if "allowedTools" in result:
                            resp["allowedTools"] = result["allowedTools"]
                        if "disableModelInvocation" in result:
                            resp["disableModelInvocation"] = result["disableModelInvocation"]
                        output(success(cid, ctype, resp))
                    else:
                        err_msg = result.get("error", "unknown error")
                        err_type = result.get("errorType", "SkillError")
                        output(error(cid, ctype, f"{err_type}: {err_msg}"))
            else:
                output(error(cid, ctype or "unknown", f"Unknown command: {ctype}"))
        except EOFError:
            break
        except Exception as e:
            output(error(cid, ctype or "unknown", str(e)))
