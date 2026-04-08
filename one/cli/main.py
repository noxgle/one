from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from one.config import APP_NAME, ENV_AGENT_DIR, VERSION, get_agent_dir, get_models_path
from one.core.agent_session_runtime import AgentSessionRuntimeHost, create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager, get_default_session_dir
from one.core.settings_manager import SettingsManager
from one.modes import InteractiveMode, run_print_mode, run_rpc_mode
from one.resources.resource_loader import DefaultResourceLoader
from one.tools.index import all_tools

from .args import parse_args, print_help


async def _run(argv: list[str]) -> int:
    parsed = parse_args(argv)

    if parsed.errors:
        for err in parsed.errors:
            print(err)
        return 2

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
                print("Usage: one install <package>")
                return 2
            package = args[0].strip()
            if not package:
                print("Package name cannot be empty")
                return 2
            packages = settings.get_packages()
            if package in packages:
                print(f"Package already installed: {package}")
                return 0
            packages.append(package)
            settings.set_packages(packages)
            print(f"Installed package reference: {package}")
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
            print(f"Removed package reference: {package}")
            return 0
        if cmd == "update":
            if len(args) > 1:
                print("Usage: one update [package]")
                return 2
            installed = settings.get_packages()
            if not args:
                print(f"Updated package reference(s): all ({len(installed)} installed)")
                return 0
            target = args[0].strip()
            if target not in installed:
                print(f"Package not installed: {target}")
                return 1
            print(f"Updated package reference(s): {target}")
            return 0
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
    elif parsed.continue_session:
        session_manager = SessionManager.continue_recent(cwd, session_dir)
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
        avail = registry.get_available()
        if avail:
            default_provider = settings.get_default_provider()
            default_model = settings.get_default_model()
            model = next((m for m in avail if m.provider == default_provider and m.id == default_model), None) or avail[0]
        else:
            allm = registry.all()
            model = allm[0] if allm else None

    tool_names = ["read", "bash", "edit", "write", "grep", "find", "ls"]
    if parsed.no_tools:
        tool_names = parsed.tools or []
    elif parsed.tools:
        tool_names = parsed.tools

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

    interactive = InteractiveMode(host, {"verbose": parsed.verbose})
    await interactive.run()
    return 0


def run() -> None:
    import sys

    raise SystemExit(asyncio.run(_run(sys.argv[1:])))


if __name__ == "__main__":
    run()
