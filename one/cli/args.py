from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any

from one.config import APP_NAME

VALID_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh"}
VALID_MODES = {"text", "json", "rpc", "tui"}


@dataclass
class ParsedArgs:
    command: str | None = None
    command_args: list[str] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    llama_cpp_url: str | None = None
    ollama_url: str | None = None
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    thinking: str | None = None
    continue_session: bool = False
    resume: bool = False
    help: bool = False
    version: bool = False
    mode: str | None = None
    no_session: bool = False
    session: str | None = None
    fork: str | None = None
    session_dir: str | None = None
    models: list[str] | None = None
    tools: list[str] | None = None
    no_tools: bool = False
    extensions: list[str] = field(default_factory=list)
    no_extensions: bool = False
    print_mode: bool = False
    export: str | None = None
    export_format: str = "html"
    no_skills: bool = False
    no_mcp: bool = False
    skills: list[str] = field(default_factory=list)
    prompt_templates: list[str] = field(default_factory=list)
    no_prompt_templates: bool = False
    themes: list[str] = field(default_factory=list)
    no_themes: bool = False
    list_models: str | bool | None = None
    offline: bool = False
    verbose: bool = False
    cooperation: bool = False
    no_subagents: bool = False
    no_bash_output: bool = False
    run_task: str | None = None
    json_output: bool = False
    answer_file: str | None = None
    steer_file: str | None = None
    params: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    file_args: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_args(argv: list[str]) -> ParsedArgs:
    command = None
    command_args: list[str] = []
    run_task: str | None = None
    if argv and not argv[0].startswith("-") and argv[0] in {"install", "remove", "update", "list", "config", "run"}:
        command = argv[0]
        command_args = argv[1:]
        if command == "run":
            # Flags may appear before/after the task text; re-inject them so
            # argparse sees them, keep the rest as the task. Value-taking
            # flags (--answer-file) must keep their value next to them.
            _VALUE_FLAGS = {"--answer-file", "--steer-file", "--param"}
            flags: list[str] = []
            rest: list[str] = []
            i = 0
            while i < len(command_args):
                a = command_args[i]
                if a in _VALUE_FLAGS:
                    flags.append(a)
                    if i + 1 < len(command_args):
                        flags.append(command_args[i + 1])
                        i += 2
                        continue
                if a.startswith("-"):
                    flags.append(a)
                else:
                    rest.append(a)
                i += 1
            argv = flags
            run_task = " ".join(rest)
        else:
            argv = []


    parser = argparse.ArgumentParser(prog=APP_NAME, add_help=False)
    parser.add_argument("messages", nargs="*")
    parser.add_argument("--help", "-h", action="store_true")
    parser.add_argument("--version", "-v", action="store_true")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--api-key")
    parser.add_argument("--llama-cpp-url")
    parser.add_argument("--ollama-url")
    parser.add_argument("--system-prompt")
    parser.add_argument("--append-system-prompt")
    parser.add_argument("--thinking")
    parser.add_argument("--continue", "-c", dest="continue_session", action="store_true")
    parser.add_argument("--resume", "-r", action="store_true")
    parser.add_argument("--mode")
    parser.add_argument("--no-session", action="store_true")
    parser.add_argument("--session")
    parser.add_argument("--fork")
    parser.add_argument("--session-dir")
    parser.add_argument("--models")
    parser.add_argument("--tools")
    parser.add_argument("--no-tools", action="store_true")
    parser.add_argument("--extension", "-e", action="append", default=[])
    parser.add_argument("--no-extensions", "-ne", action="store_true")
    parser.add_argument("--print", "-p", action="store_true", dest="print_mode")
    parser.add_argument("--export")
    parser.add_argument("--export-format", choices=["html", "jsonl"], default="html")
    parser.add_argument("--skill", action="append", default=[])
    parser.add_argument("--no-skills", "-ns", action="store_true")
    parser.add_argument("--no-mcp", action="store_true", dest="no_mcp", help="Do not start MCP servers from settings.json.")
    parser.add_argument("--prompt-template", action="append", default=[])
    parser.add_argument("--no-prompt-templates", "-np", action="store_true")
    parser.add_argument("--theme", action="append", default=[])
    parser.add_argument("--no-themes", action="store_true")
    parser.add_argument("--list-models", nargs="?", const=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--cooperation",
        action="store_true",
        help="Cooperation mode: ask the user before running mutating tools (bash/write/edit); "
        "rejections require a reason that is fed back to the model.",
    )
    parser.add_argument("--no-subagents", action="store_true", help="Disable subagents: the spawn_subagent tool is not registered.")
    parser.add_argument("--no-bash-output", action="store_true", help="Do not print bash command output; show only the exit code.")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print the run result as JSON (summary, goalSuccess, finished).")
    parser.add_argument("--answer-file", dest="answer_file", help="Headless answer channel for ask_user: questions are written to this file and answers are read back (polled).")
    parser.add_argument("--steer-file", dest="steer_file", help="Headless steering channel: write a message to this file while the run is active; it is read and cleared.")
    parser.add_argument("--param", "-P", action="append", default=[], help="Template parameter name=value (repeatable); {{name}} in @file content is replaced.")

    ns = parser.parse_args(argv)
    messages: list[str] = ns.messages or []
    file_args: list[str] = []
    plain: list[str] = []
    for m in messages:
        if m.startswith("@"):
            file_args.append(m[1:])
        else:
            plain.append(m)

    errors: list[str] = []
    thinking = ns.thinking
    if thinking and thinking not in VALID_THINKING_LEVELS:
        errors.append(f"Invalid --thinking value: {thinking}. Allowed: {', '.join(sorted(VALID_THINKING_LEVELS))}")

    mode = ns.mode
    if mode and mode not in VALID_MODES:
        errors.append(f"Invalid --mode value: {mode}. Allowed: {', '.join(sorted(VALID_MODES))}")

    if ns.print_mode and mode == "rpc":
        errors.append("--print cannot be combined with --mode rpc")

    if ns.no_session and (ns.session or ns.continue_session or ns.resume or ns.fork):
        errors.append("--no-session cannot be combined with --session/--continue/--resume/--fork")

    if ns.session and ns.continue_session:
        errors.append("--session cannot be combined with --continue")
    if ns.session and ns.resume:
        errors.append("--session cannot be combined with --resume")
    if ns.continue_session and ns.resume:
        errors.append("--continue cannot be combined with --resume")

    if ns.fork and (ns.session or ns.continue_session or ns.resume):
        errors.append("--fork cannot be combined with --session/--continue/--resume")

    if command == "run" and not run_task and not ns.resume:
        errors.append("Usage: one run <task>")

    return ParsedArgs(
        command=command,
        command_args=command_args,
        provider=ns.provider,
        model=ns.model,
        api_key=ns.api_key,
        llama_cpp_url=ns.llama_cpp_url,
        ollama_url=ns.ollama_url,
        system_prompt=ns.system_prompt,
        append_system_prompt=ns.append_system_prompt,
        thinking=thinking,
        continue_session=ns.continue_session,
        resume=ns.resume,
        help=ns.help,
        version=ns.version,
        mode=mode,
        no_session=ns.no_session,
        session=ns.session,
        fork=ns.fork,
        session_dir=ns.session_dir,
        models=[x.strip() for x in ns.models.split(",")] if ns.models else None,
        tools=[x.strip() for x in ns.tools.split(",")] if ns.tools else None,
        no_tools=ns.no_tools,
        extensions=list(ns.extension),
        no_extensions=ns.no_extensions,
        print_mode=ns.print_mode,
        export=ns.export,
        export_format=ns.export_format,
        no_skills=ns.no_skills,
        no_mcp=ns.no_mcp,
        skills=list(ns.skill),
        prompt_templates=list(ns.prompt_template),
        no_prompt_templates=ns.no_prompt_templates,
        themes=list(ns.theme),
        no_themes=ns.no_themes,
        list_models=ns.list_models,
        offline=ns.offline,
        verbose=ns.verbose,
        cooperation=ns.cooperation,
        no_subagents=ns.no_subagents,
        no_bash_output=ns.no_bash_output,
        run_task=run_task,
        json_output=ns.json_output,
        answer_file=ns.answer_file,
        steer_file=ns.steer_file,
        params=list(ns.param),
        messages=plain,
        file_args=file_args,
        errors=errors,
    )


