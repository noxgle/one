"""Cross-platform smoke tests for path resolution, display sanitization, and bash_tool shell semantics.

Platform note:
  bash_tool invokes ``asyncio.create_subprocess_shell`` which delegates to the
  platform's default shell (sh on POSIX, cmd.exe on Windows).  Shell quoting
  rules, variable expansion, and pipeline semantics differ between sh and
  cmd.exe; therefore only POSIX platforms (``os.name == "posix"``) get the
  interactive bash_tool smoke tests (tests 3-5).  Tests 1-2 and 6 are fully
  portable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import tempfile

from one.tools.bash import bash_tool
from one.tools.common import resolve_to_cwd, sanitize_display_text


# ---------------------------------------------------------------------------
# 1. Portable ``resolve_to_cwd``
# ---------------------------------------------------------------------------


def test_resolve_to_cwd_relative_nested_under_tmp_cwd(tmp_path: Path) -> None:
    """A relative nested path (``sub/dir/file``) must resolve beneath the given cwd."""
    cwd = tmp_path / "sandbox"
    cwd.mkdir()
    result = resolve_to_cwd("sub/dir/file", str(cwd))

    # pathlib .resolve() is always absolute — use it as the expected baseline.
    expected = (cwd / "sub" / "dir" / "file").resolve()
    assert result == expected
    # Verify the resolved path still has the cwd as its parent anchor.
    assert result.is_relative_to(cwd.resolve())


def test_resolve_to_cwd_absolute_remains_absolute(tmp_path: Path) -> None:
    """An absolute path must pass through unchanged (resolved)."""
    abs_target = tmp_path / "absolute" / "target.txt"
    abs_target.parent.mkdir(parents=True, exist_ok=True)

    result = resolve_to_cwd(str(abs_target), "/some/other/cwd")

    expected = abs_target.resolve()
    assert result == expected
    assert result.is_absolute()
    # Must NOT be anchored under the provided cwd.
    assert not result.is_relative_to(tmp_path / "sandbox" / "cwd")


# ---------------------------------------------------------------------------
# 2. Portable display sanitization
# ---------------------------------------------------------------------------


def test_sanitize_display_text_crlf_normalized_to_lf() -> None:
    """CRLF and lone CR sequences must be normalized to LF."""
    result = sanitize_display_text("line1\r\nline2\rline3\r\nline4")
    assert result == "line1\nline2\nline3\nline4"
    # No residual CR or CRLF.
    assert "\r" not in result


def test_sanitize_display_text_ansi_and_control_replaced() -> None:
    """ANSI sequences and non-safe control characters must be stripped/replaced."""
    # ANSI CSI (SGR red), OSC, lone ESC, C1 U+0080, DEL
    raw = "\x1b[31mred\x1b[0m\x1b]0;title\x07\x1b\x80\x7f"
    result = sanitize_display_text(raw)
    # After ANSI stripping: "red" + \x80 + \x7f
    # C1 and DEL -> U+FFFD
    assert "red" in result
    assert "\x1b" not in result
    assert "\x07" not in result  # BEL removed by strip_ansi (OSC)
    assert "\x80" not in result
    assert "\x7f" not in result
    assert "\ufffd" in result  # replacement chars for stripped controls


# ---------------------------------------------------------------------------
# 3-5. POSIX-only bash_tool smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="bash_tool shell semantics differ on Windows (cmd.exe)")
@pytest.mark.asyncio
async def test_bash_tool_posix_smoke_cwd_quoting_pipeline(tmp_path: Path) -> None:
    """CWD with spaces, quoted ``$PWD``, and a pipeline (tr a-z A-Z).

    Verifies:
      - The working directory (with spaces) is correctly passed as cwd.
      - Shell variable expansion ``$PWD`` inside double quotes works.
      - A pipeline (``printf ... | tr ...``) is honoured.
      - Output is non-truncated and non-fullscreen.
    """
    # Create a directory whose name contains spaces.
    workdir = tmp_path / "my dir with spaces"
    workdir.mkdir()

    resolved = str(workdir.resolve())

    result = await bash_tool(
        str(workdir),
        "printf '%s|%s' \"$PWD\" \"$(printf 'ok' | tr a-z A-Z)\"",
    )

    assert result["exitCode"] == 0
    assert not result["truncated"], "Output should not be truncated"
    assert not result["fullscreen"], "Output should not be flagged as fullscreen"
    # Exact semantic assertion: proves cwd resolution + quoting + pipeline
    # output (``ok`` → ``OK`` via ``tr a-z A-Z``), not merely that a pipe
    # character appeared somewhere in the output.
    assert result["output"].strip() == f"{resolved}|OK"


@pytest.mark.skipif(os.name == "nt", reason="command_prefix is POSIX shell-focused")
@pytest.mark.asyncio
async def test_bash_tool_command_prefix(tmp_path: Path) -> None:
    """``command_prefix`` is prepended before the command string."""
    workdir = tmp_path / "prefix-test"
    workdir.mkdir()

    result = await bash_tool(
        str(workdir),
        "printf '%s' \"$ONE_SMOKE\"",
        command_prefix="export ONE_SMOKE=prefix-ok",
    )

    assert result["exitCode"] == 0
    assert result["output"].strip() == "prefix-ok"
    assert not result["truncated"]
    assert not result["fullscreen"]


@pytest.mark.skipif(os.name == "nt", reason="exit-code semantics verified via POSIX sh")
@pytest.mark.asyncio
async def test_bash_tool_posix_failure_raises_error_code(tmp_path: Path) -> None:
    """A command that exits non-zero must raise ``RuntimeError`` containing the exit code."""
    workdir = tmp_path / "fail-test"
    workdir.mkdir()

    with pytest.raises(RuntimeError) as exc:
        await bash_tool(str(workdir), "exit 7")

    msg = str(exc.value)
    assert "code 7" in msg


# ---------------------------------------------------------------------------
# 6. Platform metadata smoke — runs on all platforms
# ---------------------------------------------------------------------------


def test_platform_metadata_basic_compatibility() -> None:
    """Ensure the runtime platform is recognized and basic filesystem ops work."""
    # os.name must be one of the standard CPython values.
    assert os.name in {"posix", "nt"}, f"Unexpected os.name: {os.name!r}"

    tmp_path = Path(tempfile.mkdtemp(prefix="one-smoke-platform-"))
    try:
        # Create a file with unicode + spaces in the name.
        unicode_name = "caf\u00e9 r\u00e9sum\u00e9.txt"
        file_path = tmp_path / unicode_name
        file_path.write_text("platform-ok\n", encoding="utf-8")

        # Verify we can read it back.
        content = file_path.read_text(encoding="utf-8")
        assert content == "platform-ok\n"

        # pathlib operations work with spaces.
        space_name = "hello world.txt"
        (tmp_path / space_name).write_text("spaces-ok\n", encoding="utf-8")
        assert (tmp_path / space_name).read_text(encoding="utf-8") == "spaces-ok\n"
    finally:
        import shutil
        shutil.rmtree(str(tmp_path), ignore_errors=True)


# ---------------------------------------------------------------------------
