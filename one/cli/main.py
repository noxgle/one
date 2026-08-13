from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from one.config import APP_NAME, ENV_AGENT_DIR, VERSION, get_agent_dir, get_models_path
from one.core.agent_session_runtime import AgentSessionRuntimeHost, create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager, get_default_session_dir
from one.core.settings_manager import SettingsManager
from one.modes import InteractiveMode, TuiMode, run_print_mode, run_rpc_mode, run_run_mode
from one.mcp import McpManager
from one.resources.resource_loader import DefaultResourceLoader
from one.tools.index import all_tools

from .args import parse_args, print_help


def _parse_params(raw: list[str]) -> tuple[dict[str, str], list[str]]:
    """Parse --param name=value pairs into a dict; returns (params, errors)."""
    params: dict[str, str] = {}
    errors: list[str] = []
    for p in raw:
        if "=" not in p:
            errors.append(f"Invalid --param (expected name=value): {p}")
            continue
        name, value = p.split("=", 1)
        name = name.strip()
        if not name:
            errors.append(f"Invalid --param (empty name): {p}")
            continue
        params[name] = value
    return params, errors


def _expand_file_tokens(tokens: list[str], params: dict[str, str]) -> tuple[list[str], list[str]]:
    """Expand @file tokens: replace each with the file content, applying
    {{name}} parameter substitution. Returns (expanded, errors)."""
    out: list[str] = []
    errors: list[str] = []
    for tok in tokens:
        if not tok.startswith("@"):
            out.append(tok)
            continue
        path = Path(tok[1:]).expanduser()
        if not path.is_file():
            errors.append(f"File not found: {tok}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            errors.append(f"Cannot read {tok}: {e}")
            continue
        for name, value in params.items():
            content = content.replace("{{" + name + "}}", value)
        out.append(content)
    return out, errors


async def _run(argv: list[str]) -> int:
    parsed = parse_args(argv)

    if parsed.errors:
        for err in parsed.errors:
            print(err)
        return 2

    params, param_errors = _parse_params(parsed.params)
    if param_errors:
        for err in param_errors:
            print(err)
        return 2

    expanded_messages, file_errors = _expand_file_tokens(parsed.messages, params)
    if file_errors:
        for err in file_errors:
            print(err)
        return 2
    parsed.messages = expanded_messages

    if parsed.run_task and parsed.run_task.startswith("@"):
        expanded_task, task_errors = _expand_file_tokens([parsed.run_task], params)
        if task_errors:
            for err in task_errors:
                print(err)
            return 2
        parsed.run_task = expanded_task[0]

    if parsed.version:
        print(VERSION)
        return 0

    if parsed.help:
        print_help()
        return 0

    if parsed.offline:
        os.environ[f"{APP_NAME.upper()}_OFFLINE"] = "1"
    if parsed.llama_cpp_url:
        os.environ["LLAMA_CPP_BASE_URL"] = parsed.llama_cpp_url
    if parsed.ollama_url:
        os.environ["OLLAMA_BASE_URL"] = parsed.ollama_url

    cwd = str(Path.cwd())
    fallback_agent_dir_path = Path(cwd) / ".one" / "agent"
    agent_dir_path = Path(get_agent_dir())
    try:
        agent_dir_path.mkdir(parents=True, exist_ok=True)
        # Validate writability; existing read-only dirs can pass mkdir(exist_ok=True).
        probe = agent_dir_path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        # Fallback for read-only home or sandboxed environments.
        agent_dir_path = fallback_agent_dir_path
        agent_dir_path.mkdir(parents=True, exist_ok=True)
    # Keep config path resolution consistent for this process.
    os.environ[ENV_AGENT_DIR] = str(agent_dir_path)
    agent_dir = str(agent_dir_path)

    settings = SettingsManager.create(cwd, agent_dir)
    if parsed.no_subagents:
        settings.set_subagents_enabled(False, persist=False)
    if parsed.no_bash_output:
        settings.set_bash_show_output(False, persist=False)
    auth = AuthStorage.create()
    registry = ModelRegistry.create(auth, get_models_path())

    if parsed.command:
        cmd = parsed.command
        args = parsed.command_args
        if cmd == "list":
            print(json.dumps({"packages": settings.get_packages()}, ensure_ascii=False, indent=2))
            return 0
        if cmd == "install":
            if len(args) != 1:
                print("Usage: one install <path-to-extension>")
                return 2
            source = Path(args[0].strip()).expanduser()
            if not source.exists():
                print(f"Source not found: {source}")
                return 1
            extensions_dir = Path(agent_dir) / "extensions"
            extensions_dir.mkdir(parents=True, exist_ok=True)
            name = source.name
            target = extensions_dir / name
            if target.exists():
                print(f"Package already installed: {name}")
                return 0
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
            packages = settings.get_packages()
            if name not in packages:
                packages.append(name)
                settings.set_packages(packages)
            print(f"Installed package: {name} -> {target}")
            return 0
        if cmd == "remove":
            if len(args) != 1:
                print("Usage: one remove <package>")
                return 2
            package = args[0].strip()
            if not package:
                print("Package name cannot be empty")
                return 2
            current = settings.get_packages()
            if package not in current:
                print(f"Package not installed: {package}")
                return 1
            packages = [p for p in current if p != package]
            settings.set_packages(packages)
            ext = Path(agent_dir) / "extensions" / package
            if ext.is_dir():
                shutil.rmtree(ext)
                print(f"Removed package: {package}")
            elif ext.exists():
                ext.unlink()
                print(f"Removed package: {package}")
            else:
                print(f"Removed package reference: {package}")
            return 0
        if cmd == "update":
            if len(args) > 1:
                print("Usage: one update [package]")
                return 2
            extensions_dir = Path(agent_dir) / "extensions"
            on_disk = {p.name for p in extensions_dir.iterdir()} if extensions_dir.is_dir() else set()
            installed = set(settings.get_packages())
            if args:
                target = args[0].strip()
                if target not in installed:
                    print(f"Package not installed: {target}")
                    return 1
                if target not in on_disk:
                    print(f"Package files missing on disk: {target}")
                    return 1
                print(f"Package up to date: {target}")
                return 0
            # No argument: sync the manifest with what is actually on disk.
            pruned = sorted(installed - on_disk)
            added = sorted(on_disk - installed)
            settings.set_packages(sorted(on_disk))
            if added and pruned:
                print(f"Added package reference(s): {', '.join(added)}")
                print(f"Pruned missing package reference(s): {', '.join(pruned)}")
            elif added:
                print(f"Added package reference(s): {', '.join(added)}")
            elif pruned:
                print(f"Pruned missing package reference(s): {', '.join(pruned)}")
            else:
                print(f"Packages up to date: {len(on_disk)} installed")
            return 0
        if cmd == "run":
            # run_task already correctly set by parse_args (flags extracted).
            # fall through to the normal runtime setup below.
            pass

        if cmd == "config":
            if not args:
                print(json.dumps(settings.get_global_settings(), ensure_ascii=False, indent=2))
                return 0
            if len(args) == 1:
                cur: object = settings.merged()
                for part in args[0].split("."):
                    if isinstance(cur, dict):
                        cur = cur.get(part)  # type: ignore[assignment]
                    else:
                        cur = None
                if cur is None:
                    print(f"Config key not found: {args[0]}")
                    return 1
                print(json.dumps({"key": args[0], "value": cur}, ensure_ascii=False, indent=2))
                return 0
            raw = " ".join(args[1:])
            val: object = raw
            try:
                val = json.loads(raw)
            except Exception:
                pass
            try:
                settings.set_config_value(args[0], val)
            except ValueError as e:
                print(str(e))
                return 2
            print(f"Updated config: {args[0]}")
            return 0

    if parsed.api_key and parsed.model:
        provider = parsed.provider
        model_id = parsed.model
        if "/" in parsed.model and not provider:
            provider, model_id = parsed.model.split("/", 1)
        provider = provider or settings.get_default_provider() or "openai"
        auth.set_runtime_api_key(provider, parsed.api_key)

    if parsed.list_models is not None:
        needle = parsed.list_models if isinstance(parsed.list_models, str) else None
        models = registry.all()
        if needle:
            n = needle.lower()
            models = [m for m in models if n in f"{m.provider}/{m.id}".lower()]
        for m in models:
            print(f"{m.provider}/{m.id}")
        return 0

    if parsed.export:
        manager = SessionManager.open(parsed.export)
        from one.core.agent_session import AgentSession

        loader = DefaultResourceLoader(cwd=manager.cwd, agent_dir=agent_dir, settings_manager=settings)
        await loader.reload()
        model = registry.all()[0] if registry.all() else None
        session = AgentSession(manager, settings, registry, loader, model, settings.get_default_thinking_level())
        if parsed.export_format == "jsonl":
            path = session.export_to_jsonl(parsed.messages[0] if parsed.messages else None)
        else:
            path = await session.export_to_html(parsed.messages[0] if parsed.messages else None)
        print(f"Exported to: {path}")
        return 0

    loader = DefaultResourceLoader(
        cwd=cwd,
        agent_dir=agent_dir,
        settings_manager=settings,
        additional_extension_paths=parsed.extensions,
        additional_skill_paths=parsed.skills,
        additional_prompt_template_paths=parsed.prompt_templates,
        additional_theme_paths=parsed.themes,
        no_extensions=parsed.no_extensions,
        no_skills=parsed.no_skills,
        no_prompt_templates=parsed.no_prompt_templates,
        no_themes=parsed.no_themes,
        system_prompt=parsed.system_prompt,
        append_system_prompt=parsed.append_system_prompt,
    )
    await loader.reload()

    mcp_manager = None
    if not parsed.no_mcp:
        mcp_manager = McpManager.create(settings)
        await mcp_manager.start()
        for err in mcp_manager.errors():
            print(f"[mcp] {err}")

    configured_session_dir = parsed.session_dir or settings.get_session_dir()
    if configured_session_dir:
        try:
            sd_path = Path(configured_session_dir)
            sd_path.mkdir(parents=True, exist_ok=True)
            probe = sd_path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            session_dir = str(sd_path)
        except OSError:
            session_dir = get_default_session_dir(cwd, agent_dir)
    else:
        session_dir = get_default_session_dir(cwd, agent_dir)
    if parsed.no_session:
        session_manager = SessionManager.in_memory(cwd)
    elif parsed.session:
        session_manager = SessionManager.open(parsed.session, session_dir)
    elif parsed.continue_session or parsed.resume:
        try:
            session_manager = SessionManager.continue_recent(cwd, session_dir)
        except Exception:
            print("No session to continue/resume.")
            return 1
    elif parsed.fork:
        source = parsed.fork
        if "/" not in source and "\\" not in source and not source.endswith(".jsonl"):
            local = [s for s in SessionManager.list(cwd, session_dir) if s.id.startswith(source)]
            if local:
                source = local[0].path
            else:
                global_match = [s for s in SessionManager.list_all() if s.id.startswith(source)]
                if not global_match:
                    raise SystemExit(f"No session found matching '{parsed.fork}'")
                source = global_match[0].path
        session_manager = SessionManager.fork_from(source, cwd, session_dir)
    else:
        session_manager = SessionManager.create(cwd, session_dir)

    model = None
    if parsed.model:
        if "/" in parsed.model and not parsed.provider:
            p, m = parsed.model.split("/", 1)
            model = registry.resolve(p, m, allow_dynamic=True)
        else:
            provider = parsed.provider or settings.get_default_provider() or "openai"
            model = registry.resolve(provider, parsed.model, allow_dynamic=True)
    if not model:
        model = registry.select_default(
            settings.get_default_provider(),
            settings.get_default_model(),
        )

    tool_names = ["read", "bash", "edit", "write", "grep", "find", "ls", "finish", "spawn_subagent", "ask_user"]
    if parsed.no_tools:
        tool_names = parsed.tools or []
    elif parsed.tools:
        tool_names = parsed.tools

    if not settings.get_subagents_enabled():
        tool_names = [t for t in tool_names if t != "spawn_subagent"]

    bad_tools = [t for t in tool_names if t not in all_tools]
    if bad_tools:
        print(f"Unknown tools: {', '.join(bad_tools)}")
        return 2

    scoped_models = []
    if parsed.models:
        for item in parsed.models:
            model_part, thinking_part = (item.split(":", 1) + [None])[:2] if ":" in item else (item, None)
            if "/" in model_part:
                provider, mid = model_part.split("/", 1)
                m = registry.find(provider, mid)
                if m:
                    scoped_models.append({"model": m, "thinkingLevel": thinking_part})
            else:
                candidates = [m for m in registry.all() if model_part.lower() in m.id.lower()]
                if candidates:
                    scoped_models.append({"model": candidates[0], "thinkingLevel": thinking_part})

    bootstrap = {
        "agentDir": agent_dir,
        "authStorage": auth,
        "modelRegistry": registry,
        "settingsManager": settings,
        "resourceLoader": loader,
        "model": model,
        "thinkingLevel": parsed.thinking or settings.get_default_thinking_level(),
        "scopedModels": scoped_models,
        "tools": [all_tools[t] for t in tool_names],
        "mcpManager": mcp_manager,
    }

    runtime = await create_agent_session_runtime(
        bootstrap,
        {
            "cwd": session_manager.cwd,
            "sessionManager": session_manager,
            "resourceLoader": loader,
        },
    )
    host = AgentSessionRuntimeHost(bootstrap, runtime)

    try:
        if parsed.command == "run":
            if parsed.cooperation:
                host.session.approval_callback = _headless_approval_prompt
            return await run_run_mode(
                host,
                {
                    "task": parsed.run_task,
                    "resume": parsed.resume,
                    "json": parsed.json_output,
                    "answer_file": parsed.answer_file,
                    "steer_file": parsed.steer_file,
                    "agentDir": agent_dir,
                },
            )

        if parsed.print_mode or parsed.mode in {"text", "json"}:
            code = await run_print_mode(
                host,
                {
                    "mode": parsed.mode or "text",
                    "messages": parsed.messages,
                    "initialMessage": None,
                },
            )
            return code

        if parsed.mode == "rpc":
            await run_rpc_mode(host)
            return 0

        if parsed.mode == "tui":
            tui = TuiMode(
                host,
                {
                    "verbose": parsed.verbose,
                    "theme": settings.get_theme(),
                    "cooperation": parsed.cooperation,
                },
            )
            await tui.run()
            return 0

        interactive = InteractiveMode(host, {"verbose": parsed.verbose, "cooperation": parsed.cooperation})
        await interactive.run()
        return 0
    finally:
        if mcp_manager is not None:
            await mcp_manager.close()


async def _headless_approval_prompt(tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
    """Cooperation gate for headless runs: ask on stdin, default deny on EOF."""
    import sys

    print(f"\n[cooperation] Approve {tool_name}? args={json.dumps(args, ensure_ascii=False)[:300]}")
    print("[cooperation] y = approve, n = reject (optionally add a reason, e.g. 'n: reason')")
    sys.stdout.flush()
    try:
        line = (await asyncio.to_thread(sys.stdin.readline)).strip()
    except Exception:
        line = ""
    if not line:
        return (False, "No answer (EOF) - rejected")
    if line.lower().startswith("y"):
        return (True, "")
    reason = line[2:].strip() if line.lower().startswith("n") else line
    return (False, reason or "Rejected by user")


def run() -> None:
    import sys

    raise SystemExit(asyncio.run(_run(sys.argv[1:])))


if __name__ == "__main__":
    run()
