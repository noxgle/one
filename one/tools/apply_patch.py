from __future__ import annotations

import difflib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Envelope / section headers
# ---------------------------------------------------------------------------

_ENVELOPE_START = "*** Begin Patch"
_ENVELOPE_END = "*** End Patch"

_ADD = "*** Add File:"
_DELETE = "*** Delete File:"
_UPDATE = "*** Update File:"
_MOVE = "*** Move to:"

# ---------------------------------------------------------------------------
# Hunk parser (preserved from original - preserves @@ syntax and .. paths)
# ---------------------------------------------------------------------------


def _parse_hunks(raw_lines: list[str]) -> list[tuple[str, list[str], list[str], list[str]]]:
    """Parse unified-diff hunks from raw section lines.

    Returns a list of (hunk_header_str, removed_lines, added_lines, context_lines) per hunk.
    """
    hunks: list[tuple[str, list[str], list[str], list[str]]] = []
    current_header: str | None = None
    current_removed: list[str] = []
    current_added: list[str] = []
    current_context: list[str] = []

    def _flush() -> None:
        nonlocal current_header, current_removed, current_added, current_context
        if current_header is not None:
            hunks.append((current_header, current_removed, current_added, current_context))
        current_header = None
        current_removed = []
        current_added = []
        current_context = []

    for line in raw_lines:
        if line.startswith("@@"):
            _flush()
            current_header = line
            continue
        if not line:
            current_context.append("")
            continue
        prefix = line[0] if line else ""
        if prefix == "-":
            current_removed.append(line[1:])
        elif prefix == "+":
            current_added.append(line[1:])
        elif prefix == " ":
            current_context.append(line[1:])
        else:
            current_context.append(line)

    _flush()
    return hunks


def _validate_hunks(
    hunks: list[tuple[str, list[str], list[str], list[str]]],
    file_content: str,
) -> str:
    """Apply hunks to file_content with exact context matching.

    Returns the new content string, or raises ValueError on mismatch.
    """
    lines = file_content.split("\n")
    out: list[str] = []
    pos = 0

    for header, removed, added, context in hunks:
        if not context and removed:
            anchor = removed[0]
            found = False
            for j in range(pos, len(lines)):
                if lines[j] == anchor:
                    for k in range(pos, j):
                        out.append(lines[k])
                    pos = j
                    found = True
                    break
            if not found:
                raise ValueError(
                    "apply_patch verification failed: hunk anchor not found in file"
                )

        for ctx_line in context:
            if pos >= len(lines):
                raise ValueError("apply_patch verification failed: hunk context exhausted too early")
            if lines[pos] != ctx_line:
                raise ValueError("apply_patch verification failed: hunk context mismatch")
            out.append(lines[pos])
            pos += 1

        for removed_line in removed:
            if pos >= len(lines):
                raise ValueError("apply_patch verification failed: hunk removal ran off end")
            if lines[pos] != removed_line:
                raise ValueError(
                    "apply_patch verification failed: hunk removal mismatch"
                )
            pos += 1

        out.extend(added)

    out.extend(lines[pos:])
    return "\n".join(out)

# ---------------------------------------------------------------------------
# Path helpers - lexical resolution WITHOUT following symlinks
# ---------------------------------------------------------------------------


def _resolve_lexical(path_str: str, cwd: str) -> Path:
    """Resolve a path string to a normalized absolute path relative to cwd.

    Collapses ``..`` and ``.`` segments without following symlinks.  Symlink
    safety is enforced at preflight time via lstat-based checks.
    """
    p = Path(path_str)
    if p.is_absolute():
        return _normalise_absolute(p)
    return _normalise_absolute(Path(cwd) / p)


def _normalise_absolute(p: Path) -> Path:
    """Collapse ``..`` and ``.`` segments without following symlinks."""
    parts: list[str] = []
    for part in p.parts:
        if part == "" or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    result = Path(*parts) if parts else Path(".")
    if p.is_absolute():
        result = Path("/") / result
    return result


