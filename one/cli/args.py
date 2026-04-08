from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any

from one.config import APP_NAME

VALID_THINKING_LEVELS = {"off", "minimal", "low", "medium", "high", "xhigh"}
VALID_MODES = {"text", "json", "rpc"}


@dataclass
class ParsedArgs:
    command: str | None = None
    command_args: list[str] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    llama_cpp_url: str | None = None
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
    no_skills: bool = False
    skills: list[str] = field(default_factory=list)
    prompt_templates: list[str] = field(default_factory=list)
    no_prompt_templates: bool = False
    themes: list[str] = field(default_factory=list)
    no_themes: bool = False
    list_models: str | bool | None = None
    offline: bool = False
    verbose: bool = False
    messages: list[str] = field(default_factory=list)
    file_args: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_args(argv: list[str]) -> ParsedArgs:
    command = None
    command_args: list[str] = []
    if argv and not argv[0].startswith("-") and argv[0] in {"install", "remove", "update", "list", "config"}:
        command = argv[0]
        command_args = argv[1:]
        argv = []

    parser = argparse.ArgumentParser(prog=APP_NAME, add_help=False)
    parser.add_argument("messages", nargs="*")
    parser.add_argument("--help", "-h", action="store_true")
    parser.add_argument("--version", "-v", action="store_true")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--api-key")
    parser.add_argument("--llama-cpp-url")
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
    parser.add_argument("--skill", action="append", default=[])
    parser.add_argument("--no-skills", "-ns", action="store_true")
    parser.add_argument("--prompt-template", action="append", default=[])
    parser.add_argument("--no-prompt-templates", "-np", action="store_true")
    parser.add_argument("--theme", action="append", default=[])
    parser.add_argument("--no-themes", action="store_true")
    parser.add_argument("--list-models", nargs="?", const=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--verbose", action="store_true")

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

    return ParsedArgs(
        command=command,
        command_args=command_args,
        provider=ns.provider,
        model=ns.model,
        api_key=ns.api_key,
        llama_cpp_url=ns.llama_cpp_url,
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
        no_skills=ns.no_skills,
        skills=list(ns.skill),
        prompt_templates=list(ns.prompt_template),
        no_prompt_templates=ns.no_prompt_templates,
        themes=list(ns.theme),
        no_themes=ns.no_themes,
        list_models=ns.list_models,
        offline=ns.offline,
        verbose=ns.verbose,
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
  --system-prompt <text>
  --append-system-prompt <text>
  --mode <text|json|rpc>
  --print, -p
  --continue, -c
  --resume, -r
  --session <path>
  --fork <path-or-id>
  --session-dir <dir>
  --no-session
  --models <patterns>
  --tools <read,bash,edit,write,grep,find,ls>
  --no-tools
  --extension, -e <path>
  --no-extensions
  --skill <path>
  --no-skills
  --prompt-template <path>
  --no-prompt-templates
  --theme <path>
  --no-themes
  --export <file>
  --list-models [search]
  --offline
  --verbose
  --help, -h
  --version, -v

Commands:
  install <package>
  remove <package>
  update [package]
  list
  config [key] [value]
"""
    )
