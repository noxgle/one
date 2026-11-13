# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import pytest

from one.core.compaction.constraints import Constraint, render_ledger
from one.core.compaction.summary import checkpoint_prompt, extractive_checkpoint, validate_checkpoint


def test_fallback_and_validation():
    ledger = (Constraint("Never push.", 0, "u1"),)
    messages = [{"role": "user", "content": "Build it"},
                {"role": "assistant", "content": [{"type": "toolCall", "name": "edit", "arguments": {"path": "one/a.py"}}]},
                {"role": "toolResult", "content": "error: edit failed", "isError": True},
                {"role": "user", "customType": "plan", "content": "Retry edit"}]
    output = extractive_checkpoint(messages, ledger, estimate_tokens=len)
    assert validate_checkpoint(output, estimate_tokens=len, ledger=ledger)
    assert "None confirmed" in output and "Untrusted tool error: error: edit failed" in output
    assert "one/a.py" in output and "Retry edit" in output
    assert render_ledger(ledger) in checkpoint_prompt(ledger, "Focus on errors")
    assert not validate_checkpoint(output, estimate_tokens=len, ledger=ledger, tool_calls=[{}])
    assert not validate_checkpoint(output + "\n{{placeholder}}", estimate_tokens=len)
    assert not validate_checkpoint(output, estimate_tokens=len, max_output_tokens=10)
    assert not validate_checkpoint(output.replace("Never push.", "Always push."), estimate_tokens=len, ledger=ledger)
    with pytest.raises(ValueError):
        extractive_checkpoint(messages, ledger, estimate_tokens=len, max_output_tokens=10)


def test_file_limit():
    messages = [{"role": "assistant", "content": [{"type": "toolCall", "name": "read", "arguments": {"path": f"src/{i}.py"}} for i in range(20)]}]
    output = extractive_checkpoint(messages, (), estimate_tokens=len)
    assert "src/14.py" in output and "src/15.py" not in output
    assert not validate_checkpoint(output.replace("src/14.py", "src/14.py\nsrc/15.py"), estimate_tokens=len)


def test_fallback_native_multimodal_and_structured_plan():
    messages = [
        {"role": "user", "content": [{"type": "image", "data": "SECRET"}, {"type": "text", "text": "Fix parser"}]},
        {"role": "assistant", "content": "", "_nativeToolCalls": [
            {"name": "edit", "arguments": {"path": "src/parser.py"}},
            {"name": "read", "arguments": '{"path":"src/input.py"}'},
            {"name": "read", "arguments": "bad json"}]},
        {"role": "user", "customType": "plan", "content": [{"step": "Inspect parser", "status": "in_progress"}]},
        {"role": "toolResult", "content": '{"ok": false, "error": "denied"}'},
    ]
    output = extractive_checkpoint(messages, (), estimate_tokens=len)
    assert "Fix parser" in output and "SECRET" not in output
    assert "src/parser.py" in output and "src/input.py" in output
    assert "in_progress: Inspect parser" in output and "Untrusted tool error" in output
    assert "## Completed\nNone confirmed" in output
    assert output == extractive_checkpoint(messages, (), estimate_tokens=len)
    assert "Prior goal" in extractive_checkpoint(messages, (), estimate_tokens=len, previous_objective="Prior goal")


@pytest.mark.parametrize("residue", ["<template>", "This summary contains the history.",
                                    "Summarizing the conversation", '<tool_call>read</tool_call>',
                                    '{"tool": "read"}', 'function_call: read'])
def test_rejects_meta_and_tool_syntax(residue):
    output = extractive_checkpoint([], (), estimate_tokens=len)
    assert not validate_checkpoint(output + residue, estimate_tokens=len)


def test_heading_terse_and_ledger_exemption():
    ledger = (Constraint('Never emit {"tool": "read"} or <template>.', 0),)
    output = extractive_checkpoint([], ledger, estimate_tokens=len)
    assert validate_checkpoint(output, estimate_tokens=len, ledger=ledger)
    assert not validate_checkpoint(output.replace("## Completed", "# Completed"), estimate_tokens=len, ledger=ledger)
    assert not validate_checkpoint(output.replace("# Objective", "## Objective"), estimate_tokens=len, ledger=ledger)
    assert not validate_checkpoint(output.replace("Unknown", "x" * 4001, 1), estimate_tokens=len,
                                   max_output_tokens=10000, ledger=ledger)