def _rel_path(p: Path, cwd: Path) -> str:
    """Return a POSIX-style relative path from cwd to p."""
    try:
        rel = p.relative_to(cwd)
    except ValueError:
        return str(p).replace("\\", "/")
    return str(rel).replace("\\", "/")


def _norm_key(p: Path) -> str:
    """Normalize a path for collision-key comparison."""
    return os.path.normcase(str(p))


# ---------------------------------------------------------------------------
# _stat_safe - only FileNotFoundError means "missing"
# ---------------------------------------------------------------------------


def _stat_safe(p: Path) -> os.stat_result | None:
    """Return ``lstat()`` result or ``None`` only for FileNotFoundError.

    Any other OSError (PermissionError, NotADirectoryError, etc.) is propagated
    to the caller - we do NOT treat them as "file missing".
    """
    try:
        return p.lstat()
    except FileNotFoundError:
        return None

# ---------------------------------------------------------------------------
# Full-component symlink/parent preflight walker
# ---------------------------------------------------------------------------


def _walk_components(path: Path, cwd_path: Path) -> list[tuple[Path, os.stat_result]]:
    """Walk every EXISTING component of *path* (leaf ... up toward *cwd_path*).

    Returns a list of (component, lstat_result) for each component that exists.
    Non-existent components (FileNotFoundError) are skipped; other OSErrors are
    propagated.
    """
    parts: list[Path] = []
    cur = path
    while cur != cwd_path and cur != cur.parent:
        parts.append(cur)
        cur = cur.parent
    result: list[tuple[Path, os.stat_result]] = []
    for comp in reversed(parts):
        try:
            st = comp.lstat()
        except FileNotFoundError:
            # Component doesn't exist - skip it but continue with ancestors
            continue
        result.append((comp, st))
    return result


def _check_no_symlink_or_non_dir(path: Path, cwd_path: Path, *, check_leaf: bool = True) -> None:
    """Raise ValueError if any existing component of *path* is a symlink or a
    non-directory.

    For Add/Move targets, *check_leaf* is True (the leaf itself must not be
    a symlink or non-directory - if it doesn't exist, it passes).
    For Update/Delete sources, *check_leaf* is True (the source must be a
    regular file, checked separately).
    """
    comps = list(_walk_components(path, cwd_path))
    for comp, st in comps:
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(
                f"apply_patch rejected: component of path is a symlink ({comp})"
            )
        # If this is an ancestor (not the leaf) and it's not a directory, reject
        if comp != path and not stat.S_ISDIR(st.st_mode):
            raise ValueError(
                f"apply_patch rejected: parent component of path is not a directory ({comp})"
            )


# ---------------------------------------------------------------------------
# Internal phase hooks - narrow entry points for tests to inject failures
# ---------------------------------------------------------------------------


def _before_commit(plans: list[_OpPlan]) -> None:
    """Narrow no-op hook called after staging and immediately before backups.

    Tests can monkey-patch this function to inject pre-commit failures.
    """
    pass


def _stage_file(directory: Path, content_bytes: bytes, mode: int | None = None) -> Path:
    """Write *content_bytes* to a unique temp file inside *directory*.

    Uses a complete os.write loop.  Flushes via fsync.  Cleans up on error.
    chmod is inside the cleanup-protected try so failure triggers cleanup.
    """
    fd, tmp_path_str = tempfile.mkstemp(dir=str(directory), prefix=".stage_", suffix=".patch")
    tmp_path = Path(tmp_path_str)
    _closed = False

    def _close_fd() -> None:
        nonlocal _closed
        if not _closed:
            _closed = True
            try:
                os.close(fd)
            except OSError:
                pass

    try:
        written = 0
        while written < len(content_bytes):
            try:
                n = os.write(fd, content_bytes[written:])
            except OSError:
                raise
            if n == 0:
                raise OSError("_stage_file: os.write returned 0, zero progress")
            written += n

        os.fsync(fd)
        _close_fd()
        fd = -1  # sentinel: no further close

        if mode is not None:
            os.chmod(str(tmp_path), mode)

        return tmp_path

    except BaseException:
        _close_fd()
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

