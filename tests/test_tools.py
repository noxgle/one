from __future__ import annotations

from pathlib import Path

import pytest

from one.tools.edit import edit_tool
from one.tools.find import find_tool
from one.tools.grep import grep_tool
from one.tools.ls import ls_tool
from one.tools.read import read_tool
from one.tools.write import write_tool


def test_write_and_read_basic(tmp_path: Path):
    write_tool(str(tmp_path), "a.txt", "line1\nline2\nline3")
    out = read_tool(str(tmp_path), "a.txt", offset=1, limit=2)
    assert "line1" in out["content"][0]["text"]
    assert "Use offset=3 to continue." in out["content"][0]["text"]


def test_read_errors_and_truncation(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        read_tool(str(tmp_path), "missing.txt")

    # 2001 lines should trigger head truncation (default max lines = 2000).
    many_lines = "\n".join(f"line-{i}" for i in range(1, 2002))
    write_tool(str(tmp_path), "big.txt", many_lines)
    out = read_tool(str(tmp_path), "big.txt")
    text = out["content"][0]["text"]
    assert "Use offset=2001 to continue." in text
    assert out["details"]["truncation"] is not None

    with pytest.raises(ValueError):
        read_tool(str(tmp_path), "big.txt", offset=99999)


def test_edit_success_and_errors(tmp_path: Path):
    write_tool(str(tmp_path), "a.txt", "line1\nline2\nline3")

    edit_tool(str(tmp_path), "a.txt", [{"oldText": "line2", "newText": "LINE2"}])
    after = read_tool(str(tmp_path), "a.txt")
    assert "LINE2" in after["content"][0]["text"]
    assert "line2" not in after["content"][0]["text"]

    with pytest.raises(ValueError):
        edit_tool(str(tmp_path), "a.txt", [])

    with pytest.raises(FileNotFoundError):
        edit_tool(str(tmp_path), "missing.txt", [{"oldText": "x", "newText": "y"}])

    # Non-unique oldText should fail.
    write_tool(str(tmp_path), "dupe.txt", "x\nx\n")
    with pytest.raises(ValueError):
        edit_tool(str(tmp_path), "dupe.txt", [{"oldText": "x", "newText": "y"}])

    # Missing oldText should fail.
    with pytest.raises(ValueError):
        edit_tool(str(tmp_path), "a.txt", [{"oldText": "does-not-exist", "newText": "X"}])


def test_find_grep_ls_basic(tmp_path: Path):
    (tmp_path / "x.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "y.txt").write_text("hello world\n", encoding="utf-8")

    assert "x.py" in find_tool(str(tmp_path), "*.py")["content"][0]["text"]
    assert "hello world" in grep_tool(str(tmp_path), "hello")["content"][0]["text"]
    assert "x.py" in ls_tool(str(tmp_path))["content"][0]["text"]


def test_find_grep_ls_errors_and_empty(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        find_tool(str(tmp_path), "*.py", "missing")

    with pytest.raises(FileNotFoundError):
        grep_tool(str(tmp_path), "hello", "missing")

    with pytest.raises(FileNotFoundError):
        ls_tool(str(tmp_path), "missing")

    (tmp_path / "nested").mkdir()
    no_files = find_tool(str(tmp_path), "*.doesnotexist")["content"][0]["text"]
    assert no_files == "No files found"

    no_matches = grep_tool(str(tmp_path), "definitely-no-match")["content"][0]["text"]
    assert no_matches == "No matches"

    file_path = tmp_path / "single.txt"
    file_path.write_text("content", encoding="utf-8")
    ls_file = ls_tool(str(tmp_path), "single.txt")["content"][0]["text"]
    assert ls_file.endswith("single.txt")


def test_bash_tool_success(tmp_path: Path):
    import asyncio

    from one.tools.bash import bash_tool

    result = asyncio.run(bash_tool(str(tmp_path), "echo hello"))
    assert "hello" in result["content"][0]["text"]
    assert result["exitCode"] == 0
    assert result["truncated"] is False


def test_bash_tool_nonzero_exit_raises(tmp_path: Path):
    import asyncio

    from one.tools.bash import bash_tool

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(bash_tool(str(tmp_path), "echo bad && exit 7", timeout=2))
    msg = str(exc.value)
    assert "bad" in msg or "timed out" in msg.lower()
    assert "Command exited with code" in msg
