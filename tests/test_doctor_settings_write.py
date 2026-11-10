# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import os
import stat

import pytest

from one.core.doctor import settings_write as writer


def valid(data):
    assert data == b'new'


def test_existing_and_idempotent(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_bytes(b'old')
    path.chmod(0o640)
    original = path.stat()
    backup = writer.apply(path, writer.inspect(path), b'new', valid)
    assert backup.read_bytes() == b'old'
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert (path.stat().st_uid, path.stat().st_gid, stat.S_IMODE(path.stat().st_mode)) == (
        original.st_uid, original.st_gid, 0o640
    )
    assert writer.apply(path, writer.inspect(path), b'new', valid) is None
    assert len(list(tmp_path.glob('.settings.json.backup-*'))) == 1


def test_conflict_and_unique_backups(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_bytes(b'old')
    snapshot = writer.inspect(path)
    path.write_bytes(b'changed')
    with pytest.raises(writer.WriteError, match='conflict'):
        writer.apply(path, snapshot, b'new', valid)
    first = writer.apply(path, writer.inspect(path), b'new', valid)
    second = writer.apply(path, writer.inspect(path), b'other', lambda _: None)
    assert first != second and first.read_bytes() == b'changed' and second.read_bytes() == b'new'


@pytest.mark.parametrize('existing', [False, True])
def test_postvalidation_rollback(tmp_path, existing):
    path = tmp_path / 'settings.json'
    if existing:
        path.write_bytes(b'old')
    calls = 0
    def validate(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError('invalid')
    with pytest.raises(writer.WriteError, match='rolled_back') as error:
        writer.apply(path, writer.inspect(path), b'new', validate)
    assert path.exists() == existing
    if existing:
        assert path.read_bytes() == b'old' and error.value.backup.read_bytes() == b'old'


def test_race_prevents_rollback(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_bytes(b'old')
    calls = 0
    def validate(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b'other')
            raise ValueError()
    with pytest.raises(writer.WriteError, match='unsafe_rollback') as error:
        writer.apply(path, writer.inspect(path), b'new', validate)
    assert path.read_bytes() == b'other' and error.value.backup.read_bytes() == b'old'


@pytest.mark.parametrize('kind', ['symlink', 'directory', 'hardlink', 'xattr'])
def test_unsafe_target(tmp_path, kind):
    path = tmp_path / 'settings.json'
    if kind == 'symlink':
        path.symlink_to(tmp_path / 'absent')
    elif kind == 'directory':
        path.mkdir()
    else:
        path.write_bytes(b'old')
        if kind == 'hardlink':
            os.link(path, tmp_path / 'link')
        else:
            os.setxattr(path, 'user.test', b'value')
    with pytest.raises((writer.WriteError, OSError)):
        writer.inspect(path)


@pytest.mark.parametrize('operation', ['fsync', 'replace', 'fchown'])
def test_injected_failure_preserves_original(tmp_path, monkeypatch, operation):
    path = tmp_path / 'settings.json'
    path.write_bytes(b'old')
    snapshot = writer.inspect(path)
    def fail(*args, **kwargs):
        raise OSError('injected')
    monkeypatch.setattr(writer.os, operation, fail)
    with pytest.raises(writer.WriteError):
        writer.apply(path, snapshot, b'new', valid)
    assert path.read_bytes() == b'old'


def test_change_before_replace_detected(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_bytes(b'old')
    def validate(data):
        path.write_bytes(b'concurrent')
    with pytest.raises(writer.WriteError, match='conflict') as error:
        writer.apply(path, writer.inspect(path), b'new', validate)
    assert path.read_bytes() == b'concurrent' and error.value.backup.read_bytes() == b'old'