def _backup_file(source: Path, dest_dir: Path) -> Path:
    """Evacuate *source* to a unique same-directory backup via atomic replace.

    Raises ValueError if the source no longer exists.
    """
    if _stat_safe(source) is None:
        raise ValueError(
            f"apply_patch rollback error: source {source} missing during backup phase"
        )
    fd, tmp_path_str = tempfile.mkstemp(dir=str(dest_dir), prefix=".backup_", suffix=".patch")
    tmp_path = Path(tmp_path_str)
    try:
        os.close(fd)
        fd = None
        os.replace(str(source), str(tmp_path))
        return tmp_path
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _install_file(stage: Path, target: Path) -> None:
    """Install a staged file onto its target via os.replace."""
    os.replace(str(stage), str(target))


# ---------------------------------------------------------------------------
# Plan data structure
# ---------------------------------------------------------------------------


class _SourceSnapshot:
    """Preserved snapshot of a source file before mutation."""
    __slots__ = ("bytes", "mode", "st_dev", "st_ino", "st_size", "st_mtime_ns")

    def __init__(self, bytes_data: bytes, mode: int, dev: int, ino: int,
                 size: int, mtime_ns: int):
        self.bytes = bytes_data
        self.mode = mode
        self.st_dev = dev
        self.st_ino = ino
        self.st_size = size
        self.st_mtime_ns = mtime_ns


class _OpPlan:
    """Holds the computed plan for a single section."""
    __slots__ = (
        "kind", "path_str", "src", "dst", "final_bytes",
        "src_mode", "src_stat", "src_snapshot", "content_lines",
        "_stage_path",
    )

    def __init__(self, kind: str, path_str: str, src: Path, dst: Path | None,
                 final_bytes: bytes, src_mode: int | None,
                 src_stat: os.stat_result | None,
                 src_snapshot: _SourceSnapshot | None,
                 content_lines: list[str] | None = None):
        self.kind = kind
        self.path_str = path_str
        self.src = src
        self.dst = dst
        self.final_bytes = final_bytes
        self.src_mode = src_mode
        self.src_stat = src_stat
        self.src_snapshot = src_snapshot
        self.content_lines = content_lines
        self._stage_path: Path | None = None

# ---------------------------------------------------------------------------
# Helper: create missing parent directories
# ---------------------------------------------------------------------------


def _create_parents(dir_path: Path, cwd_path: Path, created_dirs: list[Path]) -> None:
    """Create missing ancestor directories for *dir_path*, recording each.

    Uses _stat_safe to avoid following symlinks.  Creates only truly missing
    dirs shallow-first; race-created symlink/non-dir after creation is rejected.
    """
    parts: list[Path] = []
    cur = dir_path
    while cur != cwd_path and cur != cur.parent:
        parts.append(cur)
        cur = cur.parent
    for part in reversed(parts):
        st = _stat_safe(part)
        if st is not None:
            # Already exists - validate it is a real directory
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(
                    f"apply_patch rejected: parent component of {dir_path} is a symlink ({part})"
                )
            if not stat.S_ISDIR(st.st_mode):
                raise ValueError(
                    f"apply_patch rejected: parent component of {dir_path} is not a directory ({part})"
                )
            continue
        # Missing - create it (shallow, one level at a time)
        part.mkdir(parents=False, exist_ok=False)
        created_dirs.append(part)

# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------


