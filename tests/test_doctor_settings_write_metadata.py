# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys

import pytest

from one.core.doctor import settings_write as writer


def test_acl_is_rejected_without_modification(tmp_path):
    setfacl = shutil.which("setfacl")
    if not setfacl:
        pytest.skip("setfacl unavailable")
    path = tmp_path / "settings.json"
    path.write_bytes(b"old")
    result = subprocess.run([setfacl, "-m", "u:12345:r--", str(path)], capture_output=True, check=False)
    if result.returncode:
        pytest.skip("filesystem does not support ACLs")
    before = os.getxattr(path, "system.posix_acl_access")
    with pytest.raises(writer.WriteError, match="unsafe_metadata"):
        writer.inspect(path)
    assert path.read_bytes() == b"old"
    assert os.getxattr(path, "system.posix_acl_access") == before


@pytest.mark.parametrize("operation", ["listxattr", "fchmod", "fchown"])
@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_metadata_failure_never_replaces_original(tmp_path, monkeypatch, operation):
    path = tmp_path / "settings.json"
    path.write_bytes(b"old")
    path.chmod(0o640)
    snapshot = writer.inspect(path)
    before = path.stat()

    def fail(*args, **kwargs):
        raise OSError("metadata unavailable")

    monkeypatch.setattr(writer.os, operation, fail, raising=False)
    with pytest.raises(writer.WriteError):
        writer.apply(path, snapshot, b"new", lambda _: None)
    after = path.stat()
    assert path.read_bytes() == b"old"
    assert (after.st_ino, after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
        before.st_ino, before.st_uid, before.st_gid, 0o640
    )


def test_inherited_default_acl_refuses_replacement(tmp_path):
    setfacl = shutil.which("setfacl")
    if not setfacl:
        pytest.skip("setfacl unavailable")
    path = tmp_path / "settings.json"
    path.write_bytes(b"old")
    snapshot = writer.inspect(path)
    result = subprocess.run([setfacl, "-m", "d:u:12345:r--", str(tmp_path)], capture_output=True, check=False)
    if result.returncode:
        pytest.skip("filesystem does not support ACLs")
    with pytest.raises(writer.WriteError, match="unsafe_metadata"):
        writer.apply(path, snapshot, b"new", lambda _: None)
    assert path.read_bytes() == b"old"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_parent_replaced_after_inspection(tmp_path, existing):
    parent = tmp_path / "parent"
    parent.mkdir()
    path = parent / "settings.json"
    if existing:
        path.write_bytes(b"old")
    snapshot = writer.inspect(path)
    parent.rename(tmp_path / "old-parent")
    parent.mkdir()
    with pytest.raises(writer.WriteError, match="conflict"):
        writer.apply(path, snapshot, b"new", lambda _: None)
    assert not path.exists()
    assert (tmp_path / "old-parent" / "settings.json").exists() == existing


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_parent_replaced_before_rename(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    path = parent / "settings.json"
    path.write_bytes(b"old")
    snapshot = writer.inspect(path)

    def swap(_):
        parent.rename(tmp_path / "old-parent")
        parent.mkdir()
        path.write_bytes(b"other")

    with pytest.raises(writer.WriteError, match="conflict"):
        writer.apply(path, snapshot, b"new", swap)
    assert path.read_bytes() == b"other"
    assert (tmp_path / "old-parent" / "settings.json").read_bytes() == b"old"


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_ancestor_symlink_rejected(tmp_path):
    real = tmp_path / "real"
    (real / "parent").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        writer.inspect(tmp_path / "link" / "parent" / "settings.json")
