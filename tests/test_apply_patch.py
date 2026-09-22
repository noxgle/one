from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from one.core.agent_session import AgentSession
from one.tools.apply_patch import apply_patch_tool


def _cwd(tmp_path: Path) -> str:
    return str(tmp_path)


@pytest.mark.asyncio
async def test_registered_apply_patch_dispatches_from_agent_session(tmp_path: Path) -> None:
    session = object.__new__(AgentSession)
    session._active_tools = ["apply_patch"]
    session.session_manager = type("SessionManager", (), {"cwd": str(tmp_path)})()
    result = await session._execute_tool_by_name(
        "apply_patch", {"patchText": "*** Begin Patch\n*** Add File: dispatched.txt\n+ok\n*** End Patch\n"},
    )
    assert result["content"][0]["text"].startswith("Success.")
    assert (tmp_path / "dispatched.txt").read_text(encoding="utf-8") == "ok\n"


# ── Add File ──────────────────────────────────────────────────────────────────


def test_add_file(tmp_path: Path):
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: hello.txt\n+Hello world\n*** End Patch\n"
    result = apply_patch_tool(cwd, patch)
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "Hello world\n"
    assert result["content"][0]["text"].startswith("Success. Updated the following files:")
    assert "A hello.txt" in result["content"][0]["text"]


def test_add_file_trailing_newline_ensured(tmp_path: Path):
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: no_newline.txt\n+no trailing newline\n*** End Patch\n"
    apply_patch_tool(cwd, patch)
    content = (tmp_path / "no_newline.txt").read_text(encoding="utf-8")
    assert content.endswith("\n")


def test_add_file_multiline(tmp_path: Path):
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: multi.txt\n"
        "+line one\n"
        "+line two\n"
        "+line three\n"
        "*** End Patch\n"
    )
    apply_patch_tool(cwd, patch)
    assert (tmp_path / "multi.txt").read_text(encoding="utf-8") == "line one\nline two\nline three\n"


def test_add_file_creates_parent_dirs(tmp_path: Path):
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: subdir/deep/file.txt\n+deep content\n*** End Patch\n"
    apply_patch_tool(cwd, patch)
    assert (tmp_path / "subdir" / "deep" / "file.txt").read_text(encoding="utf-8") == "deep content\n"


def test_add_file_with_empty_lines(tmp_path: Path):
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: blanks.txt\n"
        "+first\n"
        "+\n"
        "+last\n"
        "*** End Patch\n"
    )
    apply_patch_tool(cwd, patch)
    assert (tmp_path / "blanks.txt").read_text(encoding="utf-8") == "first\n\nlast\n"


