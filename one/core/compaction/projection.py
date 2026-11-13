"""Detached projections of canonical compaction messages."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Literal


def project_compaction_summary(
    messages: Sequence[Mapping[str, Any]], position: Literal["prefix", "suffix"] = "prefix",
) -> list[dict[str, Any]]:
    """Copy messages, optionally moving summaries to the end in stable order."""
    if position not in ("prefix", "suffix"):
        raise ValueError("invalid summary position")
    result = deepcopy([dict(message) for message in messages])
    if position == "suffix":
        return ([m for m in result if m.get("customType") != "compaction_summary"]
                + [m for m in result if m.get("customType") == "compaction_summary"])
    return result
