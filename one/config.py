from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

APP_NAME = "one"
LEGACY_CONFIG_DIR_NAME = ".one"
VERSION = "0.1.15"
ENV_AGENT_DIR = f"{APP_NAME.upper()}_CODING_AGENT_DIR"


def _expand(path: str) -> str:
    return str(Path(path).expanduser())


def _tighten_migrated_state(agent_dir: Path) -> None:
    """Best-effort permission tightening after a successful legacy migration.

    Uses the public persistence helpers via a local import to avoid
    circular-import risk with ``one/core/persistence``.  On failure the
    already-copied files remain untouched and the caller has already
    selected the XDG path.
    """
    from one.core.persistence import ensure_private_dir, ensure_private_file

    # Top-level agent dir → 0700.
    try:
        ensure_private_dir(agent_dir)
    except OSError:
        pass

    # Sensitive files at the top level → 0600.
    for child in agent_dir.iterdir():
        if child.is_file() and child.name in (
            "auth.json",
            "settings.json",
            "models.json",
        ):
            try:
                ensure_private_file(child, 0o600)
            except OSError:
                pass
        elif child.is_file() and child.name.endswith(".jsonl"):
            # Top-level reports.jsonl and similar state JSONL files.
            try:
                ensure_private_file(child, 0o600)
            except OSError:
                pass

    # Recursively tighten session layout.
    sessions_dir = agent_dir / "sessions"
    if sessions_dir.is_dir():
        try:
            ensure_private_dir(sessions_dir)
        except OSError:
            pass
        for root, dirs, files in os.walk(str(sessions_dir)):
            root_path = Path(root)
            for d in dirs:
                try:
                    ensure_private_dir(root_path / d)
                except OSError:
                    pass
            for f in files:
                if f.endswith(".jsonl"):
                    try:
                        ensure_private_file(root_path / f, 0o600)
                    except OSError:
                        pass


def get_agent_dir() -> str:
    env = os.getenv(ENV_AGENT_DIR)
    if env:
        return _expand(env)
    xdg_home = Path(_expand(os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")))
    xdg_agent_dir = xdg_home / APP_NAME
    legacy_agent_dir = Path.home() / LEGACY_CONFIG_DIR_NAME / "agent"
    # One-time migration from legacy ~/.one/agent -> ~/.config/one.
    if not xdg_agent_dir.exists() and legacy_agent_dir.exists():
        try:
            xdg_agent_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(legacy_agent_dir, xdg_agent_dir)
        except Exception:
            # If migration fails, keep backward-compatible behavior.
            return str(legacy_agent_dir)
        # Migration copy succeeded — tighten permissions (best-effort).
        # Permission failure must NOT change the selected path.
        try:
            _tighten_migrated_state(xdg_agent_dir)
        except OSError:
            pass
        return str(xdg_agent_dir)
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