def test_add_then_update_diff_segments_separate(tmp_path: Path):
    """Add + Update in one patch: diff must have separate ---/+++ headers."""
    (tmp_path / "existing.txt").write_text("old\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: added.txt\n"
        "+added line\n"
        "*** Update File: existing.txt\n"
        "@@ old\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )
    result = apply_patch_tool(cwd, patch)
    diff = result["details"]["diff"]
    # Both ---/+++ headers present
    assert "--- added.txt" in diff
    assert "+++ added.txt" in diff
    assert "--- existing.txt" in diff
    assert "+++ existing.txt" in diff
    # +added line\n present
    assert "+added line\n" in diff
    # Second --- on its own line (no concatenation)
    lines = diff.splitlines()
    add_idx = next(i for i, l in enumerate(lines) if l.startswith("--- added.txt"))
    existing_idx = next(i for i, l in enumerate(lines) if l.startswith("--- existing.txt"))
    # They must be on different lines with gap
    assert existing_idx > add_idx + 2, "Diff segments concatenated"


# ── Update File ───────────────────────────────────────────────────────────────


def test_update_file_single_hunk(tmp_path: Path):
    (tmp_path / "app.py").write_text("def greet():\n    print(\"Hi\")\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    # Anchor is the actual file line with indentation
    patch = (
        "*** Begin Patch\n"
        "*** Update File: app.py\n"
        "@@     print(\"Hi\")\n"
        "-    print(\"Hi\")\n"
        "+    print(\"Hello, world!\")\n"
        "*** End Patch\n"
    )
    result = apply_patch_tool(cwd, patch)
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def greet():\n    print(\"Hello, world!\")\n"
    assert "M app.py" in result["content"][0]["text"]


def test_update_file_with_context(tmp_path: Path):
    (tmp_path / "src.py").write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    # Anchor "line1" to find position, context "line1" matches, then remove "line2"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src.py\n"
        "@@ line1\n"
        " line1\n"
        "-line2\n"
        "+line2-new\n"
        "*** End Patch\n"
    )
    apply_patch_tool(cwd, patch)
    assert (tmp_path / "src.py").read_text(encoding="utf-8") == "line1\nline2-new\nline3\nline4\n"


def test_update_file_multiple_hunks(tmp_path: Path):
    (tmp_path / "code.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: code.py\n"
        "@@ a\n"
        "-a\n"
        "+A\n"
        "@@ e\n"
        "-e\n"
        "+E\n"
        "*** End Patch\n"
    )
    result = apply_patch_tool(cwd, patch)
    content = (tmp_path / "code.py").read_text(encoding="utf-8")
    assert content == "A\nb\nc\nd\nE\n"


def test_update_file_with_space_context(tmp_path: Path):
    (tmp_path / "f.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ line1\n"
        " line1\n"
        "-line2\n"
        "+line2-new\n"
        "+line2b\n"
        "*** End Patch\n"
    )
    apply_patch_tool(cwd, patch)
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "line1\nline2-new\nline2b\nline3\n"


# ── Delete File ───────────────────────────────────────────────────────────────


def test_delete_file(tmp_path: Path):
    (tmp_path / "old.txt").write_text("obsolete\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Delete File: old.txt\n*** End Patch\n"
    result = apply_patch_tool(cwd, patch)
    assert not (tmp_path / "old.txt").exists()
    assert "D old.txt" in result["content"][0]["text"]


# ── Move (Update + *** Move to) ──────────────────────────────────────────────


def test_move_file(tmp_path: Path):
    (tmp_path / "src.txt").write_text("content\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src.txt\n"
        "*** Move to: dst.txt\n"
        "@@ content\n"
        "-content\n"
        "+moved content\n"
        "*** End Patch\n"
    )
    result = apply_patch_tool(cwd, patch)
    assert not (tmp_path / "src.txt").exists()
    assert (tmp_path / "dst.txt").read_text(encoding="utf-8") == "moved content\n"
    assert "M dst.txt" in result["content"][0]["text"]


def test_move_file_creates_parent_dirs(tmp_path: Path):
    (tmp_path / "src.txt").write_text("content\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src.txt\n"
        "*** Move to: new_dir/moved.txt\n"
        "@@ content\n"
        "-content\n"
        "+new content\n"
        "*** End Patch\n"
    )
    apply_patch_tool(cwd, patch)
    assert not (tmp_path / "src.txt").exists()
    assert (tmp_path / "new_dir" / "moved.txt").read_text(encoding="utf-8") == "new content\n"


# ── Multi-file patch ─────────────────────────────────────────────────────────


def test_multifile_patch_all_ops(tmp_path: Path):
    (tmp_path / "to_delete.txt").write_text("bye\n", encoding="utf-8")
    (tmp_path / "to_update.txt").write_text("old\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: added.txt\n"
        "+added\n"
        "*** Update File: to_update.txt\n"
        "@@ old\n"
        "-old\n"
        "+new\n"
        "*** Delete File: to_delete.txt\n"
        "*** End Patch\n"
    )
    result = apply_patch_tool(cwd, patch)
    assert (tmp_path / "added.txt").read_text(encoding="utf-8") == "added\n"
    assert (tmp_path / "to_update.txt").read_text(encoding="utf-8") == "new\n"
    assert not (tmp_path / "to_delete.txt").exists()
    text = result["content"][0]["text"]
    assert "A added.txt" in text
    assert "M to_update.txt" in text
    assert "D to_delete.txt" in text


# ── Error cases ───────────────────────────────────────────────────────────────


def test_empty_patch_raises():
    with pytest.raises(ValueError, match="patch rejected: empty patch"):
        apply_patch_tool("/tmp", "*** Begin Patch\n*** End Patch\n")


def test_whitespace_only_patch_raises():
    with pytest.raises(ValueError, match="patch rejected: empty patch"):
        apply_patch_tool("/tmp", "   ")


def test_no_envelope_raises():
    with pytest.raises(ValueError, match=r"missing \*\*\* Begin Patch header"):
        apply_patch_tool("/tmp", "some patch text\n")


def test_missing_trailer_raises():
    with pytest.raises(ValueError, match=r"missing \*\*\* End Patch trailer"):
        apply_patch_tool("/tmp", "*** Begin Patch\nsome content\n")


def test_update_missing_file_raises(tmp_path: Path):
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: nonexistent.txt\n"
        "@@ hello\n"
        "-hello\n"
        "+world\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="Failed to read file to update: nonexistent.txt"):
        apply_patch_tool(cwd, patch)


def test_delete_missing_file_raises(tmp_path: Path):
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Delete File: nonexistent.txt\n*** End Patch\n"
    with pytest.raises(ValueError, match="Failed to read file to update: nonexistent.txt"):
        apply_patch_tool(cwd, patch)


def test_hunk_context_mismatch_raises(tmp_path: Path):
    (tmp_path / "f.txt").write_text("actual content\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ actual\n"
        " actual\n"
        "-wrong\n"
        "+right\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="hunk context mismatch"):
        apply_patch_tool(cwd, patch)


def test_hunk_mismatch_nothing_written(tmp_path: Path):
    """All-or-nothing: on validation failure, no files should be changed."""
    (tmp_path / "f.txt").write_text("original\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ original\n"
        "-wrong\n"
        "+right\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="hunk anchor not found"):
        apply_patch_tool(cwd, patch)
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "original\n"


def test_valid_and_invalid_section_all_or_nothing(tmp_path: Path):
    """Patch with one valid add and one invalid update should fail entirely."""
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: good.txt\n"
        "+good content\n"
        "*** Update File: missing.txt\n"
        "@@ x\n"
        "-x\n"
        "+y\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="Failed to read file to update: missing.txt"):
        apply_patch_tool(cwd, patch)
    assert not (tmp_path / "good.txt").exists()


def test_unknown_header_raises():
    with pytest.raises(ValueError, match="unknown header"):
        apply_patch_tool("/tmp", "*** Begin Patch\n*** Unknown: x\n*** End Patch\n")


def test_add_file_no_plus_lines_raises():
    """Add File with lines that don't start with '+' is an error."""
    with pytest.raises(ValueError, match="unexpected line"):
        apply_patch_tool("/tmp", "*** Begin Patch\n*** Add File: x.txt\n+content\nplain text\n*** End Patch\n")


def test_update_file_no_hunks_raises():
    with pytest.raises(ValueError, match="Update File must have hunks"):
        apply_patch_tool("/tmp", "*** Begin Patch\n*** Update File: x.txt\n*** End Patch\n")


# ── Absolute and .. paths ────────────────────────────────────────────────────


def test_absolute_path_allowed(tmp_path: Path):
    abs_file = tmp_path / "abs.txt"
    abs_file.write_text("data\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: " + str(abs_file) + "\n"
        "@@ data\n"
        "-data\n"
        "+updated\n"
        "*** End Patch\n"
    )
    result = apply_patch_tool(cwd, patch)
    assert abs_file.read_text(encoding="utf-8") == "updated\n"
    assert "M abs.txt" in result["content"][0]["text"]


def test_relative_path_resolved_against_cwd(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "f.txt").write_text("data\n", encoding="utf-8")
    cwd = _cwd(sub)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ data\n"
        "-data\n"
        "+from_cwd\n"
        "*** End Patch\n"
    )
    apply_patch_tool(cwd, patch)
    assert (sub / "f.txt").read_text(encoding="utf-8") == "from_cwd\n"


def test_dotdot_path(tmp_path: Path):
    (tmp_path / "a.txt").write_text("outer\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "f.txt").write_text("data\n", encoding="utf-8")
    cwd = _cwd(sub)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: ../a.txt\n"
        "@@ outer\n"
        "-outer\n"
        "+updated\n"
        "*** End Patch\n"
    )
    apply_patch_tool(cwd, patch)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "updated\n"


# ── Return shape ──────────────────────────────────────────────────────────────


def test_return_shape_add(tmp_path: Path):
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: x.txt\n+hi\n*** End Patch\n"
    result = apply_patch_tool(cwd, patch)
    assert isinstance(result, dict)
    assert "content" in result
    assert "details" in result
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"].startswith("Success. Updated the following files:")
    assert "A x.txt" in result["content"][0]["text"]
    assert isinstance(result["details"]["diff"], str)
    assert len(result["details"]["diff"]) > 0


def test_return_shape_update(tmp_path: Path):
    (tmp_path / "f.txt").write_text("line one\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ line one\n"
        "-line one\n"
        "+line two\n"
        "*** End Patch\n"
    )
    result = apply_patch_tool(cwd, patch)
    assert result["content"][0]["text"].startswith("Success. Updated the following files:")
    assert "M f.txt" in result["content"][0]["text"]
    assert len(result["details"]["diff"]) > 0


def test_return_shape_delete(tmp_path: Path):
    (tmp_path / "f.txt").write_text("old\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Delete File: f.txt\n*** End Patch\n"
    result = apply_patch_tool(cwd, patch)
    assert "D f.txt" in result["content"][0]["text"]
    assert isinstance(result["details"]["diff"], str)


def test_diff_is_unified_diff_format(tmp_path: Path):
    (tmp_path / "f.txt").write_text("old line\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ old line\n"
        "-old line\n"
        "+new line\n"
        "*** End Patch\n"
    )
    result = apply_patch_tool(cwd, patch)
    diff = result["details"]["diff"]
    assert "--- f.txt" in diff
    assert "+++ f.txt" in diff
    assert "-old line" in diff
    assert "+new line" in diff


# ── Line ending normalization ────────────────────────────────────────────────


def test_crlf_normalized(tmp_path: Path):
    (tmp_path / "f.txt").write_text("old\r\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\r\n*** Update File: f.txt\r\n@@ old\r\n-old\r\n+new\r\n*** End Patch\r\n"
    )
    apply_patch_tool(cwd, patch)
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new\n"


# ── Add file with trailing newline content ───────────────────────────────────


def test_add_file_content_already_has_newline(tmp_path: Path):
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: x.txt\n+hello world\n*** End Patch\n"
    apply_patch_tool(cwd, patch)
    content = (tmp_path / "x.txt").read_text(encoding="utf-8")
    assert content == "hello world\n"


# ── Update with context line that is empty ───────────────────────────────────


def test_update_with_empty_context_line(tmp_path: Path):
    (tmp_path / "f.txt").write_text("line1\n\nline3\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    # Anchor "line1", context "line1", remove empty line, add "new blank"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ line1\n"
        " line1\n"
        "-\n"
        "+new blank\n"
        "*** End Patch\n"
    )
    apply_patch_tool(cwd, patch)
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "line1\nnew blank\nline3\n"


# ── Anchor-not-found mismatch ────────────────────────────────────────────────


def test_anchor_not_found_raises(tmp_path: Path):
    (tmp_path / "f.txt").write_text("only one line\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ nonexistent\n"
        "-nonexistent\n"
        "+replacement\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="hunk anchor not found"):
        apply_patch_tool(cwd, patch)


# ── Removed-line mismatch ────────────────────────────────────────────────────


def test_removed_line_mismatch_raises(tmp_path: Path):
    """A patch whose - lines don't match the file should raise and leave the file unchanged."""
    (tmp_path / "f.txt").write_text("context\nWRONG\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ context\n"
        " context\n"
        "-wrong\n"
        "+right\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="removal mismatch"):
        apply_patch_tool(cwd, patch)
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "context\nWRONG\n"


# ── CRLF file update ─────────────────────────────────────────────────────────


def test_update_crlf_file(tmp_path: Path):
    """LF patch should apply to a CRLF file; result is written back as LF."""
    (tmp_path / "f.txt").write_bytes(b"old\r\n")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ old\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )
    result = apply_patch_tool(cwd, patch)
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new\n"
    assert "M f.txt" in result["content"][0]["text"]


# ── Collision preflight ──────────────────────────────────────────────────────


def test_add_existing_file_rejected(tmp_path: Path):
    """Add must not overwrite an existing file."""
    (tmp_path / "existing.txt").write_text("original\n", encoding="utf-8")
    original_bytes = (tmp_path / "existing.txt").read_bytes()
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: existing.txt\n+new content\n*** End Patch\n"
    with pytest.raises(ValueError, match="already exists"):
        apply_patch_tool(cwd, patch)
    assert (tmp_path / "existing.txt").read_bytes() == original_bytes


def test_add_existing_dir_rejected(tmp_path: Path):
    """Add must not overwrite an existing directory."""
    (tmp_path / "existing_dir").mkdir()
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: existing_dir\n+content\n*** End Patch\n"
    with pytest.raises(ValueError, match="already exists"):
        apply_patch_tool(cwd, patch)


def test_add_existing_symlink_rejected(tmp_path: Path):
    """Add must not overwrite an existing symlink."""
    (tmp_path / "target.txt").write_text("target\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "target.txt")
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: link.txt\n+content\n*** End Patch\n"
    with pytest.raises(ValueError, match="already exists"):
        apply_patch_tool(cwd, patch)


def test_add_dangling_symlink_rejected(tmp_path: Path):
    """Add must not overwrite a dangling symlink."""
    (tmp_path / "dangling").symlink_to(tmp_path / "nonexistent_target")
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: dangling\n+content\n*** End Patch\n"
    with pytest.raises(ValueError, match="already exists"):
        apply_patch_tool(cwd, patch)


def test_move_destination_file_rejected(tmp_path: Path):
    """Move must not overwrite an existing file."""
    (tmp_path / "src.txt").write_text("src\n", encoding="utf-8")
    (tmp_path / "dst.txt").write_text("dst\n", encoding="utf-8")
    src_bytes = (tmp_path / "src.txt").read_bytes()
    dst_bytes = (tmp_path / "dst.txt").read_bytes()
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src.txt\n"
        "*** Move to: dst.txt\n"
        "@@ src\n"
        "-src\n"
        "+moved\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="already exists"):
        apply_patch_tool(cwd, patch)
    assert (tmp_path / "src.txt").read_bytes() == src_bytes
    assert (tmp_path / "dst.txt").read_bytes() == dst_bytes


def test_move_destination_dir_rejected(tmp_path: Path):
    """Move must not overwrite an existing directory."""
    (tmp_path / "src.txt").write_text("src\n", encoding="utf-8")
    (tmp_path / "dst_dir").mkdir()
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src.txt\n"
        "*** Move to: dst_dir\n"
        "@@ src\n"
        "-src\n"
        "+moved\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="already exists"):
        apply_patch_tool(cwd, patch)


def test_move_destination_symlink_rejected(tmp_path: Path):
    """Move must not overwrite an existing symlink."""
    (tmp_path / "src.txt").write_text("src\n", encoding="utf-8")
    (tmp_path / "target.txt").write_text("target\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "target.txt")
    src_bytes = (tmp_path / "src.txt").read_bytes()
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src.txt\n"
        "*** Move to: link.txt\n"
        "@@ src\n"
        "-src\n"
        "+moved\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="already exists"):
        apply_patch_tool(cwd, patch)
    assert (tmp_path / "src.txt").read_bytes() == src_bytes


def test_self_move_rejected(tmp_path: Path):
    """Move source to the same path is rejected."""
    (tmp_path / "f.txt").write_text("data\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "*** Move to: f.txt\n"
        "@@ data\n"
        "-data\n"
        "+changed\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="self-move"):
        apply_patch_tool(cwd, patch)


def test_duplicate_sources_rejected(tmp_path: Path):
    """Same source listed in two operations is rejected."""
    (tmp_path / "f.txt").write_text("data\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ data\n"
        "-data\n"
        "+first\n"
        "*** Update File: f.txt\n"
        "@@ data\n"
        "-data\n"
        "+second\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="duplicated"):
        apply_patch_tool(cwd, patch)


def test_duplicate_targets_rejected(tmp_path: Path):
    """Same target in Add and Move is rejected."""
    (tmp_path / "src.txt").write_text("src\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: dst.txt\n"
        "+added\n"
        "*** Update File: src.txt\n"
        "*** Move to: dst.txt\n"
        "@@ src\n"
        "-src\n"
        "+moved\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="duplicated"):
        apply_patch_tool(cwd, patch)


def test_cross_role_conflict_rejected(tmp_path: Path):
    """A path used as target by one op and source by another is rejected."""
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "*** Move to: b.txt\n"
        "@@ a\n"
        "-a\n"
        "+moved\n"
        "*** Update File: b.txt\n"
        "@@ b\n"
        "-b\n"
        "+second\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="target of a previous operation"):
        apply_patch_tool(cwd, patch)


# ── Symlink safety ──────────────────────────────────────────────────────────


def test_symlink_source_rejected(tmp_path: Path):
    """Update on a symlink source is rejected."""
    (tmp_path / "target.txt").write_text("target\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "target.txt")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: link.txt\n"
        "@@ target\n"
        "-target\n"
        "+modified\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="symlink"):
        apply_patch_tool(cwd, patch)


def test_symlink_parent_rejected(tmp_path: Path):
    """Operation on a path whose parent is a symlink is rejected."""
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "f.txt").write_text("data\n", encoding="utf-8")
    symlink_dir = tmp_path / "link_dir"
    symlink_dir.symlink_to(real_dir)
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: link_dir/f.txt\n"
        "@@ data\n"
        "-data\n"
        "+modified\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="symlink"):
        apply_patch_tool(cwd, patch)


def test_delete_symlink_source_rejected(tmp_path: Path):
    """Delete on a symlink source is rejected."""
    (tmp_path / "target.txt").write_text("target\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "target.txt")
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Delete File: link.txt\n*** End Patch\n"
    with pytest.raises(ValueError, match="symlink"):
        apply_patch_tool(cwd, patch)
    assert (tmp_path / "link.txt").exists()  # symlink still there


def test_symlink_target_rejected_in_move(tmp_path: Path):
    """Move destination that is a symlink is rejected."""
    (tmp_path / "src.txt").write_text("src\n", encoding="utf-8")
    (tmp_path / "target.txt").write_text("target\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(tmp_path / "target.txt")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src.txt\n"
        "*** Move to: link.txt\n"
        "@@ src\n"
        "-src\n"
        "+moved\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="symlink"):
        apply_patch_tool(cwd, patch)


def test_parent_is_file_rejected(tmp_path: Path):
    """Add when the parent component is a file is rejected."""
    (tmp_path / "adir").write_text("not a dir\n", encoding="utf-8")
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: adir/deep/file.txt\n+content\n*** End Patch\n"
    with pytest.raises(ValueError, match="parent.*is not a directory|does not exist"):
        apply_patch_tool(cwd, patch)


# ── Hunk failure no mutation ────────────────────────────────────────────────


def test_later_hunk_failure_no_mutation(tmp_path: Path):
    """If a later hunk fails, earlier operations must be rolled back."""
    (tmp_path / "a.txt").write_text("original a\n", encoding="utf-8")
    a_bytes = (tmp_path / "a.txt").read_bytes()
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@ original\n"
        "-original\n"
        "+updated a\n"
        "*** Update File: nonexistent.txt\n"
        "@@ x\n"
        "-x\n"
        "+y\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="Failed to read file to update"):
        apply_patch_tool(cwd, patch)
    # a.txt must be untouched
    assert (tmp_path / "a.txt").read_bytes() == a_bytes


# ── Successful nested parent creation and cleanup ───────────────────────────


def test_nested_parent_creation(tmp_path: Path):
    """Add to a deeply nested path creates all intermediate dirs."""
    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: a/b/c/d.txt\n+deep\n*** End Patch\n"
    apply_patch_tool(cwd, patch)
    assert (tmp_path / "a" / "b" / "c" / "d.txt").read_text(encoding="utf-8") == "deep\n"


# ── Mode preservation for update/move ──────────────────────────────────────


def test_mode_preservation_update(tmp_path: Path):
    """Update preserves the source file mode where POSIX."""
    f = tmp_path / "mode_test.txt"
    f.write_text("original\n", encoding="utf-8")
    # Set a non-default mode
    try:
        os.chmod(str(f), 0o755)
    except OSError:
        pytest.skip("chmod not supported")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: mode_test.txt\n"
        "@@ original\n"
        "-original\n"
        "+updated\n"
        "*** End Patch\n"
    )
    apply_patch_tool(cwd, patch)
    try:
        actual_mode = stat.S_IMODE((tmp_path / "mode_test.txt").stat().st_mode)
        assert actual_mode == 0o755
    except OSError:
        pytest.skip("stat not supported")


def test_mode_preservation_move(tmp_path: Path):
    """Move preserves the source file mode where POSIX."""
    src = tmp_path / "mode_src.txt"
    src.write_text("original\n", encoding="utf-8")
    try:
        os.chmod(str(src), 0o700)
    except OSError:
        pytest.skip("chmod not supported")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: mode_src.txt\n"
        "*** Move to: mode_dst.txt\n"
        "@@ original\n"
        "-original\n"
        "+moved\n"
        "*** End Patch\n"
    )
    apply_patch_tool(cwd, patch)
    try:
        actual_mode = stat.S_IMODE((tmp_path / "mode_dst.txt").stat().st_mode)
        assert actual_mode == 0o700
    except OSError:
        pytest.skip("stat not supported")



# ── Deterministic stage failure ────────────────────────────────────────────


def test_stage_failure_leaves_originals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Patch with Update existing file + Add nested/deep/new.txt;
    first stage succeeds, second fails. Original unchanged, nested dirs removed."""
    (tmp_path / "f.txt").write_text("original\n", encoding="utf-8")
    original_bytes = (tmp_path / "f.txt").read_bytes()
    original_mode = stat.S_IMODE((tmp_path / "f.txt").stat().st_mode)
    import one.tools.apply_patch as ap
    stage_count = [0]
    original_stage = ap._stage_file

    def selective_stage(directory, content_bytes, mode=None):
        stage_count[0] += 1
        if stage_count[0] == 2:
            raise OSError("injected stage failure on second file")
        return original_stage(directory, content_bytes, mode)

    monkeypatch.setattr(ap, "_stage_file", selective_stage)
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ original\n"
        "-original\n"
        "+updated\n"
        "*** Add File: nested/deep/new.txt\n"
        "+new content\n"
        "*** End Patch\n"
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        apply_patch_tool(cwd, patch)

    # Existing file unchanged with original bytes and mode
    assert (tmp_path / "f.txt").read_bytes() == original_bytes
    actual_mode = stat.S_IMODE((tmp_path / "f.txt").stat().st_mode)
    assert actual_mode == original_mode
    # Nested dirs removed
    assert not (tmp_path / "nested").exists()
    assert not (tmp_path / "nested" / "deep").exists()
    # No artifacts
    _no_stage_backup_artifacts(tmp_path)


# ── Backup failure after prior backup ──────────────────────────────────────


def test_backup_failure_restores_prior_backups(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """If backup fails mid-transaction, previously evacuated backups are restored."""
    # Two updates — first backup succeeds, second fails
    (tmp_path / "a.txt").write_text("a original\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b original\n", encoding="utf-8")
    a_bytes = (tmp_path / "a.txt").read_bytes()
    b_bytes = (tmp_path / "b.txt").read_bytes()
    import one.tools.apply_patch as ap
    original_backup = ap._backup_file
    backup_count = [0]

    def failing_backup(source, dest_dir):
        backup_count[0] += 1
        if backup_count[0] == 2:
            raise OSError("injected backup failure")
        return original_backup(source, dest_dir)

    monkeypatch.setattr(ap, "_backup_file", failing_backup)
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@ a original\n"
        "-a original\n"
        "+a updated\n"
        "*** Update File: b.txt\n"
        "@@ b original\n"
        "-b original\n"
        "+b updated\n"
        "*** End Patch\n"
    )
    with pytest.raises(RuntimeError):
        apply_patch_tool(cwd, patch)
    # Both originals must be restored
    assert (tmp_path / "a.txt").read_bytes() == a_bytes
    assert (tmp_path / "b.txt").read_bytes() == b_bytes
    _no_stage_backup_artifacts(tmp_path)


# ── Source changed after preflight ─────────────────────────────────────────


def test_source_inode_changed_after_preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """_before_commit swaps source for a new inode via os.replace.
    Revalidation detects new inode and rejects. Source remains as replaced."""
    (tmp_path / "f.txt").write_text("original\n", encoding="utf-8")
    import one.tools.apply_patch as ap

    def inode_changer(plans):
        for p in plans:
            if p.kind == "update":
                tmp = p.src.parent / ".tmp_inode_swap"
                tmp.write_text("new inode content\n", encoding="utf-8")
                os.replace(str(tmp), str(p.src))

    monkeypatch.setattr(ap, "_before_commit", inode_changer)

    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ original\n"
        "-original\n"
        "+updated\n"
        "*** End Patch\n"
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        apply_patch_tool(cwd, patch)
    # Source has new inode from os.replace (different content)
    assert (tmp_path / "f.txt").read_bytes() == b"new inode content\n"
    _no_stage_backup_artifacts(tmp_path)


# ── Update with non-hunk payload rejects ───────────────────────────────────


def test_update_empty_hunk_payload_rejected(tmp_path: Path):
    """Update with no @@ hunks is rejected by the parser."""
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "some random text\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="@@ hunks"):
        apply_patch_tool(cwd, patch)

# ── No-stage/backup-artifacts helper ────────────────────────────────────────


def _no_stage_backup_artifacts(tmp_path: Path) -> None:
    """Assert no .stage_* or .backup_* files exist under tmp_path."""
    for root, dirs, files in os.walk(tmp_path):
        for f_name in files:
            assert not f_name.startswith(".stage_"), f"Stage artifact found: {os.path.join(root, f_name)}"
            assert not f_name.startswith(".backup_"), f"Backup artifact found: {os.path.join(root, f_name)}"


# ── Install failure on Update+Delete+Move+Add ──────────────────────────────


def test_install_failure_restores_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """If _install_file fails on the 3rd install (Add),
    Update a.txt + Delete b.txt + Move c.txt -> nested/moved.txt + Add nested/added.txt
    must all be rolled back: originals restored, targets absent, dirs cleaned.
    Note: Delete has no stage/install output, so only 3 installs total."""
    (tmp_path / "a.txt").write_text("a original\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b original\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("c original\n", encoding="utf-8")
    a_bytes = (tmp_path / "a.txt").read_bytes()
    b_bytes = (tmp_path / "b.txt").read_bytes()
    c_bytes = (tmp_path / "c.txt").read_bytes()
    a_mode_before = stat.S_IMODE((tmp_path / "a.txt").stat().st_mode)
    b_mode_before = stat.S_IMODE((tmp_path / "b.txt").stat().st_mode)
    c_mode_before = stat.S_IMODE((tmp_path / "c.txt").stat().st_mode)
    import one.tools.apply_patch as ap
    original_install = ap._install_file
    install_count = [0]

    def failing_install(stage, target):
        install_count[0] += 1
        # Fail on the 3rd install (the Add) - Delete has no stage/install
        if install_count[0] == 3:
            raise OSError("injected install failure on Add")
        return original_install(stage, target)

    monkeypatch.setattr(ap, "_install_file", failing_install)

    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.txt\n"
        "@@ a original\n"
        "-a original\n"
        "+a updated\n"
        "*** Delete File: b.txt\n"
        "*** Update File: c.txt\n"
        "*** Move to: nested/moved.txt\n"
        "@@ c original\n"
        "-c original\n"
        "+c moved\n"
        "*** Add File: nested/added.txt\n"
        "+added content\n"
        "*** End Patch\n"
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        apply_patch_tool(cwd, patch)

    # All originals restored with correct bytes AND POSIX modes
    assert (tmp_path / "a.txt").read_bytes() == a_bytes
    assert (tmp_path / "b.txt").read_bytes() == b_bytes
    assert (tmp_path / "c.txt").read_bytes() == c_bytes
    a_mode_after = stat.S_IMODE((tmp_path / "a.txt").stat().st_mode)
    b_mode_after = stat.S_IMODE((tmp_path / "b.txt").stat().st_mode)
    c_mode_after = stat.S_IMODE((tmp_path / "c.txt").stat().st_mode)
    assert a_mode_after == a_mode_before
    assert b_mode_after == b_mode_before
    assert c_mode_after == c_mode_before
    # Move destination and add target absent
    assert not (tmp_path / "nested" / "moved.txt").exists()
    assert not (tmp_path / "nested" / "added.txt").exists()
    # Newly created nested dir removed
    assert not (tmp_path / "nested").exists()
    # No stage/backup artifacts
    _no_stage_backup_artifacts(tmp_path)


# ── _before_commit source content/inode mutation ───────────────────────────


def test_before_commit_source_mutation_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """_before_commit mutates source in-place. Revalidation (5d) catches it.
    Backups (5e) are AFTER revalidation, so source can't be restored.
    Per requirements: external changed/replacement content remains."""
    (tmp_path / "f.txt").write_text("original\n", encoding="utf-8")
    import one.tools.apply_patch as ap

    monkeypatch.setattr(ap, "_before_commit", lambda plans: [p.src.write_text("tampered\n", encoding="utf-8") for p in plans if p.kind == "update"])

    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ original\n"
        "-original\n"
        "+updated\n"
        "*** End Patch\n"
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        apply_patch_tool(cwd, patch)

    # Source remains tampered - revalidation (5d) runs BEFORE backups (5e),
    # so rollback has no backup to restore from. Per requirements:
    # "external changed/replacement content remains (transaction must not overwrite concurrent change)"
    assert (tmp_path / "f.txt").read_bytes() == b"tampered\n"
    # No stage/backup artifacts created (staging may exist but should be cleaned)
    _no_stage_backup_artifacts(tmp_path)


def test_before_commit_add_target_appearance_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """If _before_commit creates the Add target (p.dst is None, target is p.src),
    revalidation (5d) catches the appearing target. Transaction rejects.
    No backups were created yet, so no artifacts."""
    # target.txt does NOT exist - it will be created by the hook during _before_commit
    import one.tools.apply_patch as ap

    monkeypatch.setattr(ap, "_before_commit", lambda plans: [p.src.write_text("appeared\n", encoding="utf-8") for p in plans if p.kind == "add"])

    cwd = _cwd(tmp_path)
    patch = "*** Begin Patch\n*** Add File: target.txt\n+will-be-rejected\n*** End Patch\n"
    with pytest.raises(RuntimeError, match="commit failed"):
        apply_patch_tool(cwd, patch)

    # Appearing target preserved with hook-created content
    assert (tmp_path / "target.txt").read_bytes() == b"appeared\n"
    # No stage/backup artifacts
    _no_stage_backup_artifacts(tmp_path)


def test_before_commit_move_target_appearance_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """If _before_commit creates the Move destination (dst.txt),
    revalidation (5d) catches the appearing target. Transaction rejects.
    dst.txt is absent initially - hook creates it during _before_commit."""
    (tmp_path / "src.txt").write_text("src\n", encoding="utf-8")
    # dst.txt does NOT exist - it will be created by the hook during _before_commit
    import one.tools.apply_patch as ap

    monkeypatch.setattr(ap, "_before_commit", lambda plans: [p.dst.write_text("appeared\n", encoding="utf-8") for p in plans if p.kind == "update" and p.dst is not None and p.src != p.dst])

    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src.txt\n"
        "*** Move to: dst.txt\n"
        "@@ src\n"
        "-src\n"
        "+moved\n"
        "*** End Patch\n"
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        apply_patch_tool(cwd, patch)

    # Appearing destination preserved with hook-created content
    assert (tmp_path / "dst.txt").read_bytes() == b"appeared\n"
    # Source unchanged (no backup created - revalidation failed first)
    assert (tmp_path / "src.txt").read_bytes() == b"src\n"
    # No artifacts
    _no_stage_backup_artifacts(tmp_path)


# ── Rollback interference ──────────────────────────────────────────────────


def test_rollback_interference_occupant_not_deleted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """After backing up a source, if _install_file creates an occupant at the original
    path and raises, the occupant must NOT be deleted and error must report retention."""
    (tmp_path / "f.txt").write_text("original\n", encoding="utf-8")
    original_bytes = (tmp_path / "f.txt").read_bytes()
    import one.tools.apply_patch as ap
    original_install = ap._install_file

    install_count = [0]

    def interfering_install(stage, target):
        install_count[0] += 1
        # On first install (update), create occupant at original path and raise
        if install_count[0] == 1:
            (target).parent.joinpath("f.txt").write_text("occupant\n", encoding="utf-8")
            # Actually we need to create occupant at the ORIGINAL source path
            # Since this is an Update, target == src. Let's overwrite it.
            raise OSError("injected install failure")
        return original_install(stage, target)

    monkeypatch.setattr(ap, "_install_file", interfering_install)

    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@ original\n"
        "-original\n"
        "+updated\n"
        "*** End Patch\n"
    )
    with pytest.raises(RuntimeError) as exc_info:
        apply_patch_tool(cwd, patch)

    err_msg = str(exc_info.value)
    assert "rollback interference" in err_msg
    assert "retained backups" in err_msg

    # Occupant NOT overwritten/deleted
    assert (tmp_path / "f.txt").read_bytes() == b"occupant\n"

    # Backup exists for recovery
    backup_found = False
    for f in tmp_path.iterdir():
        if f.name.startswith(".backup_"):
            backup_found = True
            assert f.read_bytes() == original_bytes
    assert backup_found, "No retained backup found"


# ── Direct _stage_file tests ──────────────────────────────────────────────


def test_stage_file_partial_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Monkeypatch os.write to write a bounded prefix through real os.write;
    assert the helper loop is called multiple times, full bytes correct."""
    import one.tools.apply_patch as ap
    original_write = os.write
    call_count = [0]

    def limited_write(fd, data):
        call_count[0] += 1
        chunk = data[:5]
        return original_write(fd, chunk)

    monkeypatch.setattr(os, "write", limited_write)
    result_path = ap._stage_file(tmp_path, b"hello world\n")
    # assert helper called multiple times (11 bytes, 5 per call = 3 calls)
    assert call_count[0] >= 2, f"Expected multiple os.write calls, got {call_count[0]}"
    assert result_path.read_bytes() == b"hello world\n"
    # Unlink returned stage and assert no artifacts
    result_path.unlink(missing_ok=True)
    _no_stage_backup_artifacts(tmp_path)


def test_stage_file_zero_progress_cleans_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """os.write returning 0 raises; no .stage_ artifact remains."""
    import one.tools.apply_patch as ap
    monkeypatch.setattr(os, "write", lambda fd, data: 0)  # zero progress
    with pytest.raises(OSError, match="zero progress"):
        ap._stage_file(tmp_path, b"should not be written")
    # No stage artifacts
    for f in tmp_path.iterdir():
        assert not f.name.startswith(".stage_"), f"Stage artifact left: {f}"


def test_stage_file_chmod_failure_cleans_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """If chmod fails after write, stage is cleaned up."""
    import one.tools.apply_patch as ap
    monkeypatch.setattr(os, "chmod", lambda *a, **k: (_ for _ in ()).throw(OSError("chmod denied")))
    with pytest.raises(OSError, match="chmod denied"):
        ap._stage_file(tmp_path, b"content", mode=0o700)
    # No stage artifacts
    for f in tmp_path.iterdir():
        assert not f.name.startswith(".stage_"), f"Stage artifact left: {f}"


# ── Strict collision tests ─────────────────────────────────────────────────


def test_cross_role_add_then_delete_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Add then Delete same path is a cross-role conflict."""
    import one.tools.apply_patch as ap

    monkeypatch.setattr(ap, "_stage_file", lambda d, b, m=None: (_ for _ in ()).throw(OSError("block")))

    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: f.txt\n"
        "+added\n"
        "*** Delete File: f.txt\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="source of|target of"):
        apply_patch_tool(cwd, patch)


def test_duplicate_deletes_rejected(tmp_path: Path):
    """Same source listed in two Delete operations is rejected."""
    with pytest.raises(ValueError, match="duplicated"):
        apply_patch_tool(_cwd(tmp_path), (
            "*** Begin Patch\n"
            "*** Delete File: x.txt\n"
            "*** Delete File: x.txt\n"
            "*** End Patch\n"
        ))


def test_duplicate_adds_rejected(tmp_path: Path):
    """Same target in two Add operations is rejected."""
    with pytest.raises(ValueError, match="duplicated"):
        apply_patch_tool(_cwd(tmp_path), (
            "*** Begin Patch\n"
            "*** Add File: x.txt\n"
            "+first\n"
            "*** Add File: x.txt\n"
            "+second\n"
            "*** End Patch\n"
        ))


def test_normalized_alias_targets_rejected(tmp_path: Path):
    """Normalized alias: Add to dir/x.txt and Add to dir/../dir/x.txt are the same key."""
    subdir = tmp_path / "dir"
    subdir.mkdir()
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: dir/x.txt\n"
        "+first\n"
        "*** Add File: dir/../dir/x.txt\n"
        "+second\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="duplicated"):
        apply_patch_tool(cwd, patch)


def test_hardlink_source_aliases_rejected(tmp_path: Path):
    """Distinct source paths sharing same inode (hardlinks) are rejected."""
    (tmp_path / "orig.txt").write_text("data\n", encoding="utf-8")
    try:
        os.link(str(tmp_path / "orig.txt"), str(tmp_path / "link.txt"))
    except OSError as exc:
        pytest.skip(f"os.link not supported: {exc}")
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: orig.txt\n"
        "@@ data\n"
        "-data\n"
        "+first\n"
        "*** Update File: link.txt\n"
        "@@ data\n"
        "-data\n"
        "+second\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="hardlink alias"):
        apply_patch_tool(cwd, patch)
    # No mutation
    assert (tmp_path / "orig.txt").read_bytes() == b"data\n"


# ── Nested symlink ancestor tests ─────────────────────────────────────────


def test_add_nested_symlink_ancestor_rejected(tmp_path: Path):
    """Add when an ancestor component is a symlink is rejected."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "target.txt").write_text("target\n", encoding="utf-8")
    symlink_ancestor = tmp_path / "link_ancestor"
    symlink_ancestor.symlink_to(real_dir)
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Add File: link_ancestor/deep/file.txt\n"
        "+content\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="symlink"):
        apply_patch_tool(cwd, patch)
    # referent untouched
    assert (real_dir / "target.txt").read_bytes() == b"target\n"


def test_delete_nested_symlink_ancestor_rejected(tmp_path: Path):
    """Delete when an ancestor component is a symlink is rejected."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "target.txt").write_text("target\n", encoding="utf-8")
    symlink_ancestor = tmp_path / "link_ancestor"
    symlink_ancestor.symlink_to(real_dir)
    cwd = _cwd(tmp_path)
    patch = (
        "*** Begin Patch\n"
        "*** Delete File: link_ancestor/target.txt\n"
        "*** End Patch\n"
    )
    with pytest.raises(ValueError, match="symlink"):
        apply_patch_tool(cwd, patch)
    # referent untouched
    assert (real_dir / "target.txt").read_bytes() == b"target\n"
