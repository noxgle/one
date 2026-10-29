"""Strict, data-only named agent preset definitions."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.tokens import AliasToken, AnchorToken

from one.config import get_agent_dir
from one.core.temperature import normalize_temperature

_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_MAX_FILE = 256 * 1024
_MAX_HEADER = 32 * 1024
_KEYS = {"version", "id", "name", "model", "temperature", "thinking", "description", "tools", "sandboxes", "mcp_servers"}
_THINKING = {"off", "on", "minimal", "low", "medium", "high", "xhigh"}


class PresetError(ValueError):
    pass


class _Loader(yaml.SafeLoader):
    pass


def _mapping(loader: _Loader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise PresetError("frontmatter keys must be strings")
        if key in result:
            raise PresetError(f"duplicate frontmatter key: {key}")
        # YAML 1.1 treats unquoted `on`/`off` as booleans.  Preserve only
        # this enum field as text; other booleans keep their normal type.
        if key == "thinking" and isinstance(value_node, yaml.ScalarNode) and value_node.tag == "tag:yaml.org,2002:bool":
            result[key] = value_node.value
        else:
            result[key] = loader.construct_object(value_node, deep=deep)
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class PresetDefinition:
    id: str
    name: str
    body: str
    source: str
    model: str | None = None
    temperature: float | None = None
    thinking: str | None = None
    description: str | None = None
    tools: tuple[str, ...] | None = None
    sandboxes: tuple[str, ...] = ()
    content_hash: str = ""
    mcp_servers: tuple[str, ...] | None = None


def parse_preset(content: str, source: str = "<preset>") -> PresetDefinition:
    if len(content.encode("utf-8")) > _MAX_FILE:
        raise PresetError("preset file exceeds 256 KiB")
    if not (content.startswith("---\n") or content.startswith("---\r\n")):
        raise PresetError("preset must start with YAML frontmatter")
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise PresetError("preset must start with YAML frontmatter")
    consumed = len(lines[0])
    end = None
    for line in lines[1:]:
        if consumed > _MAX_HEADER:
            break
        if line.rstrip("\r\n") == "---":
            end = consumed
            consumed += len(line)
            break
        consumed += len(line)
    if end is None:
        raise PresetError("preset frontmatter is missing or too large")
    header = content[len(lines[0]):end]
    body = content[consumed:]
    try:
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(header)):
            raise PresetError("YAML anchors and aliases are not permitted")
        data = yaml.load(header, Loader=_Loader)
    except (yaml.YAMLError, PresetError, TypeError) as exc:
        raise PresetError(f"invalid preset frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise PresetError("preset frontmatter must be a mapping")
    unknown = set(data) - _KEYS
    if unknown:
        raise PresetError(f"unknown preset field(s): {', '.join(sorted(map(str, unknown)))}")
    if type(data.get("version")) is not int or data.get("version") != 1:
        raise PresetError("preset version must be integer 1")
    ident, name = data.get("id"), data.get("name")
    if not isinstance(ident, str) or not _ID.fullmatch(ident):
        raise PresetError("preset id must be a lowercase slug")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise PresetError("preset name must be a non-empty string up to 120 characters")
    model = data.get("model")
    if model is not None and (not isinstance(model, str) or "/" not in model or not all(model.split("/", 1))):
        raise PresetError("preset model must be provider/model")
    temperature = data.get("temperature")
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(temperature)):
        raise PresetError("preset temperature must be a finite number")
    if temperature is not None:
        try:
            temperature = normalize_temperature(temperature)
        except ValueError as exc:
            raise PresetError(str(exc)) from exc
    thinking = data.get("thinking")
    if thinking is not None and (not isinstance(thinking, str) or thinking not in _THINKING):
        raise PresetError("preset thinking must be one of: " + ", ".join(sorted(_THINKING)))
    description = data.get("description")
    if description is not None and (not isinstance(description, str) or len(description) > 500):
        raise PresetError("preset description must be a string up to 500 characters")
    tools = data.get("tools")
    if "tools" in data and (not isinstance(tools, list) or not tools or not all(isinstance(x, str) and x for x in tools) or len(set(tools)) != len(tools)):
        raise PresetError("preset tools must be a non-empty list of unique strings")
    if tools is not None and any("*" in x for x in tools) and tools != ["*"]:
        raise PresetError("preset tools wildcard must be the sole entry")
    mcp_servers = data.get("mcp_servers")
    if "mcp_servers" in data and (
        not isinstance(mcp_servers, list)
        or not all(isinstance(x, str) and x and x == x.strip() for x in mcp_servers)
        or len(set(mcp_servers)) != len(mcp_servers)
    ):
        raise PresetError("preset mcp_servers must be a list of unique, non-empty trimmed server names without '*'")
    if mcp_servers is not None and any("*" in x for x in mcp_servers) and mcp_servers != ["*"]:
        raise PresetError("preset mcp_servers wildcard must be the sole entry")
    sandboxes = data.get("sandboxes", [])
    if not isinstance(sandboxes, list) or not all(isinstance(x, str) and x for x in sandboxes):
        raise PresetError("preset sandboxes must be a list of non-empty strings")
    return PresetDefinition(ident, name.strip(), body, source, model, float(temperature) if temperature is not None else None,
                            thinking, description, tuple(tools) if tools is not None else None, tuple(sandboxes),
                            hashlib.sha256(content.encode()).hexdigest(),
                            tuple(mcp_servers) if mcp_servers is not None else None)


def _files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    resolved = root.resolve()
    out: list[Path] = []
    for path in sorted(root.glob("*.md")):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(resolved):
            raise PresetError(f"preset path escapes configured root: {path}")
        out.append(path)
    return out


def discover_presets(agent_dir: str | None = None) -> dict[str, PresetDefinition]:
    packaged = Path(__file__).parent.parent / "resources" / "presets"
    roots = [packaged, Path(agent_dir or get_agent_dir()) / "presets"]
    found: dict[str, PresetDefinition] = {}
    for root in roots:
        for path in _files(root):
            try:
                # Bound the actual read, not only a preceding stat which can
                # race a replacement file.  Decode failures are preset errors,
                # never leaked file contents or platform diagnostics.
                with path.open("rb") as stream:
                    raw = stream.read(_MAX_FILE + 1)
                if len(raw) > _MAX_FILE:
                    raise PresetError("preset file exceeds 256 KiB")
                content = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise PresetError("cannot read preset file") from exc
            definition = parse_preset(content, str(path))
            if path.stem != definition.id:
                raise PresetError(f"preset filename must match id: {path.name}")
            if definition.id in found:
                raise PresetError(f"duplicate preset id: {definition.id}")
            found[definition.id] = definition
    return found
