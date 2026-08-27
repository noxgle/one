"""Secure file-persistence primitives for user-state files.

All functions are cross-platform, standard-library-only, and tolerate
platform differences (e.g. chmod on Windows).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------

_IS_POSIX = os.name == "posix"


def _chmod(path: Path, mode: int) -> None:
    """Chmod *path* when possible; silently skip on Windows."""
    if _IS_POSIX:
        try:
            os.chmod(str(path), mode)
        except OSError:
            pass  # best-effort — permissions may already be fine


def _write_all(fd: int, data: bytes) -> None:
    """Write *data* to file descriptor *fd* in a loop.

    ``os.write`` may perform a partial write; this function retries until
    every byte is written.  Zero-progress is treated as an error.
    """
    if not data:
        return
    total = len(data)
    offset = 0
    while offset < total:
        n = os.write(fd, data[offset:])
        if n <= 0:
            raise OSError(f"write returned {n} at offset {offset} (expected >0)")
        offset += n


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_private_dir(path: Path) -> None:
    """Create *path* (parents included) and enforce mode 0700 on POSIX.

    The **exact** directory passed is always chmod 0700 on POSIX after
    mkdir, whether newly created or already present.  Intermediate
    directories follow mkdir/umask — only the target itself is tightened.

    On non-POSIX platforms the directory is still created (parents too) but
    permission bits are left as-is.
    """
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, 0o700)


def ensure_private_file(path: Path, mode: int = 0o600) -> None:
    """Ensure *path*'s parent directory is private (0700 on POSIX) and
    tighten the file itself to *mode* (default 0600).

    Never creates the file — only manages its directory and permissions.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    _chmod(parent, 0o700)
    _chmod(path, mode)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Atomically write *text* to *path* with restricted permissions.

    Writes to a same-directory temporary file, fsyncs, then atomically
    replaces the target.  The final file is chmod'd to *mode* (default 0600).

    On failure the temporary file is cleaned up and no original is touched.
    """
    parent = path.parent
    ensure_private_dir(parent)

    fd = None
    tmp_path: Path | None = None
    try:
        # Create a temp file in the same directory so os.replace is atomic.
        fd, tmp_path_str = tempfile.mkstemp(dir=str(parent), prefix=".tmp_", suffix=".write")
        tmp_path = Path(tmp_path_str)
        data = text.encode("utf-8")
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = None
        _chmod(tmp_path, mode)
        os.replace(str(tmp_path), str(path))
        _chmod(path, mode)
        # fsync the directory on POSIX so the rename survives a crash.
        if _IS_POSIX:
            try:
                dir_fd = os.open(str(parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
    except BaseException:
        # Clean up temp on any failure — never corrupt target.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def append_private_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Append *text* to *path* with restricted permissions.

    Opens/creates the file with ``O_APPEND | O_CREAT`` and 0600 on POSIX,
    writes all bytes, then fsyncs.  Existing files get their mode tightened.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    _chmod(parent, 0o700)

    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if _IS_POSIX:
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(str(path), flags, 0o600)
        try:
            _chmod(path, mode)
        finally:
            pass
    else:
        path.touch(exist_ok=True)
        _chmod(path, mode)
        fd = os.open(str(path), flags)

    try:
        data = text.encode("utf-8")
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def load_json_text_safe(path: Path) -> tuple[Any, Exception | None]:
    """Load JSON from *path*, returning ``(parsed, None)`` or ``(None, err)``.

    Never overwrites or deletes the file.  Returns ``(None, err)`` when the
    file cannot be parsed.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data, None
    except Exception as exc:
        return None, exc
