from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "one"
LEGACY_CONFIG_DIR_NAME = ".one"
VERSION = "0.1.0"
ENV_AGENT_DIR = f"{APP_NAME.upper()}_CODING_AGENT_DIR"


def _expand(path: str) -> str:
    return str(Path(path).expanduser())


def get_agent_dir() -> str:
    env = os.getenv(ENV_AGENT_DIR)
    if env:
        return _expand(env)
    xdg_home = Path(_expand(os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")))
    xdg_agent_dir = xdg_home / APP_NAME
    legacy_agent_dir = Path.home() / LEGACY_CONFIG_DIR_NAME / "agent"
    # Backward compatibility: if legacy path exists and new path does not, keep using legacy.
    if not xdg_agent_dir.exists() and legacy_agent_dir.exists():
        return str(legacy_agent_dir)
    return str(xdg_agent_dir)


def get_models_path() -> str:
    return str(Path(get_agent_dir()) / "models.json")


def get_auth_path() -> str:
    return str(Path(get_agent_dir()) / "auth.json")


def get_settings_path() -> str:
    return str(Path(get_agent_dir()) / "settings.json")


def get_sessions_dir() -> str:
    return str(Path(get_agent_dir()) / "sessions")


def get_package_json(path: str | None = None) -> dict:
    p = Path(path or "package.json")
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}
