from __future__ import annotations

from pathlib import Path

import pytest

from one.tools.apply_patch import apply_patch_tool


def _cwd(tmp_path: Path) -> str:
    return str(tmp_path)


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
