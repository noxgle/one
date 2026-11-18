# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from one.core.tool_text_protocols import is_dsml_tool_call_candidate, parse_dsml_tool_calls

_DSML = "｜DSML｜"


def _block(*invokes: str) -> str:
    return f"<{_DSML}tool_calls>\n{''.join(invokes)}\n</{_DSML}tool_calls>"


def _invoke(name: str, *parameters: str) -> str:
    return f'<{_DSML}invoke name="{name}">\n{"".join(parameters)}\n</{_DSML}invoke>\n'


def _parameter(name: str, value: str, *, string: bool = True) -> str:
    kind = "true" if string else "false"
    return f'<{_DSML}parameter name="{name}" string="{kind}">{value}</{_DSML}parameter>\n'


def test_parse_single_invoke_with_typed_parameters() -> None:
    text = _block(_invoke("read", _parameter("path", "a.txt"), _parameter("limit", "10", string=False)))
    assert parse_dsml_tool_calls(text) == [
        {"id": "", "name": "read", "arguments": {"path": "a.txt", "limit": 10}}
    ]


def test_parse_multiple_invokes_preserves_order() -> None:
    text = _block(
        _invoke("read", _parameter("path", "a.txt")),
        _invoke("bash", _parameter("command", "echo ok")),
    )
    calls = parse_dsml_tool_calls(text)
    assert calls is not None
    assert [call["name"] for call in calls] == ["read", "bash"]
    assert calls[1]["arguments"] == {"command": "echo ok"}


def test_string_parameter_preserves_json_like_text_and_newlines() -> None:
    value = '{"path": "a.txt"}\nsecond line'
    text = _block(_invoke("write", _parameter("content", value)))
    calls = parse_dsml_tool_calls(text)
    assert calls is not None
    assert calls[0]["arguments"]["content"] == value


def test_invalid_json_typed_parameter_rejects_the_whole_block() -> None:
    text = _block(_invoke("read", _parameter("limit", "{not json", string=False)))
    assert parse_dsml_tool_calls(text) is None


def test_invoke_without_parameters_parses_as_empty_arguments() -> None:
    text = _block(f'<{_DSML}invoke name="todo_list">\n</{_DSML}invoke>')
    assert parse_dsml_tool_calls(text) == [{"id": "", "name": "todo_list", "arguments": {}}]


def test_duplicate_parameter_name_is_rejected() -> None:
    text = _block(_invoke("read", _parameter("path", "a"), _parameter("path", "b")))
    assert parse_dsml_tool_calls(text) is None


def test_incomplete_block_is_a_candidate_but_not_parseable() -> None:
    text = f'<{_DSML}tool_calls>\n<{_DSML}invoke name="read">'
    assert is_dsml_tool_call_candidate(text) is True
    assert parse_dsml_tool_calls(text) is None


def test_prose_example_is_not_a_candidate() -> None:
    text = "Example: " + _block(_invoke("bash", _parameter("command", "echo ok")))
    assert is_dsml_tool_call_candidate(text) is False
    assert parse_dsml_tool_calls(text) is None


def test_leading_think_block_is_ignored() -> None:
    text = "<think>plan</think >\n" + _block(_invoke("read", _parameter("path", "a.txt")))
    calls = parse_dsml_tool_calls(text)
    assert calls is not None
    assert calls[0]["arguments"] == {"path": "a.txt"}


def test_ascii_pipe_variant_is_accepted() -> None:
    text = (
        '<|DSML|tool_calls><|DSML|invoke name="read">'
        '<|DSML|parameter name="path" string="true">a.txt</|DSML|parameter>'
        "</|DSML|invoke></|DSML|tool_calls>"
    )
    assert parse_dsml_tool_calls(text) == [{"id": "", "name": "read", "arguments": {"path": "a.txt"}}]
