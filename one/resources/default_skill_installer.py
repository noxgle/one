"""First-run installation of packaged default skills."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import logging
from collections.abc import Iterable
from importlib import resources
from pathlib import Path

import yaml

_DEFAULT_SKILL_NAMES = ("explore", "one")
_LOG = logging.getLogger(__name__)


def default_skill_bytes(name: str) -> bytes:
    """Return the packaged bytes for a default skill."""
    if name not in _DEFAULT_SKILL_NAMES:
        raise ValueError(f"Unknown default skill: {name}")
    return resources.files("one.resources").joinpath("default_skills", name, "SKILL.md").read_bytes()


def _root_has_skill_named(root: Path, name: str) -> bool:
    """Return whether *root* contains a readable skill declaring *name*."""
    if not root.is_dir():
        return False
    try:
        files = root.rglob("SKILL.md")
        for skill_file in files:
            try:
                frontmatter = skill_file.read_text(encoding="utf-8").split("---", 2)
                if len(frontmatter) >= 3:
                    metadata = yaml.safe_load(frontmatter[1]) or {}
                    if isinstance(metadata, dict) and metadata.get("name") == name:
                        return True
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                continue
    except OSError:
        return False
    return False


def install_default_skills(
    agent_dir: str | Path,
    cwd: str | Path,
    *,
    external_skill_roots: Iterable[str | Path] | None = None,
) -> list[str]:
    """Best-effort, non-destructive installation of packaged default skills.

    Existing user files always win.  We also decline to install a default whose
    name is already supplied by a platform-wide or project skill: this avoids a
    first-run copy unexpectedly changing the established discovery winner.
    """
    agent_path = Path(agent_dir)
    roots = (
        [Path(root) for root in external_skill_roots]
        if external_skill_roots is not None
        else [Path.home() / ".agents" / "skills", Path(cwd) / ".one" / "skills"]
    )
    diagnostics: list[str] = []

    for name in _DEFAULT_SKILL_NAMES:
        target_dir = agent_path / "skills" / name
        target = target_dir / "SKILL.md"
        if target.exists() or target.is_symlink():
            continue
        if any(_root_has_skill_named(root, name) for root in roots):
            diagnostics.append(f"default skill '{name}' not installed: a lower-precedence skill already provides that name")
            continue
        try:
            if target_dir.exists() and (not target_dir.is_dir() or target_dir.is_symlink()):
                raise OSError("destination skill directory is not a normal directory")
            target_dir.mkdir(parents=True, exist_ok=True)
            # Exclusive creation is race-safe and guarantees an existing user
            # file is never overwritten.
            with target.open("xb") as destination:
                destination.write(default_skill_bytes(name))
        except (OSError, ValueError) as exc:
            message = f"default skill '{name}' not installed: {type(exc).__name__}: {exc}"
            diagnostics.append(message)
            _LOG.warning(message)

    return diagnostics
