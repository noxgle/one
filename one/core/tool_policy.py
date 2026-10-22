"""Tool capability ceilings for named presets."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from dataclasses import dataclass

READONLY_TOOLS = frozenset({"read", "read_image", "grep", "find", "ls", "evidence_read", "finish", "ask_user", "spawn_subagent"})


@dataclass(frozen=True)
class ToolPolicy:
    allowed: frozenset[str] | None = None

    def permits(self, name: str) -> bool:
        return self.allowed is None or name in self.allowed

    def intersect(self, names: set[str] | frozenset[str] | None) -> ToolPolicy:
        if names is None:
            return self
        values = frozenset(names)
        return ToolPolicy(values if self.allowed is None else self.allowed & values)

    @property
    def is_readonly(self) -> bool:
        return self.allowed is not None and self.allowed.issubset(READONLY_TOOLS)

    @classmethod
    def readonly(cls) -> ToolPolicy:
        return cls(READONLY_TOOLS)