def print_help() -> None:
    print(
        f"""{APP_NAME} - AI coding assistant with read, bash, edit, write tools

Usage:
  {APP_NAME} [options] [@files...] [messages...]

Options:
  --provider <name>
  --model <pattern>
  --api-key <key>
  --llama-cpp-url <url>
  --ollama-url <url>
  --system-prompt <text>
  --append-system-prompt <text>
  --mode <text|json|rpc|tui>
  --print, -p
  --continue, -c
  --resume, -r
  --session <path>
  --fork <path-or-id>
  --session-dir <dir>
  --no-session
  --models <patterns>
  --tools <read,bash,edit,write,grep,find,ls,finish,plan,spawn_subagent,ask_user,apply_patch>
  --no-tools
  --extension, -e <path>
  --no-extensions
  --no-mcp
  --skill <path>
  --no-skills
  --prompt-template <path>
  --no-prompt-templates
  --theme <path>
  --no-themes
  --export <file>
  --export-format <html|jsonl>
  --list-models [search]
  --offline
  --verbose
  --cooperation
  --no-subagents
  --no-bash-output
  --json
   --answer-file <file>
   --steer-file <file>
   --param <name=value>
  --help, -h
  --version, -v

Commands:
  install <package>
  remove <package>
  update [package]
  list
  config [key] [value]
  run <task>
"""
    )
