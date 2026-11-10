"""Conservative, single-file settings replacement; not a transaction or a CAS.

Linux only. The containing directory must already exist and be trusted: flock is
advisory and non-cooperating writers can race the final check and rename/unlink.
A crash can leave temporary files or a backup. No parent permissions are changed.
Extended attributes (including POSIX ACLs), hard links and special mode bits are
rejected rather than silently lost. Filesystems must support directory fsync and
xattr enumeration. Callers must obtain approval before calling apply().
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import fcntl
import os
import secrets
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class WriteError(Exception):
    """A failed operation, including its recovery state and retained backup."""

    def __init__(self, status: str, backup: Path | None = None):
        super().__init__(status)
        self.status = status
        self.backup = backup


@dataclass(frozen=True)
class Snapshot:
    exists: bool
    data: bytes = b""
    identity: tuple[int, int, int, int, int, int, int] | None = None
    parent: tuple[int, int] | None = None


def _identity(st: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_uid, st.st_gid, st.st_mode, st.st_mtime_ns, st.st_ctime_ns)


def _snapshot(directory: int) -> Snapshot:
    parent_stat = os.fstat(directory)
    parent = (parent_stat.st_dev, parent_stat.st_ino)
    try:
        fd = os.open("settings.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return Snapshot(False, parent=parent)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o7000:
            raise WriteError("unsafe_target")
        if os.listxattr(fd):
            raise WriteError("unsafe_metadata")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read()
        after = os.fstat(fd)
        if _identity(st) != _identity(after) or st.st_size != after.st_size or len(data) != after.st_size:
            raise WriteError("conflict")
        return Snapshot(True, data, _identity(after), parent)
    finally:
        os.close(fd)


def _directory(path: Path) -> int:
    if sys.platform != "linux" or path.name != "settings.json":
        raise WriteError("unsupported_target")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open("/" if path.is_absolute() else ".", flags)
    try:
        for part in path.parent.parts:
            if part in ("/", "."):
                continue
            if part == "..":
                raise WriteError("unsupported_target")
            next_directory = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        return directory
    except BaseException:
        os.close(directory)
        raise


def _check_directory(path: Path, directory: int) -> None:
    current = _directory(path)
    try:
        a, b = os.fstat(directory), os.fstat(current)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise WriteError("conflict")
    finally:
        os.close(current)


def inspect(path: Path) -> Snapshot:
    """Capture the exact existing version (or explicit absence) for approval."""
    directory = _directory(path)
    try:
        return _snapshot(directory)
    finally:
        os.close(directory)


def _create(directory: int, prefix: str, data: bytes, mode: int, uid: int, gid: int) -> str:
    name = prefix + secrets.token_hex(16)
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        st = os.fstat(fd)
        if (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (uid, gid, mode) or os.listxattr(fd):
            raise WriteError("unsafe_metadata")
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            if stream.read() != data:
                raise WriteError("failure")
    except BaseException:
        os.unlink(name, dir_fd=directory)
        raise
    finally:
        os.close(fd)
    return name


def apply(path: Path, expected: Snapshot, data: bytes, validate: Callable[[bytes], None]) -> Path | None:
    """Apply an approved snapshot; validator raises on invalid bytes.

    Returns backup path or None. WriteError statuses distinguish conflict,
    failure, interrupted, rolled_back and unsafe_rollback; backup is retained on failure.
    A backup path is only reported while its parent still identifies the opened
    directory; a moved parent may still contain a backup at an unknown path.
    The permanent lock file is intentionally never removed, avoiding lock inode
    replacement between cooperating processes. Backups are private (0600).
    """
    directory = _directory(path)
    backup = None
    temp = None
    replacing = False
    written = None
    lock = None
    try:
        lock = os.open(
            ".settings.json.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600, dir_fd=directory,
        )
        st = os.fstat(lock)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise WriteError("unsafe_lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        lock_path = os.stat(".settings.json.lock", dir_fd=directory, follow_symlinks=False)
        if (lock_path.st_dev, lock_path.st_ino) != (st.st_dev, st.st_ino):
            raise WriteError("unsafe_lock")
        _check_directory(path, directory)
        current = _snapshot(directory)
        if current != expected:
            raise WriteError("conflict")
        if current.exists and current.data == data:
            validate(data)
            _check_directory(path, directory)
            if _snapshot(directory) != current:
                raise WriteError("conflict")
            return None
        if current.exists:
            backup_name = _create(directory, ".settings.json.backup-", current.data, 0o600, os.geteuid(), os.getegid())
            backup = path.parent / backup_name
            os.fsync(directory)
            assert current.identity is not None
            uid, gid, mode = current.identity[2], current.identity[3], stat.S_IMODE(current.identity[4])
        else:
            uid, gid, mode = os.geteuid(), os.getegid(), 0o600
        temp = _create(directory, ".settings.json.tmp-", data, mode, uid, gid)
        validate(data)
        _check_directory(path, directory)
        if _snapshot(directory) != expected:
            raise WriteError("conflict", backup)
        temp_stat = os.stat(temp, dir_fd=directory, follow_symlinks=False)
        # From here onward an interruption may occur after rename but before
        # Python regains control. Never assume that replacement did not happen.
        replacing = True
        os.replace(temp, "settings.json", src_dir_fd=directory, dst_dir_fd=directory)
        temp = None
        written = _snapshot(directory)
        if (not written.exists or written.data != data or written.identity is None
                or written.identity[:2] != (temp_stat.st_dev, temp_stat.st_ino)):
            written = None
            raise WriteError("unsafe_rollback", backup)
        os.fsync(directory)
        validate(written.data)
        _check_directory(path, directory)
        if _snapshot(directory) != written:
            raise WriteError("unsafe_rollback", backup)
        return backup
    except (Exception, KeyboardInterrupt) as exc:
        if replacing:
            try:
                if (written is None or not written.exists or written.data != data or written.identity is None
                        or written.identity[:2] != (temp_stat.st_dev, temp_stat.st_ino)
                        or _snapshot(directory) != written):
                    raise WriteError("unsafe_rollback", backup)
                _check_directory(path, directory)
                if expected.exists:
                    assert expected.identity is not None
                    uid, gid = expected.identity[2:4]
                    mode = stat.S_IMODE(expected.identity[4])
                    restore = _create(directory, ".settings.json.restore-", expected.data, mode, uid, gid)
                    try:
                        restore_stat = os.stat(restore, dir_fd=directory, follow_symlinks=False)
                        if _snapshot(directory) != written:
                            raise WriteError("unsafe_rollback", backup)
                        _check_directory(path, directory)
                        os.replace(restore, "settings.json", src_dir_fd=directory, dst_dir_fd=directory)
                    finally:
                        try:
                            os.unlink(restore, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                else:
                    os.unlink("settings.json", dir_fd=directory)
                os.fsync(directory)
                _check_directory(path, directory)
                restored = _snapshot(directory)
                if expected.exists:
                    if (not restored.exists or restored.data != expected.data or restored.identity is None
                            or restored.identity[:2] != (restore_stat.st_dev, restore_stat.st_ino)
                            or restored.identity[2:5] != expected.identity[2:5]):
                        raise WriteError("unsafe_rollback", backup)
                elif restored.exists:
                    raise WriteError("unsafe_rollback", backup)
            except (Exception, KeyboardInterrupt) as rollback_error:
                try:
                    _check_directory(path, directory)
                except (Exception, KeyboardInterrupt):
                    backup = None
                raise WriteError("unsafe_rollback", backup) from rollback_error
            raise WriteError("rolled_back", backup) from exc
        if backup is not None:
            try:
                _check_directory(path, directory)
            except (Exception, KeyboardInterrupt):
                backup = None
        if isinstance(exc, WriteError):
            raise WriteError(exc.status, backup) from exc
        if isinstance(exc, KeyboardInterrupt):
            raise WriteError("interrupted", backup) from exc
        raise WriteError("failure", backup) from exc
    finally:
        if lock is not None:
            os.close(lock)
        if temp is not None:
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)
