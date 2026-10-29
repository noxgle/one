"""Preset parser tests."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import pytest

from one.core.presets import PresetError, discover_presets, parse_preset


def test_packaged_presets_are_discoverable(tmp_path) -> None:
    presets = discover_presets(str(tmp_path))
    assert set(presets) == {"default", "readonly", "explore"}
    assert presets["readonly"].tools and "bash" not in presets["readonly"].tools


@pytest.mark.parametrize("header", ["thinking: true", "unknown: nope", "temperature: .nan"])
def test_preset_parser_rejects_unsafe_or_unknown_values(header: str) -> None:
    content = f"---\nversion: 1\nid: safe\nname: Safe\n{header}\n---\nrole"
    with pytest.raises(PresetError):
        parse_preset(content)


def test_preset_parser_rejects_duplicate_keys_and_sandboxes() -> None:
    with pytest.raises(PresetError, match="duplicate"):
        parse_preset("---\nversion: 1\nid: safe\nid: other\nname: Safe\n---\n")
    preset = parse_preset("---\nversion: 1\nid: safe\nname: Safe\nsandboxes: [future]\n---\nrole")
    assert preset.sandboxes == ("future",)


@pytest.mark.parametrize(("header", "expected"), [
    ("", None),
    ("mcp_servers: []", ()),
    ("mcp_servers: [alpha, beta]", ("alpha", "beta")),
])
def test_preset_mcp_servers(header: str, expected: tuple[str, ...] | None) -> None:
    preset = parse_preset(f"---\nversion: 1\nid: safe\nname: Safe\n{header}\n---\n")
    assert preset.mcp_servers == expected


@pytest.mark.parametrize("value", [
    "null", "true", "server", "{}", "[null]", "[true]", "[123]", "[[]]",
    "['']", "['   ']", "[' alpha']", "['alpha ']", "[alpha, alpha]",
])
def test_preset_rejects_invalid_mcp_servers(value: str) -> None:
    with pytest.raises(PresetError, match="mcp_servers"):
        parse_preset(f"---\nversion: 1\nid: safe\nname: Safe\nmcp_servers: {value}\n---\n")


@pytest.mark.parametrize("ident", ["default", "readonly", "explore"])
def test_user_collision_with_reserved_preset_fails(tmp_path, ident: str) -> None:
    directory = tmp_path / "presets"
    directory.mkdir()
    (directory / f"{ident}.md").write_text(
        f"---\nversion: 1\nid: {ident}\nname: Override\nmcp_servers: []\n---\n", encoding="utf-8"
    )
    with pytest.raises(PresetError, match="duplicate"):
        discover_presets(str(tmp_path))
