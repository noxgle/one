"""Fail-closed parsing policy for the TUI manual command runner.

This is an invocation restriction, not an operating-system sandbox.  Development
commands can execute code checked out in the workspace, and filesystem state can
change after validation (TOCTOU). The module deliberately does not spawn a process.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn


class ManualCommandDenied(ValueError):
    """Raised when a manual command is outside the deliberately small grammar."""


@dataclass(frozen=True)
class ManualCommandPlan:
    """Validated process inputs.  ``env_tweaks`` values of None mean unset."""

    executable: str
    argv: tuple[str, ...]
    cwd: str
    env_tweaks: Mapping[str, str | None]


_SYSTEM_DIRS = (Path("/usr/bin"), Path("/bin"))
_SAFE_PATH = "/usr/bin:/bin"
_GIT_ENV = {
    "GIT_DIR": None,
    "GIT_WORK_TREE": None,
    "GIT_COMMON_DIR": None,
    "GIT_CONFIG": None,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_COUNT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}


def _deny(message: str) -> NoReturn:
    raise ManualCommandDenied(f"Manual command denied: {message}")


def _resolve_system_executable(name: str) -> str:
    """Resolve only a known system binary, never the current PATH or workspace."""
    for directory in _SYSTEM_DIRS:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    _deny(f"'{name}' is not available in trusted system directories")


def _tokens(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        _deny("enter one supported command")
    if "\x00" in command:
        _deny("NUL bytes are not allowed")
    if any(item in command for item in ("\n", ";", "&&", "||", "|", "&", ">", "<", "`", "$(", "${")):
        _deny("shell operators, redirects, substitutions, and command composition are not allowed")
    try:
        tokens = shlex.split(command, posix=True, comments=False)
        if not tokens:
            _deny("enter one supported command")
        return tokens
    except ValueError as exc:
        _deny(f"invalid command quoting ({exc})")


def _root(value: str | Path) -> Path:
    root = Path(value).resolve()
    if not root.is_dir():
        _deny("workspace root must be an existing directory")
    return root


def _path(value: str, root: Path, *, output: bool = False) -> str:
    if not value or value == "-" or value.startswith("-"):
        _deny("file operands must be workspace-relative paths, not options or standard input")
    candidate = Path(value)
    if candidate.is_absolute():
        _deny("absolute paths are not allowed")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        _deny("path escapes the workspace")
    # resolve() follows all existing parent symlinks and also finds the nearest
    # existing ancestor for a future output path.
    return str(resolved)


def _number(value: str, what: str, maximum: int = 1000) -> str:
    try:
        number = int(value)
    except ValueError:
        _deny(f"{what} must be a whole number")
    if not 0 <= number <= maximum:
        _deny(f"{what} must be between 0 and {maximum}")
    return str(number)


def _read_paths(args: list[str], root: Path) -> list[str]:
    if not args:
        _deny("at least one workspace-relative path is required")
    return [_path(arg, root) for arg in args]


def _simple(command: str, args: list[str], root: Path) -> list[str]:
    if command == "pwd":
        if args:
            _deny("pwd takes no arguments")
        return []
    if command == "ls":
        flags, paths = [], []
        for arg in args:
            if arg.startswith("-"):
                if arg == "-" or any(letter not in "alh" for letter in arg[1:]):
                    _deny("ls supports only -a, -l, and -h")
                flags.append(arg)
            else:
                paths.append(_path(arg, root))
        return flags + paths
    if command == "tree":
        out: list[str] = []
        index = 0
        if args[:1] == ["-L"]:
            if len(args) < 2:
                _deny("tree -L requires a depth")
            out.extend(["-L", _number(args[1], "tree depth", 20)])
            index = 2
        elif args and args[0].startswith("-"):
            _deny("tree supports only -L <depth>")
        paths = _read_paths(args[index:] or ["."], root)
        return out + paths
    if command in {"cat", "stat", "file"}:
        if any(arg.startswith("-") for arg in args):
            _deny(f"{command} does not permit options in manual mode")
        return _read_paths(args, root)
    if command in {"head", "tail"}:
        if len(args) < 3 or args[0] != "-n":
            _deny(f"{command} requires -n <0..1000> followed by paths; follow mode is not allowed")
        return ["-n", _number(args[1], "line count")] + _read_paths(args[2:], root)
    if command == "wc":
        flags = []
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option == "-" or any(letter not in "lwc" for letter in option[1:]):
                _deny("wc supports only -l, -w, and -c")
            flags.append(option)
        return flags + _read_paths(args, root)
    if command == "du":
        flags = []
        while args and args[0].startswith("-"):
            option = args.pop(0)
            if option == "-" or any(letter not in "sh" for letter in option[1:]):
                _deny("du supports only -s and -h")
            flags.append(option)
        paths = _read_paths(args or ["."], root)
        return flags + paths
    if command == "df":
        if any(arg not in {"-h", "--human-readable"} for arg in args):
            _deny("df supports only -h")
        return args
    _deny(f"unsupported command '{command}'")


def _search(command: str, args: list[str], root: Path) -> list[str]:
    allowed = set("niHhIwr") if command == "grep" else set("niHS")
    flags: list[str] = []
    while args and args[0].startswith("-"):
        option = args.pop(0)
        if option == "-g" and command == "rg":
            if not args:
                _deny("rg -g requires a literal glob")
            flags.extend([option, args.pop(0)])
            continue
        if option in {"--files-from", "--pre", "--pre-glob", "--config", "--exec", "-R"} or option.startswith("--"):
            _deny(f"{command} option '{option}' is not allowed")
        if not option.startswith("-") or any(letter not in allowed for letter in option[1:]):
            _deny(f"unsupported {command} option '{option}'")
        flags.append(option)
    if not args:
        _deny(f"{command} requires a pattern")
    pattern = args.pop(0)
    if pattern == "":
        _deny(f"{command} pattern must not be empty")
    # These exact grammars do not enable symlink following: rg's default is
    # no-follow; grep permits -r but rejects -R. tree and du likewise do not
    # receive their follow-symlink options. Explicit operands are canonicalized.
    paths = _read_paths(args or ["."], root)
    return flags + [pattern] + paths


def _git(args: list[str], root: Path, mode: str) -> list[str]:
    if not args or args[0].startswith("-"):
        _deny("git requires an approved subcommand without global options")
    subcommand, rest = args[0], args[1:]
    base = ["--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
    if subcommand == "status":
        if any(arg not in {"--short", "--porcelain", "--branch"} for arg in rest):
            _deny("git status supports only --short, --porcelain, and --branch")
        return base + [subcommand, *rest]
    if subcommand in {"diff", "log", "show"}:
        allowed = {"--stat", "--cached", "--staged", "--name-only", "--oneline", "--shortstat"}
        out = []
        index = 0
        while index < len(rest):
            arg = rest[index]
            if arg == "-n":
                if index + 1 >= len(rest):
                    _deny("git -n requires a bounded limit")
                out += [arg, _number(rest[index + 1], "git limit")]
                index += 2
            elif arg in allowed:
                out.append(arg)
                index += 1
            elif not arg.startswith("-") and subcommand == "show" and index == len(rest) - 1:
                out.append(arg)  # a revision, not a path or option
                index += 1
            else:
                _deny(f"unsupported git {subcommand} argument '{arg}'")
        signature = ["-c", "log.showSignature=false"] if subcommand in {"log", "show"} else []
        no_signature = ["--no-show-signature"] if subcommand in {"log", "show"} else []
        return base + signature + [subcommand, *no_signature, *out, "--no-ext-diff", "--no-textconv"]
    if subcommand == "branch":
        if rest not in ([], ["--list"], ["--show-current"]):
            _deny("git branch supports only --list or --show-current")
        return base + [subcommand, *rest]
    if subcommand == "rev-parse":
        if rest not in (["--show-toplevel"], ["--abbrev-ref", "HEAD"], ["--short", "HEAD"]):
            _deny("git rev-parse supports only --show-toplevel, --abbrev-ref HEAD, or --short HEAD")
        return base + [subcommand, *rest]
    if mode == "dev" and subcommand == "add":
        if rest[:1] == ["--"]:
            rest = rest[1:]
        if not rest or any(arg.startswith("-") for arg in rest):
            _deny("git add accepts selected workspace-relative paths only")
        return base + [subcommand, "--", *_read_paths(rest, root)]
    if mode == "dev" and subcommand == "restore":
        if not rest or rest[0] != "--staged":
            _deny("git restore is limited to --staged followed by selected workspace paths")
        paths = rest[1:]
        if paths[:1] == ["--"]:
            paths = paths[1:]
        if not paths or any(arg.startswith("-") for arg in paths):
            _deny("git restore is limited to --staged followed by selected workspace paths")
        return base + [subcommand, "--staged", "--", *_read_paths(paths, root)]
    _deny(f"git subcommand '{subcommand}' is not allowed in {mode} mode")


def _dev(command: str, args: list[str], root: Path) -> tuple[str, list[str]]:
    if command == "mkdir":
        if args[:1] == ["-p"]:
            args = args[1:]
            prefix = ["-p"]
        else:
            prefix = []
        return command, prefix + _read_paths(args, root)
    if command == "touch":
        return command, _read_paths(args, root)
    if command in {"cp", "mv"}:
        if any(arg in {"-f", "--force", "--remove-destination"} or "backup" in arg for arg in args):
            _deny(f"{command} overwrite and backup options are not allowed")
        if len(args) < 2 or any(arg.startswith("-") for arg in args):
            _deny(f"{command} requires source paths and a destination without options")
        return command, ["-n"] + _read_paths(args, root)
    if command in {"npm", "cargo", "go"}:
        allowed = {"npm": {("test",), ("run", "lint"), ("run", "build")}, "cargo": {("check",), ("test",), ("fmt", "--check")}, "go": {("test",), ("vet",)}}
        if tuple(args) not in allowed[command]:
            _deny(f"{command} supports only its documented fixed development forms")
        return command, args
    if command in {"python", ".venv/bin/python"}:
        if command == ".venv/bin/python":
            lexical = root / command
            executable = str(lexical)
            target = lexical.resolve()
            trusted_targets = {Path(_resolve_system_executable("python3")).resolve()}
            venv_bin = root / ".venv" / "bin"
            if not venv_bin.resolve().is_relative_to(root) or not lexical.is_file() or not (target.is_relative_to(root) or target in trusted_targets):
                _deny(".venv/bin/python must be the workspace virtualenv interpreter")
        else:
            executable = _resolve_system_executable("python3")
        if len(args) < 2 or args[0] != "-m":
            _deny("python is limited to approved '-m' invocations")
        module, rest = args[1], args[2:]
        if module == "pytest":
            flags = [arg for arg in rest if arg in {"-q", "-x", "--tb=short"}]
            paths = [arg for arg in rest if arg not in flags]
            if len(flags) + len(paths) != len(rest):
                _deny("pytest supports only -q, -x, --tb=short, and test paths")
            scoped_paths = []
            for path in paths:
                base, separator, selectors = path.partition("::")
                if not base:
                    _deny("pytest node IDs require a workspace-relative test path before '::'")
                scoped_paths.append(_path(base, root) + (separator + selectors if separator else ""))
            return executable, ["-m", module, *flags, *(scoped_paths or [str(root)])]
        if module == "ruff" and rest[:1] in (["check"], ["format"]):
            prefix = rest[:1]
            remainder = rest[1:]
            if prefix == ["format"]:
                if remainder[:1] != ["--check"]:
                    _deny("ruff format requires --check and workspace paths")
                remainder = remainder[1:]
                prefix.append("--check")
            if not remainder:
                _deny("ruff requires workspace paths")
            return executable, ["-m", module, *prefix, *_read_paths(remainder, root)]
        if module == "compileall":
            if any(arg != "-q" for arg in rest):
                _deny("compileall supports only optional -q")
            return executable, ["-m", module, *rest, str(root)]
        if module == "pip" and rest[:1] == ["show"] and len(rest) > 1:
            packages = rest[1:]
            if all(
                item
                and item[0].isascii()
                and item[0].isalnum()
                and all(char.isascii() and (char.isalnum() or char in "._-") for char in item[1:])
                for item in packages
            ):
                return executable, ["-m", module, *rest]
        fixed = {"build": {()}, "pip": {("check",), ("list",)}}
        if module not in fixed or tuple(rest) not in fixed[module]:
            _deny("unsupported Python module invocation")
        return executable, ["-m", module, *rest]
    _deny(f"unsupported development command '{command}'")


def validate_manual_command(command: str, workspace_root: str | Path, mode: object = "strict") -> ManualCommandPlan:
    """Parse one supported command into immutable, shell-free process inputs."""
    if not isinstance(mode, str) or mode not in {"strict", "dev"}:
        mode = "strict"
    root = _root(workspace_root)
    tokens = _tokens(command)
    name, args = tokens[0], tokens[1:]
    if name in {"pwd", "ls", "tree", "cat", "head", "tail", "wc", "stat", "file", "du", "df"}:
        argv = _simple(name, args, root)
        executable = _resolve_system_executable(name)
    elif name in {"rg", "grep"}:
        argv = _search(name, args, root)
        executable = _resolve_system_executable(name)
    elif name == "git":
        argv = _git(args, root, mode)
        executable = _resolve_system_executable(name)
    elif mode == "dev":
        executable, argv = _dev(name, args, root)
        if executable == name:
            executable = _resolve_system_executable(name)
    else:
        _deny(f"'{name}' is not supported in strict mode")
    tweaks: dict[str, str | None] = {"PATH": _SAFE_PATH, "RIPGREP_CONFIG_PATH": None}
    if name == "rg":
        argv.insert(0, "--no-config")
    if name == "git":
        tweaks.update(_GIT_ENV)
    return ManualCommandPlan(executable, tuple([executable, *argv]), str(root), MappingProxyType(tweaks))
