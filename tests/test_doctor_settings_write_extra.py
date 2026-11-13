# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import os
import sys

import pytest

from one.core.doctor import settings_write as writer


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_post_replace_fsync_failure_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_bytes(b'old')
    original = writer.os.fsync
    calls = 0
    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError('directory fsync')
        return original(fd)
    monkeypatch.setattr(writer.os, 'fsync', fsync)
    with pytest.raises(writer.WriteError, match='rolled_back') as error:
        writer.apply(path, writer.inspect(path), b'new', lambda _: None)
    assert path.read_bytes() == b'old'
    assert error.value.backup.read_bytes() == b'old'


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_replacement_after_rename_not_rolled_back(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_bytes(b'old')
    original = writer.os.replace
    def replace(src, dst, **kwargs):
        original(src, dst, **kwargs)
        path.unlink()
        path.write_bytes(b'new')
    monkeypatch.setattr(writer.os, 'replace', replace)
    with pytest.raises(writer.WriteError, match='unsafe_rollback') as error:
        writer.apply(path, writer.inspect(path), b'new', lambda _: None)
    assert path.read_bytes() == b'new'
    assert error.value.backup.read_bytes() == b'old'


def test_symlink_lock_rejected(tmp_path):
    path = tmp_path / 'settings.json'
    (tmp_path / '.settings.json.lock').symlink_to(tmp_path / 'other')
    with pytest.raises(writer.WriteError):
        writer.apply(path, writer.inspect(path), b'new', lambda _: None)
    assert not path.exists() and not (tmp_path / 'other').exists()


def test_auth_rejected(tmp_path):
    path = tmp_path / 'auth.json'
    path.write_bytes(b'secret')
    with pytest.raises(writer.WriteError, match='unsupported_target'):
        writer.inspect(path)
    assert path.read_bytes() == b'secret'


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_new_file_private(tmp_path):
    path = tmp_path / 'settings.json'
    assert writer.apply(path, writer.inspect(path), b'new', lambda _: None) is None
    assert os.stat(path).st_mode & 0o777 == 0o600


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_fifo_lock_rejected_without_blocking(tmp_path):
    path = tmp_path / 'settings.json'
    os.mkfifo(tmp_path / '.settings.json.lock')
    with pytest.raises(writer.WriteError, match='unsafe_lock'):
        writer.apply(path, writer.inspect(path), b'new', lambda _: None)
    assert not path.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_lock_replaced_while_waiting(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    lock = tmp_path / '.settings.json.lock'
    original = writer.fcntl.flock

    def flock(fd, operation):
        lock.unlink()
        lock.write_bytes(b'')
        lock.chmod(0o600)
        original(fd, operation)

    monkeypatch.setattr(writer.fcntl, 'flock', flock)
    with pytest.raises(writer.WriteError, match='unsafe_lock'):
        writer.apply(path, writer.inspect(path), b'new', lambda _: None)
    assert not path.exists()


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_prevalidation_failure_does_not_replace(tmp_path, existing):
    path = tmp_path / 'settings.json'
    if existing:
        path.write_bytes(b'old')
    def reject(_):
        raise ValueError('invalid')
    with pytest.raises(writer.WriteError, match='failure') as error:
        writer.apply(path, writer.inspect(path), b'new', reject)
    assert path.exists() == existing
    if existing:
        assert path.read_bytes() == b'old'
        assert error.value.backup.read_bytes() == b'old'
    else:
        assert error.value.backup is None
    assert not list(tmp_path.glob('.settings.json.tmp-*'))


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_rollback_replace_failure_retains_backup_and_written_file(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_bytes(b'old')
    original = writer.os.replace
    calls = 0
    def replace(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError('restore denied')
        return original(*args, **kwargs)
    def validate(_):
        if calls:
            raise ValueError('postvalidation')
    monkeypatch.setattr(writer.os, 'replace', replace)
    with pytest.raises(writer.WriteError, match='unsafe_rollback') as error:
        writer.apply(path, writer.inspect(path), b'new', validate)
    assert path.read_bytes() == b'new'
    assert error.value.backup.read_bytes() == b'old'
    assert not list(tmp_path.glob('.settings.json.restore-*'))


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_absent_file_created_by_other_writer_is_conflict(tmp_path):
    path = tmp_path / 'settings.json'
    snapshot = writer.inspect(path)
    path.write_bytes(b'other')
    with pytest.raises(writer.WriteError, match='conflict'):
        writer.apply(path, snapshot, b'new', lambda _: None)
    assert path.read_bytes() == b'other'


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_rollback_directory_fsync_failure_reports_uncertainty(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    path.write_bytes(b'old')
    original = writer.os.fsync
    calls = 0
    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 6:
            raise OSError('rollback fsync')
        return original(fd)
    def validate(_):
        if calls >= 4:
            raise ValueError('postvalidation')
    monkeypatch.setattr(writer.os, 'fsync', fsync)
    with pytest.raises(writer.WriteError, match='unsafe_rollback') as error:
        writer.apply(path, writer.inspect(path), b'new', validate)
    assert path.read_bytes() == b'old'
    assert error.value.backup.read_bytes() == b'old'


@pytest.mark.skipif(sys.platform != "linux", reason="doctor settings writer is Linux-only")
def test_symlink_substitution_before_replace_preserves_backup(tmp_path):
    path = tmp_path / 'settings.json'
    other = tmp_path / 'other'
    path.write_bytes(b'old')
    other.write_bytes(b'other')
    def validate(_):
        path.unlink()
        path.symlink_to(other)
    with pytest.raises(writer.WriteError) as error:
        writer.apply(path, writer.inspect(path), b'new', validate)
    assert path.is_symlink()
    assert other.read_bytes() == b'other'
    assert error.value.backup.read_bytes() == b'old'
