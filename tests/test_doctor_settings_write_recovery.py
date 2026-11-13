# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import os
import sys

import pytest

from one.core.doctor import settings_write as writer


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_idempotent_write_still_validates(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"old")

    def reject(_):
        raise ValueError("invalid")

    with pytest.raises(writer.WriteError, match="failure"):
        writer.apply(path, writer.inspect(path), b"old", reject)
    assert path.read_bytes() == b"old"
    assert not list(tmp_path.glob(".settings.json.backup-*"))


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_change_during_rollback_is_not_reported_as_success(tmp_path, monkeypatch, existing):
    path = tmp_path / "settings.json"
    if existing:
        path.write_bytes(b"old")
    snapshot = writer.inspect(path)
    replace = os.replace
    unlink = os.unlink

    def replace_and_change(src, dst, **kwargs):
        replace(src, dst, **kwargs)
        if str(src).startswith(".settings.json.restore-"):
            path.write_bytes(b"other")

    def unlink_and_change(name, **kwargs):
        unlink(name, **kwargs)
        if name == "settings.json":
            path.write_bytes(b"other")

    monkeypatch.setattr(writer.os, "replace", replace_and_change)
    monkeypatch.setattr(writer.os, "unlink", unlink_and_change)
    calls = 0

    def validate(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("postvalidation")

    with pytest.raises(writer.WriteError, match="unsafe_rollback") as error:
        writer.apply(path, snapshot, b"new", validate)
    assert path.read_bytes() == b"other"
    if existing:
        assert error.value.backup.read_bytes() == b"old"
    else:
        assert error.value.backup is None


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_moved_parent_does_not_report_misleading_backup(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    path = parent / "settings.json"
    path.write_bytes(b"old")
    snapshot = writer.inspect(path)

    def move(_):
        parent.rename(tmp_path / "moved")
        parent.mkdir()

    with pytest.raises(writer.WriteError, match="conflict") as error:
        writer.apply(path, snapshot, b"new", move)
    assert error.value.backup is None
    backups = list((tmp_path / "moved").glob(".settings.json.backup-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"old"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_post_replace_directory_fsync_failure(tmp_path, monkeypatch, existing):
    path = tmp_path / "settings.json"
    if existing:
        path.write_bytes(b"old")
    snapshot = writer.inspect(path)
    fsync = os.fsync
    replace = os.replace
    replaced = False
    failed = False

    def mark_replace(*args, **kwargs):
        nonlocal replaced
        replace(*args, **kwargs)
        replaced = True

    def fail_once(fd):
        nonlocal failed
        if replaced and not failed:
            failed = True
            raise OSError("injected fsync failure")
        fsync(fd)

    monkeypatch.setattr(writer.os, "replace", mark_replace)
    monkeypatch.setattr(writer.os, "fsync", fail_once)
    with pytest.raises(writer.WriteError, match="rolled_back") as error:
        writer.apply(path, snapshot, b"new", lambda _: None)
    assert failed and path.exists() == existing
    if existing:
        assert path.read_bytes() == b"old"
        assert error.value.backup.read_bytes() == b"old"
    else:
        assert error.value.backup is None


@pytest.mark.parametrize("stage", ["before", "rename_before", "rename_after", "verification"])
@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_keyboard_interrupt_preserves_safe_recovery(tmp_path, monkeypatch, stage):
    path = tmp_path / "settings.json"
    path.write_bytes(b"old")
    snapshot = writer.inspect(path)
    replace = os.replace
    calls = 0

    def interrupt_replace(src, dst, **kwargs):
        if stage == "rename_before":
            raise KeyboardInterrupt("secret")
        replace(src, dst, **kwargs)
        if stage == "rename_after":
            raise KeyboardInterrupt("secret")

    def validate(_):
        nonlocal calls
        calls += 1
        if (stage == "before" and calls == 1) or (stage == "verification" and calls == 2):
            raise KeyboardInterrupt("secret")

    monkeypatch.setattr(writer.os, "replace", interrupt_replace)
    with pytest.raises(writer.WriteError) as error:
        writer.apply(path, snapshot, b"new", validate)
    expected = {"before": "interrupted", "rename_before": "unsafe_rollback",
                "rename_after": "unsafe_rollback", "verification": "rolled_back"}
    assert error.value.status == expected[stage]
    assert "secret" not in str(error.value)
    assert path.read_bytes() == (b"new" if stage == "rename_after" else b"old")
    assert error.value.backup.read_bytes() == b"old"


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_interrupt_after_concurrent_change_never_overwrites(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"old")
    snapshot = writer.inspect(path)
    calls = 0

    def validate(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"other")
            raise KeyboardInterrupt

    with pytest.raises(writer.WriteError, match="unsafe_rollback") as error:
        writer.apply(path, snapshot, b"new", validate)
    assert path.read_bytes() == b"other"
    assert error.value.backup.read_bytes() == b"old"