def apply_patch_tool(cwd: str, patchText: str) -> dict[str, Any]:
    """Apply a unified-diff patch envelope to files.

    **Guarantees:**
    - Validates and stages all operations before any mutation.
    - Add/Move targets must not exist; symlinks are rejected; hardlink
      aliases and duplicate operations cause rejection.
    - All final outputs are computed before any write.  Outputs are staged
      to same-directory temp files; originals are evacuated to backups;
      staged outputs are installed via ``os.replace``.
    - On caught in-process commit failure, transactional rollback attempts
      to restore originals from backups.  Kill/crash/power-loss, rollback
      I/O failure, or concurrent hostile modification can leave backups.
    - Add/Update/Delete/Move with absolute and ``..`` paths are supported.
      Cross-filesystem Move uses copy+delete (not inode-preserving).
    """
    # - Parse -

    text = patchText.replace("\r\n", "\n").replace("\r", "\n").strip()

    if text == f"{_ENVELOPE_START}\n{_ENVELOPE_END}":
        raise ValueError("patch rejected: empty patch")
    if text.strip() == "":
        raise ValueError("patch rejected: empty patch")
    if not text.startswith(_ENVELOPE_START + "\n"):
        raise ValueError("apply_patch verification failed: missing *** Begin Patch header")
    if not text.endswith("\n" + _ENVELOPE_END):
        raise ValueError("apply_patch verification failed: missing *** End Patch trailer")

    inner = text[len(_ENVELOPE_START) + 1:-len(_ENVELOPE_END) - 1]
    all_lines = inner.split("\n")

    sections: list[tuple[str, str, str | None, Any]] = []
    i = 0
    while i < len(all_lines):
        line = all_lines[i]
        if line.startswith(_ADD + " "):
            path = line[len(_ADD):].strip()
            if not path:
                raise ValueError("apply_patch verification failed: Add File requires a path")
            content_lines: list[str] = []
            i += 1
            while i < len(all_lines):
                cline = all_lines[i]
                if cline.startswith(_DELETE + " ") or cline.startswith(_UPDATE + " ") or cline.startswith(_ADD + " ") or cline.startswith(_ENVELOPE_END):
                    break
                if cline.startswith("+"):
                    content_lines.append(cline[1:])
                elif cline == "":
                    content_lines.append("")
                else:
                    raise ValueError(
                        f"apply_patch verification failed: unexpected line in Add File section: {cline!r}"
                    )
                i += 1
            sections.append(("add", path, None, content_lines))
        elif line.startswith(_DELETE + " "):
            path = line[len(_DELETE):].strip()
            if not path:
                raise ValueError("apply_patch verification failed: Delete File requires a path")
            sections.append(("delete", path, None, None))
            i += 1
        elif line.startswith(_UPDATE + " "):
            path = line[len(_UPDATE):].strip()
            if not path:
                raise ValueError("apply_patch verification failed: Update File requires a path")
            dest: str | None = None
            i += 1
            if i < len(all_lines) and all_lines[i].startswith(_MOVE + " "):
                dest = all_lines[i][len(_MOVE):].strip()
                if not dest:
                    raise ValueError("apply_patch verification failed: Move to requires a path")
                i += 1
            raw_hunk: list[str] = []
            while i < len(all_lines):
                cline = all_lines[i]
                if cline.startswith(_DELETE + " ") or cline.startswith(_UPDATE + " ") or cline.startswith(_ADD + " ") or cline.startswith(_ENVELOPE_END):
                    break
                raw_hunk.append(cline)
                i += 1
            if not raw_hunk:
                raise ValueError("apply_patch verification failed: Update File must have hunks")
            hunks = _parse_hunks(raw_hunk)
            if not hunks:
                raise ValueError("apply_patch verification failed: Update File must have @@ hunks")
            sections.append(("update", path, dest, hunks))
        elif line.startswith(_ENVELOPE_END):
            i += 1
        else:
            raise ValueError(f"apply_patch verification failed: unknown header: {line!r}")

    if not sections:
        raise ValueError("apply_patch verification failed: no hunks found")

    cwd_path = Path(cwd).resolve()

    # - Phase 1: resolve every path lexically -

    resolved: list[tuple[str, Path, Path | None]] = []
    for _idx, (kind, path, dest, _payload) in enumerate(sections):
        src = _resolve_lexical(path, cwd)
        dst: Path | None = None
        if dest is not None:
            dst = _resolve_lexical(dest, cwd)
        resolved.append((kind, src, dst))

    # - Phase 2: strict collision preflight -

    seen_source_keys: dict[str, str] = {}
    seen_target_keys: dict[str, str] = {}
    seen_hardlink_ids: dict[tuple[int, int], str] = {}

    for idx, (kind, src, dst) in enumerate(resolved):
        label = f"{kind}:{idx}"
        src_key = _norm_key(src)

        if kind in ("update", "delete"):
            # Check duplicate source FIRST (before hardlink alias check)
            if src_key in seen_source_keys:
                raise ValueError(
                    f"apply_patch rejected: {kind} source {src} is duplicated "
                    f"(also listed in {seen_source_keys[src_key]})"
                )
            seen_source_keys[src_key] = label

            # Check hardlink alias (distinct source paths sharing same inode)
            src_st = _stat_safe(src)
            if src_st is not None:
                hlink_id = (src_st.st_dev, src_st.st_ino)
                if hlink_id in seen_hardlink_ids:
                    raise ValueError(
                        f"apply_patch rejected: {kind} source {src} is a hardlink alias "
                        f"of {seen_hardlink_ids[hlink_id]}"
                    )
                seen_hardlink_ids[hlink_id] = label

            # Check source<->target intersection (source used as prior target)
            if src_key in seen_target_keys:
                raise ValueError(
                    f"apply_patch rejected: {src} is a target of a previous operation "
                    f"and a source of {label}"
                )

            # Move destination (external target)
            if dst is not None:
                dst_key = _norm_key(dst)
                # Self-move (same normkey) always rejects
                if dst_key == src_key:
                    raise ValueError(f"apply_patch rejected: self-move from {src} to {dst}")
                # Duplicate target
                if dst_key in seen_target_keys:
                    raise ValueError(
                        f"apply_patch rejected: target {dst} is duplicated "
                        f"(also a target in {seen_target_keys[dst_key]})"
                    )
                # Cross-role: target is a source of a previous op
                if dst_key in seen_source_keys:
                    raise ValueError(
                        f"apply_patch rejected: {dst} is a source of a previous operation "
                        f"and a target of {label}"
                    )
                seen_target_keys[dst_key] = label

        if kind == "add":
            add_target_key = src_key
            # Duplicate target
            if add_target_key in seen_target_keys:
                raise ValueError(
                    f"apply_patch rejected: target {src} is duplicated "
                    f"(also a target in {seen_target_keys[add_target_key]})"
                )
            # Cross-role: target is a source of a previous op
            if add_target_key in seen_source_keys:
                raise ValueError(
                    f"apply_patch rejected: {src} is a source of a previous operation "
                    f"and a target of {label}"
                )
            seen_target_keys[add_target_key] = label

    # - Phase 3: full-component preflight -

    for idx, (kind, src, dst) in enumerate(resolved):
        path_str = sections[idx][1]

        if kind in ("update", "delete"):
            src_st = _stat_safe(src)
            if src_st is None:
                raise ValueError(
                    f"apply_patch verification failed: Failed to read file to update: {path_str}"
                )
            if stat.S_ISLNK(src_st.st_mode):
                raise ValueError(f"apply_patch rejected: {kind} source is a symlink ({src})")
            if not stat.S_ISREG(src_st.st_mode):
                raise ValueError(f"apply_patch rejected: {kind} source is not a regular file ({src})")
            _check_no_symlink_or_non_dir(src, cwd_path)

        if kind == "add":
            add_target = src
            try:
                dst_st = _stat_safe(add_target)
            except NotADirectoryError:
                raise ValueError(
                    f"apply_patch rejected: parent component of {add_target} is not a directory"
                )
            except OSError as exc:
                raise ValueError(
                    f"apply_patch rejected: cannot stat add target {add_target}: {exc}"
                )
            if dst_st is not None:
                raise ValueError(
                    f"apply_patch rejected: {kind} target {add_target} already exists (cannot overwrite)"
                )
            try:
                _check_no_symlink_or_non_dir(add_target, cwd_path)
            except NotADirectoryError:
                raise ValueError(
                    f"apply_patch rejected: parent component of {add_target} is not a directory"
                )

        if kind == "update" and dst is not None:
            try:
                dst_st = _stat_safe(dst)
            except NotADirectoryError:
                raise ValueError(
                    f"apply_patch rejected: parent component of {dst} is not a directory"
                )
            except OSError as exc:
                raise ValueError(
                    f"apply_patch rejected: cannot stat move destination {dst}: {exc}"
                )
            if dst_st is not None:
                raise ValueError(
                    f"apply_patch rejected: {kind} destination {dst} already exists"
                )
            try:
                _check_no_symlink_or_non_dir(dst, cwd_path)
            except NotADirectoryError:
                raise ValueError(
                    f"apply_patch rejected: parent component of {dst} is not a directory"
                )

    # - Phase 4: read sources, compute all final content -

    old_contents: dict[int, str] = {}
    plans: list[_OpPlan] = []

    for idx, (kind, src, dst) in enumerate(resolved):
        path_str = sections[idx][1]

        if kind == "add":
            content_lines = sections[idx][3]
            add_target = src
            content = "\n".join(content_lines)
            if not content.endswith("\n"):
                content += "\n"
            plans.append(_OpPlan(kind, path_str, src, add_target,
                                 content.encode("utf-8"), None, None, None, content_lines))

        elif kind == "delete":
            # Read source exactly once: lstat-before, read_bytes, lstat-after
            src_st_before = src.lstat()
            src_bytes = src.read_bytes()
            src_st_after = src.lstat()
            # Reject if identity/size/mtime changed during snapshot read
            if (src_st_before.st_ino != src_st_after.st_ino or
                    src_st_before.st_dev != src_st_after.st_dev or
                    src_st_before.st_size != src_st_after.st_size or
                    src_st_before.st_mtime_ns != src_st_after.st_mtime_ns):
                raise ValueError(
                    f"apply_patch verification failed: source {src} changed during snapshot"
                )
            src_mode = stat.S_IMODE(src_st_before.st_mode)
            snapshot = _SourceSnapshot(
                src_bytes, src_mode, src_st_before.st_dev, src_st_before.st_ino,
                src_st_before.st_size, src_st_before.st_mtime_ns,
            )
            plans.append(_OpPlan(kind, path_str, src, None,
                                 b"", None, src_st_before, snapshot))

        elif kind == "update":
            # Read source exactly once: lstat-before, read_bytes, lstat-after
            src_st_before = src.lstat()
            raw_bytes = src.read_bytes()
            src_st_after = src.lstat()
            # Reject if identity/size/mtime changed during snapshot read
            if (src_st_before.st_ino != src_st_after.st_ino or
                    src_st_before.st_dev != src_st_after.st_dev or
                    src_st_before.st_size != src_st_after.st_size or
                    src_st_before.st_mtime_ns != src_st_after.st_mtime_ns):
                raise ValueError(
                    f"apply_patch verification failed: source {src} changed during snapshot"
                )
            # Decode from captured raw bytes (no second read)
            file_content = raw_bytes.decode("utf-8", errors="ignore")
            file_content = file_content.replace("\r\n", "\n").replace("\r", "\n")
            old_contents[idx] = file_content
            hunks = sections[idx][3]
            new_content = _validate_hunks(hunks, file_content)
            src_mode = stat.S_IMODE(src_st_before.st_mode)
            # Snapshot stores RAW bytes for post-hook revalidation
            snapshot = _SourceSnapshot(
                raw_bytes, src_mode, src_st_before.st_dev, src_st_before.st_ino,
                src_st_before.st_size, src_st_before.st_mtime_ns,
            )
            plans.append(_OpPlan(kind, path_str, src, dst,
                                 new_content.encode("utf-8"), src_mode, src_st_before, snapshot))

    # - Phase 5: transaction commit (single try/except block) -

    created_dirs: list[Path] = []
    stages: list[Path] = []
    backups: list[tuple[Path, Path]] = []
    installed: list[tuple[Path, int, int]] = []

    def _rollback_and_report(rollback_errors: list[str]) -> list[str]:
        """Perform transactional rollback. Returns list of retained backup paths."""
        retained: list[str] = []

        # 1. Remove installed outputs: absent=okay, matching=unlink, diff=interference
        for target, dev, ino in reversed(installed):
            try:
                current_st = _stat_safe(target)
                if current_st is None:
                    # Already absent - fine
                    pass
                elif current_st.st_ino == ino and current_st.st_dev == dev:
                    target.unlink(missing_ok=True)
                else:
                    rollback_errors.append(
                        f"rollback interference: {target} changed identity, not unlinked"
                    )
            except OSError as exc:
                rollback_errors.append(f"rollback error removing installed {target}: {exc}")

        # 2. Restore backups in reverse: only when original path absent
        for bp, orig in reversed(backups):
            try:
                orig_st = _stat_safe(orig)
                if orig_st is None:
                    os.replace(str(bp), str(orig))
                else:
                    # Any occupant (including dangling symlink) - do not overwrite
                    rollback_errors.append(
                        f"rollback interference: {orig} present, backup {bp} retained"
                    )
                    retained.append(str(bp))
            except OSError as exc:
                rollback_errors.append(f"rollback error restoring {orig}: {exc}")
                retained.append(str(bp))

        # 3. Clean remaining stages
        for s in stages:
            try:
                if _stat_safe(s) is not None:
                    s.unlink(missing_ok=True)
            except OSError as exc:
                rollback_errors.append(f"rollback error removing stage {s}: {exc}")

        # 4. Clean newly created empty dirs
        for d in reversed(created_dirs):
            try:
                if _stat_safe(d) is not None:
                    d.rmdir()
            except OSError as exc:
                rollback_errors.append(f"rollback error removing dir {d}: {exc}")

        return retained

    try:
        # 5a. Create parent directories
        for plan in plans:
            if plan.kind == "add":
                target = plan.dst if plan.dst is not None else plan.src
                _create_parents(target.parent, cwd_path, created_dirs)
            elif plan.kind == "update" and plan.dst is not None:
                _create_parents(plan.dst.parent, cwd_path, created_dirs)

        # 5b. Stage all outputs
        for plan in plans:
            if plan.kind == "delete":
                continue
            if plan.kind == "add":
                target = plan.dst if plan.dst is not None else plan.src
                staging_dir = target.parent
            elif plan.kind == "update" and plan.dst is not None:
                staging_dir = plan.dst.parent
            elif plan.kind == "update" and plan.dst is None:
                staging_dir = plan.src.parent
            else:
                continue

            mode = plan.src_mode if plan.kind == "update" else 0o644
            stage_path = _stage_file(staging_dir, plan.final_bytes, mode)
            stages.append(stage_path)
            plan._stage_path = stage_path

        # 5c. Invoke _before_commit hook (test monkeypatch point)
        _before_commit(plans)

        # 5d. Authoritative revalidation - after staging + _before_commit, before backups
        for plan in plans:
            if plan.kind in ("update", "delete"):
                # Full component safety check
                _check_no_symlink_or_non_dir(plan.src, cwd_path)
                st = _stat_safe(plan.src)
                if st is None or stat.S_ISLNK(st.st_mode):
                    raise ValueError(
                        f"apply_patch commit failed: source {plan.src} changed"
                    )
                # Compare ALL fingerprint fields AND bytes
                snap = plan.src_snapshot
                if snap is not None:
                    if (st.st_dev != snap.st_dev or
                            st.st_ino != snap.st_ino or
                            st.st_size != snap.st_size or
                            st.st_mtime_ns != snap.st_mtime_ns):
                        raise ValueError(
                            f"apply_patch commit failed: source {plan.src} changed after staging"
                        )
                    current_bytes = plan.src.read_bytes()
                    if current_bytes != snap.bytes:
                        raise ValueError(
                            f"apply_patch commit failed: source {plan.src} content changed"
                        )

            # Check every external target (Add target + Move destination)
            if plan.kind == "add":
                target = plan.dst if plan.dst is not None else plan.src
                _check_no_symlink_or_non_dir(target, cwd_path)
                if _stat_safe(target) is not None:
                    raise ValueError(
                        f"apply_patch commit failed: {plan.kind} target {target} appeared"
                    )
            elif plan.kind == "update" and plan.dst is not None:
                target = plan.dst
                # Skip self-move (dst == src, already validated)
                if plan.src != target:
                    _check_no_symlink_or_non_dir(target, cwd_path)
                    if _stat_safe(target) is not None:
                        raise ValueError(
                            f"apply_patch commit failed: move target {target} appeared"
                        )

        # 5e. Backup every consumed source
        for plan in plans:
            if plan.kind in ("update", "delete"):
                backup_path = _backup_file(plan.src, plan.src.parent)
                backups.append((backup_path, plan.src))

        # 5f. Install all staged outputs (single try block - no inner catch)
        for plan in plans:
            if plan.kind == "delete":
                continue

            if plan.kind == "add":
                target = plan.dst if plan.dst is not None else plan.src
            elif plan.kind == "update":
                target = plan.dst if plan.dst is not None else plan.src
            else:
                continue

            stage = plan._stage_path
            assert stage is not None

            _install_file(stage, target)

            # Record target identity immediately after install (before chmod, so rollback knows)
            installed.append((target, target.lstat().st_dev, target.lstat().st_ino))

            # chmod inside try, so failure triggers rollback
            if plan.kind == "update" and plan.src_mode is not None:
                try:
                    os.chmod(str(target), plan.src_mode)
                except OSError:
                    raise

    except Exception as exc:
        # Single rollback - no double cleanup
        rollback_errors: list[str] = []
        _rollback_and_report(rollback_errors)

        # Collect every still-existing backup path (lstat-safe)
        retained_list: list[str] = []
        for bp, orig in backups:
            if _stat_safe(bp) is not None:
                retained_list.append(str(bp))

        msg = f"apply_patch commit failed: {exc}"
        if rollback_errors:
            msg += f"; rollback errors: {'; '.join(rollback_errors)}"
        if retained_list:
            msg += f"; retained backups: {'; '.join(retained_list)}"
        raise RuntimeError(msg) from exc

    # Success: remove backups best-effort, report warnings
    cleanup_warnings: list[str] = []
    retained_backups: list[str] = []
    for bp, orig in backups:
        try:
            if _stat_safe(bp) is not None:
                bp.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_warnings.append(f"cleanup: failed to remove backup {bp}: {exc}")
            retained_backups.append(str(bp))

    result_details: dict[str, Any] = {"diff": ""}
    if cleanup_warnings:
        result_details["cleanupWarnings"] = cleanup_warnings
        result_details["retainedBackups"] = retained_backups

    # - Phase 6: build result -

    results: list[tuple[str, str]] = []
    diff_parts: list[str] = []

    for idx, plan in enumerate(plans):
        kind = plan.kind
        src = plan.src
        dst = plan.dst
        path_str = plan.path_str

        if kind == "add":
            assert dst is not None
            results.append(("A", _rel_path(dst, cwd_path)))
            diff_parts.append("".join(difflib.unified_diff(
                [], plan.final_bytes.decode("utf-8").splitlines(keepends=True),
                fromfile=path_str, tofile=path_str,
            )))
        elif kind == "delete":
            results.append(("D", _rel_path(src, cwd_path)))
        elif kind == "update":
            display_path = _rel_path(dst, cwd_path) if dst is not None else _rel_path(src, cwd_path)
            results.append(("M", display_path))
            old = old_contents.get(idx, "")
            new = plan.final_bytes.decode("utf-8")
            diff_parts.append("".join(difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=path_str, tofile=path_str,
            )))

    summary_lines = "\n".join(f"{act} {rp}" for act, rp in results)
    combined_diff = "".join(diff_parts)
    result_details["diff"] = combined_diff

    return {
        "content": [{"type": "text", "text": f"Success. Updated the following files:\n{summary_lines}"}],
        "details": result_details,
    }
