"""Tests for one.core.checkpoints."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import os

import pytest

from one.core.checkpoints import CheckpointError, CheckpointManager


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    manager = CheckpointManager(tmp_path / 'private', workspace, 'session')
    yield manager, workspace
    manager.close()


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_selective_undo_and_conflict(setup):
    manager, root = setup
    (root / 'a').write_text('old')
    (root / 'a').chmod(0o640)
    manager.begin_task('task')
    manager.capture_preimage(['a', 'b'])
    (root / 'a').write_text('new')
    (root / 'b').write_text('created')
    manager.record_postimage()
    manager.end_task()
    token = manager.prepare_undo('task', ['a'])['token']
    assert manager.confirm_undo(token) == ['a']
    assert (root / 'a').read_text() == 'old'
    assert (root / 'a').stat().st_mode & 0o777 == 0o640
    assert (root / 'b').read_text() == 'created'
    with pytest.raises(CheckpointError):
        manager.confirm_undo(token)
    token = manager.prepare_undo('task', ['b'])['token']
    (root / 'b').write_text('external')
    with pytest.raises(CheckpointError, match='Conflict'):
        manager.confirm_undo(token)


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_move_pair_and_restart(setup, tmp_path):
    manager, root = setup
    (root / 'source').write_bytes(b'data')
    manager.begin_task('move')
    manager.capture_preimage(['source', 'dest'], [('source', 'dest')])
    (root / 'source').rename(root / 'dest')
    manager.record_postimage()
    manager.end_task()
    reopened = CheckpointManager(tmp_path / 'private', root, 'session')
    try:
        with pytest.raises(CheckpointError, match='endpoints'):
            reopened.prepare_undo('move', ['dest'])
        reopened.confirm_undo(reopened.prepare_undo('move', ['source', 'dest'])['token'])
        assert (root / 'source').read_bytes() == b'data'
        assert not (root / 'dest').exists()
        fork = CheckpointManager(tmp_path / 'private', root, 'fork')
        try:
            assert fork.list_checkpoints() == []
        finally:
            fork.close()
    finally:
        reopened.close()


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_unsafe_paths_and_interrupted_intent(setup, tmp_path):
    manager, root = setup
    (root / 'file').write_text('x')
    os.link(root / 'file', root / 'hard')
    (root / 'link').symlink_to(root / 'file')
    manager.begin_task('task')
    for path in ('../escape', '/absolute', 'link', 'hard', 'missing/file'):
        with pytest.raises(CheckpointError):
            manager.capture_preimage([path])
    (root / 'hard').unlink()
    manager.capture_preimage(['file'])
    resumed = CheckpointManager(tmp_path / 'private', root, 'session')
    try:
        assert resumed.list_checkpoints()[0]['status'] == 'incomplete'
        with pytest.raises(CheckpointError):
            resumed.prepare_undo('task', ['file'])
    finally:
        resumed.close()


def test_swapped_storage_directory_uses_open_descriptor(setup, tmp_path):
    manager, _ = setup
    storage = tmp_path / 'private'
    storage.rename(tmp_path / 'original')
    storage.mkdir(mode=0o700)
    manager.begin_task('task')
    assert list((tmp_path / 'original').glob('*.json'))
    assert not list(storage.glob('*.json'))


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_manifest_rejects_invalid_image_with_valid_envelope(setup, tmp_path):
    import hashlib
    import json

    manager, root = setup
    (root / 'a').write_text('before')
    manager.begin_task('task')
    manager.capture_preimage(['a'])
    manager.record_postimage()
    manager.end_task()
    path = tmp_path / 'private' / manager.file
    envelope = json.loads(path.read_text())
    envelope['payload']['records'][0]['paths']['a']['pre']['sha256'] = '0' * 64
    encoded = json.dumps(envelope['payload'], sort_keys=True, separators=(',', ':')).encode()
    envelope['sha256'] = hashlib.sha256(encoded).hexdigest()
    path.write_text(json.dumps(envelope))
    with pytest.raises(CheckpointError, match='Corrupt'):
        CheckpointManager(tmp_path / 'private', root, 'session')


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_image_rejects_inode_swap_during_read(setup, monkeypatch):
    manager, root = setup
    (root / 'a').write_text('before')
    original = os.read
    def swap(fd, size):
        data = original(fd, size)
        if data:
            (root / 'replacement').write_text('other')
            (root / 'replacement').replace(root / 'a')
        return data
    monkeypatch.setattr(os, 'read', swap)
    with pytest.raises(CheckpointError, match='changed while reading'):
        manager._image('a')


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_reservation_failure_leaves_no_intent(setup, monkeypatch):
    manager, root = setup
    (root / 'a').write_text('old')
    manager.begin_task('task')
    monkeypatch.setattr(manager, 'MAX_STORAGE', 1000)
    with pytest.raises(CheckpointError, match='storage limit'):
        manager.capture_preimage(['a'])
    assert manager.records[-1]['status'] == 'active'
    assert manager.records[-1]['paths'] == {}
    assert (root / 'a').read_text() == 'old'


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_undo_staging_failure_does_not_modify_targets(setup, monkeypatch):
    manager, root = setup
    for path in ('a', 'b'):
        (root / path).write_text('old')
    manager.begin_task('task')
    manager.capture_preimage(['a', 'b'])
    for path in ('a', 'b'):
        (root / path).write_text('new')
    manager.record_postimage()
    manager.end_task()
    token = manager.prepare_undo('task', ['a', 'b'])['token']
    original = os.open
    count = 0

    def fail_second(path, *args, **kwargs):
        nonlocal count
        if str(path).startswith('.one-undo-'):
            count += 1
            if count == 2:
                raise OSError('disk full')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', fail_second)
    with pytest.raises(CheckpointError, match='Undo incomplete'):
        manager.confirm_undo(token)
    assert [(root / p).read_text() for p in ('a', 'b')] == ['new', 'new']
    assert not list(root.glob('.one-undo-*'))


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_undo_rejects_parent_swapped_after_staging(setup, tmp_path, monkeypatch):
    manager, root = setup
    parent = root / 'dir'
    parent.mkdir()
    (parent / 'a').write_text('old')
    manager.begin_task('task')
    manager.capture_preimage(['dir/a'])
    (parent / 'a').write_text('new')
    manager.record_postimage()
    manager.end_task()
    token = manager.prepare_undo('task', ['dir/a'])['token']

    outside = tmp_path / 'moved-parent'
    original_fsync = os.fsync
    swapped = False

    def swap_after_stage(fd):
        nonlocal swapped
        original_fsync(fd)
        if not swapped and list(parent.glob('.one-undo-*')):
            swapped = True
            parent.rename(outside)
            parent.mkdir()
            (parent / 'a').write_text('new')

    monkeypatch.setattr(os, 'fsync', swap_after_stage)
    with pytest.raises(CheckpointError, match='Undo incomplete') as exc:
        manager.confirm_undo(token)
    assert swapped
    assert 'Parent changed' in str(exc.value.__cause__)
    assert (outside / 'a').read_text() == 'new'
    assert (parent / 'a').read_text() == 'new'
    assert not list(outside.glob('.one-undo-*'))


def test_stale_manager_cannot_overwrite_manifest(setup, tmp_path):
    manager, root = setup
    other = CheckpointManager(tmp_path / 'private', root, 'session')
    try:
        manager.begin_task('first')
        with pytest.raises(CheckpointError, match='concurrently'):
            other.begin_task('second')
        assert other.active is None
        assert other.records == []
    finally:
        other.close()


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_diff_uses_stored_images_and_rejects_incomplete(setup):
    manager, root = setup
    (root / 'a').write_text('old\n')
    manager.begin_task('task')
    manager.capture_preimage(['a', 'created', 'deleted'])
    with pytest.raises(CheckpointError, match='incomplete'):
        manager.diff('task', 'a')
    (root / 'a').write_text('new\n')
    (root / 'created').write_text('created\n')
    manager.record_postimage()
    manager.end_task()
    (root / 'a').write_text('external\n')
    result = manager.diff('task', 'a')
    assert '-old\n' in result and '+new\n' in result and 'external' not in result
    assert '--- /dev/null' in manager.diff('task', 'created')
    assert manager.diff('task', 'deleted') == ''
    assert (root / 'a').read_text() == 'external\n'
    with pytest.raises(CheckpointError):
        manager.diff('task', '../missing')
    with pytest.raises(CheckpointError):
        manager.diff('missing', 'a')


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_diff_binary_and_output_bound(setup):
    manager, root = setup
    (root / 'binary').write_bytes(b'\xffsecret')
    (root / 'large').write_text('old\n')
    manager.begin_task('task')
    manager.capture_preimage(['binary', 'large'])
    (root / 'binary').write_bytes(b'\x00new')
    (root / 'large').write_text('é' * 50000 + '\n')
    manager.record_postimage()
    manager.end_task()
    binary = manager.diff('task', 'binary')
    assert 'size=7' in binary and 'size=4' in binary and 'sha256=' in binary
    assert 'secret' not in binary
    result = manager.diff('task', 'large')
    assert len(result.encode()) <= manager.MAX_DIFF
    assert result.endswith('... diff truncated ...\n')


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_diff_reports_permission_only_change(setup):
    manager, root = setup
    path = root / 'permissions'
    path.write_text('same')
    path.chmod(0o600)
    manager.begin_task('mode')
    manager.capture_preimage(['permissions'])
    path.chmod(0o644)
    manager.record_postimage()
    manager.end_task()
    assert manager.diff('mode', 'permissions') == 'Mode: 0600 -> 0644\n'


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_replace_file_records_original_and_restores(setup):
    manager, root = setup
    path = root / 'file'
    path.write_bytes(b'dirty')
    path.chmod(0o640)
    manager.begin_task('task')
    manager.replace_file('file', b'first')
    manager.replace_file('file', b'second')
    manager.end_task()
    assert path.read_bytes() == b'second'
    manager.confirm_undo(manager.prepare_undo('task', ['file'])['token'])
    assert path.read_bytes() == b'dirty'
    assert path.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_replace_file_rejects_external_change(setup, monkeypatch):
    manager, root = setup
    path = root / 'file'
    path.write_bytes(b'original')
    manager.begin_task('task')
    original = manager._image
    calls = 0
    def image(name):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b'external')
        return original(name)
    monkeypatch.setattr(manager, '_image', image)
    with pytest.raises(CheckpointError, match='Intervening change'):
        manager.replace_file('file', b'agent')
    assert path.read_bytes() == b'external'
    manager.end_task()
    assert manager.list_checkpoints()[0]['status'] == 'incomplete'
    assert not list(root.glob('.one-write-*'))


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_delete_file_restores_preimage_and_mode(setup):
    manager, root = setup
    (root / 'a').write_bytes(b'original')
    (root / 'a').chmod(0o640)
    manager.begin_task('delete')
    manager.delete_file('a')
    assert not (root / 'a').exists()
    manager.end_task()
    manager.confirm_undo(manager.prepare_undo('delete', ['a'])['token'])
    assert (root / 'a').read_bytes() == b'original'
    assert (root / 'a').stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_delete_checkpoint_invalidates_tokens(setup):
    manager, root = setup
    manager.begin_task('task')
    manager.replace_file('a', b'created')
    manager.end_task()
    token = manager.prepare_undo('task', ['a'])['token']
    manager.delete_checkpoint('task')
    with pytest.raises(CheckpointError):
        manager.confirm_undo(token)
    assert (root / 'a').read_bytes() == b'created'
    assert manager.list_checkpoints() == []


def patch(*sections):
    return '*** Begin Patch\n' + '\n'.join(sections) + '\n*** End Patch'


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_patch_all_operations_and_restart(setup, tmp_path):
    manager, root = setup
    for path in ('update', 'delete', 'source'):
        (root / path).write_bytes(b'old\n')
        (root / path).chmod(0o640)
    manager.begin_task('patch')
    changed = manager.apply_patch(patch(
        '*** Add File: added\n+created',
        '*** Update File: update\n@@\n-old\n+new',
        '*** Delete File: delete',
        '*** Update File: source\n*** Move to: dest\n@@\n-old\n+moved',
    ))
    assert set(changed) == {'added', 'update', 'delete', 'source', 'dest'}
    manager.end_task()
    assert (root / 'dest').read_bytes() == b'moved\n'
    assert (root / 'dest').stat().st_mode & 0o777 == 0o640
    assert not (root / 'source').exists()
    reopened = CheckpointManager(tmp_path / 'private', root, 'session')
    try:
        with pytest.raises(CheckpointError, match='endpoints'):
            reopened.prepare_undo('patch', ['source'])
        reopened.confirm_undo(reopened.prepare_undo('patch', changed)['token'])
        for path in ('update', 'delete', 'source'):
            assert (root / path).read_bytes() == b'old\n'
            assert (root / path).stat().st_mode & 0o777 == 0o640
        assert not (root / 'added').exists()
        assert not (root / 'dest').exists()
    finally:
        reopened.close()


@pytest.mark.parametrize('section', [
    '*** Add File: ../escape\n+bad',
    '*** Add File: /absolute\n+bad',
    '*** Add File: ./alias\n+bad',
    '*** Add File: absent/file\n+bad',
    '*** Add File: a\n+bad',
    '*** Update File: a\n*** Move to: a\n@@\n-old\n+new',
    '*** Update File: a\n*** Move to: occupied\n@@\n-old\n+new',
    '*** Update File: a\n@@\n-not-present\n+new',
    '*** Add File: same\n+x\n*** Delete File: same',
    '*** Delete File: a\n*** Delete File: a',
])
def test_patch_invalid_second_section_never_mutates(setup, section):
    manager, root = setup
    (root / 'a').write_text('old\n')
    (root / 'occupied').write_text('occupied')
    manager.begin_task('patch')
    with pytest.raises(CheckpointError):
        manager.apply_patch(patch('*** Add File: first\n+valid', section))
    assert not (root / 'first').exists()
    assert (root / 'a').read_text() == 'old\n'
    assert manager.records[-1]['paths'] == {}
    assert not list(root.glob('.one-*'))


@pytest.mark.parametrize('failure', ['stage', 'commit', 'rollback', 'manifest'])
@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_patch_injected_failures_are_incomplete(setup, monkeypatch, failure):
    manager, root = setup
    for path in ('a', 'b'):
        (root / path).write_bytes(b'old\n')
    manager.begin_task('task')
    original_open = os.open
    original_replace = os.replace
    original_save = manager._save
    stage_count = 0
    commit_count = 0

    def failing_open(path, *args, **kwargs):
        nonlocal stage_count
        if str(path).startswith('.one-write-'):
            stage_count += 1
            if failure == 'stage' and stage_count == 3:
                raise OSError('staging failed')
        return original_open(path, *args, **kwargs)

    def failing_replace(src, dest, *args, **kwargs):
        nonlocal commit_count
        if str(src).startswith('.one-write-'):
            commit_count += 1
            if failure in ('commit', 'rollback') and commit_count == 2:
                raise OSError('commit failed')
            if failure == 'rollback' and commit_count == 3:
                raise OSError('rollback failed')
        return original_replace(src, dest, *args, **kwargs)

    def failing_save(*args, **kwargs):
        if failure == 'manifest' and manager.records[-1]['status'] == 'active':
            raise OSError('postimage persistence failed')
        return original_save(*args, **kwargs)

    monkeypatch.setattr(os, 'open', failing_open)
    monkeypatch.setattr(os, 'replace', failing_replace)
    monkeypatch.setattr(manager, '_save', failing_save)
    with pytest.raises(CheckpointError) as error:
        manager.apply_patch(patch('*** Update File: a\n@@\n-old\n+new',
                                  '*** Update File: b\n@@\n-old\n+new'))
    assert manager.records[-1]['status'] == 'incomplete'
    if failure == 'rollback':
        assert 'rollback incomplete: a' in str(error.value)
        assert (root / 'a').read_bytes() == b'new\n'
    else:
        assert (root / 'a').read_bytes() == b'old\n'
    assert (root / 'b').read_bytes() == b'old\n'
    assert not list(root.glob('.one-*'))
    manager.end_task()
    with pytest.raises(CheckpointError):
        manager.prepare_undo('task', ['a'])


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_patch_parent_swap_after_staging(setup, tmp_path, monkeypatch):
    manager, root = setup
    parent = root / 'dir'
    parent.mkdir()
    (parent / 'a').write_bytes(b'old\n')
    manager.begin_task('task')
    fsync = os.fsync
    moved = tmp_path / 'moved'

    def swap(fd):
        fsync(fd)
        if parent.exists() and list(parent.glob('.one-write-*')) and not moved.exists():
            parent.rename(moved)
            parent.mkdir()
            (parent / 'a').write_bytes(b'old\n')

    monkeypatch.setattr(os, 'fsync', swap)
    with pytest.raises(CheckpointError):
        manager.apply_patch(patch('*** Update File: dir/a\n@@\n-old\n+new'))
    assert (parent / 'a').read_bytes() == b'old\n'
    assert (moved / 'a').read_bytes() == b'old\n'
    assert not list(moved.glob('.one-*'))
    assert manager.records[-1]['status'] == 'incomplete'


@pytest.mark.parametrize('when', ['between_calls', 'prepare', 'confirm'])
@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_identical_content_inode_replacement_is_conflict(setup, when):
    manager, root = setup
    manager.begin_task('task')
    manager.replace_file('a', b'agent')
    if when != 'between_calls':
        manager.end_task()
    if when == 'confirm':
        token = manager.prepare_undo('task', ['a'])['token']
    (root / 'external').write_bytes(b'agent')
    (root / 'external').chmod((root / 'a').stat().st_mode & 0o777)
    (root / 'external').replace(root / 'a')
    with pytest.raises(CheckpointError):
        if when == 'between_calls':
            manager.replace_file('a', b'again')
        elif when == 'prepare':
            manager.prepare_undo('task', ['a'])
        else:
            manager.confirm_undo(token)
    assert (root / 'a').read_bytes() == b'agent'


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_noop_and_invalid_replace_input(setup):
    manager, root = setup
    (root / 'a').write_bytes(b'same')
    manager.begin_task('task')
    manager.replace_file('a', b'same')
    with pytest.raises(CheckpointError):
        manager.replace_file('a', None)
    assert manager.records[-1]['paths'] == {}
    assert (root / 'a').read_bytes() == b'same'


@pytest.mark.parametrize('kind', ['fifo', 'hardlink', 'public'])
def test_manifest_safety_rechecked_before_save(setup, tmp_path, kind):
    manager, root = setup
    manager.begin_task('task')
    path = tmp_path / 'private' / manager.file
    if kind == 'fifo':
        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == 'hardlink':
        os.link(path, tmp_path / 'manifest-link')
    else:
        path.chmod(0o644)
    with pytest.raises(CheckpointError, match='Unsafe checkpoint'):
        manager.replace_file('a', b'not written')
    assert not (root / 'a').exists()


def test_reject_storage_inside_workspace_and_symlink_ancestor(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    with pytest.raises(CheckpointError, match='outside'):
        CheckpointManager(workspace / 'private', workspace, 's')
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'link').symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        CheckpointManager(tmp_path / 'link' / 'private', workspace, 's')
    assert not (real / 'private').exists()


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_token_expiration_and_new_task_invalidation(setup, monkeypatch):
    manager, _ = setup
    manager.begin_task('one')
    manager.replace_file('a', b'new')
    manager.end_task()
    token = manager.prepare_undo('one', ['a'])['token']
    monkeypatch.setattr('one.core.checkpoints.time.monotonic', lambda: float('inf'))
    with pytest.raises(CheckpointError, match='expired'):
        manager.confirm_undo(token)
    token = manager.prepare_undo('one', ['a'])['token']
    manager.begin_task('two')
    with pytest.raises(CheckpointError):
        manager.confirm_undo(token)


def test_patch_quota_and_output_size_preflight(setup, monkeypatch):
    manager, root = setup
    manager.begin_task('task')
    monkeypatch.setattr(manager, 'MAX_FILE', 4)
    with pytest.raises(CheckpointError):
        manager.apply_patch(patch('*** Add File: first\n+x', '*** Add File: second\n+oversized'))
    assert not list(root.iterdir())
    assert manager.records[-1]['paths'] == {}
    monkeypatch.setattr(manager, 'MAX_STORAGE', 700)
    with pytest.raises(CheckpointError):
        manager.apply_patch(patch('*** Add File: a\n+x', '*** Add File: b\n+y'))
    assert not list(root.iterdir())


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_explicit_delete_is_durable_and_does_not_touch_workspace(setup, tmp_path):
    manager, root = setup
    manager.begin_task('one')
    manager.replace_file('a', b'keep')
    manager.end_task()
    manager.begin_task('two')
    with pytest.raises(CheckpointError):
        manager.delete_checkpoint('one')
    manager.end_task()
    manager.delete_checkpoint('one')
    reopened = CheckpointManager(tmp_path / 'private', root, 'session')
    try:
        assert [r['id'] for r in reopened.list_checkpoints()] == ['two']
        assert (root / 'a').read_bytes() == b'keep'
        assert (tmp_path / 'private').stat().st_mode & 0o777 == 0o700
        assert (tmp_path / 'private' / manager.file).stat().st_mode & 0o777 == 0o600
    finally:
        reopened.close()


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_interrupted_patch_never_auto_restores(setup, tmp_path):
    manager, root = setup
    (root / 'a').write_bytes(b'old')
    manager.begin_task('interrupted')
    manager.capture_preimage(['a'])
    (root / 'a').write_bytes(b'partial')
    reopened = CheckpointManager(tmp_path / 'private', root, 'session')
    try:
        assert reopened.list_checkpoints()[0]['status'] == 'incomplete'
        assert (root / 'a').read_bytes() == b'partial'
        with pytest.raises(CheckpointError):
            reopened.prepare_undo('interrupted', ['a'])
    finally:
        reopened.close()


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_undo_staging_conflict_blocks_entire_selection(setup, monkeypatch):
    manager, root = setup
    for path in ('a', 'b'):
        (root / path).write_bytes(b'old')
    manager.begin_task('task')
    for path in ('a', 'b'):
        manager.replace_file(path, b'new')
    manager.end_task()
    token = manager.prepare_undo('task', ['a', 'b'])['token']
    original = os.fsync
    changed = False

    def change(fd):
        nonlocal changed
        original(fd)
        if not changed and list(root.glob('.one-undo-*')):
            changed = True
            (root / 'b').write_bytes(b'external')

    monkeypatch.setattr(os, 'fsync', change)
    with pytest.raises(CheckpointError, match='Undo incomplete'):
        manager.confirm_undo(token)
    assert (root / 'a').read_bytes() == b'new'
    assert (root / 'b').read_bytes() == b'external'


def test_storage_permission_change_blocks_mutation(setup, tmp_path):
    manager, root = setup
    manager.begin_task('task')
    (tmp_path / 'private').chmod(0o755)
    with pytest.raises(CheckpointError, match='private'):
        manager.replace_file('a', b'new')
    assert not (root / 'a').exists()


@pytest.mark.parametrize('kind', ['symlink', 'parent_symlink', 'hardlink', 'fifo', 'setuid', 'xattr'])
def test_patch_unsupported_targets_fail_before_mutation(setup, tmp_path, kind):
    manager, root = setup
    (root / 'a').write_bytes(b'old\n')
    path = 'a'
    if kind == 'symlink':
        (root / 'link').symlink_to(root / 'a')
        path = 'link'
    elif kind == 'parent_symlink':
        directory = tmp_path / 'outside'
        directory.mkdir()
        (directory / 'a').write_bytes(b'old\n')
        (root / 'link').symlink_to(directory, target_is_directory=True)
        path = 'link/a'
    elif kind == 'hardlink':
        os.link(root / 'a', root / 'alias')
    elif kind == 'fifo':
        (root / 'a').unlink()
        os.mkfifo(root / 'a')
    elif kind == 'setuid':
        (root / 'a').chmod(0o4600)
    elif kind == 'xattr':
        if not hasattr(os, 'setxattr'):
            pytest.skip('setting xattrs is unavailable')
        os.setxattr(root / 'a', 'user.checkpoint-test', b'unsupported')
    manager.begin_task('task')
    with pytest.raises(CheckpointError):
        manager.apply_patch(patch('*** Add File: first\n+new', f'*** Update File: {path}\n@@\n-old\n+new'))
    assert not (root / 'first').exists()
    assert manager.records[-1]['paths'] == {}


@pytest.mark.skipif(not hasattr(os, "listxattr"), reason="checkpoint transactions require descriptor xattr enumeration")
def test_edit_rejects_invalid_utf8_without_loss(setup):
    manager, root = setup
    (root / 'a').write_bytes(b'\xffold')
    manager.begin_task('task')
    with pytest.raises(UnicodeError):
        manager.edit_file('a', [{'oldString': 'old', 'newString': 'new'}])
    assert (root / 'a').read_bytes() == b'\xffold'


def test_missing_xattr_enumeration_fails_closed(setup, monkeypatch):
    manager, root = setup
    path = root / 'a'
    path.write_bytes(b'original')
    manager.begin_task('task')
    monkeypatch.delattr(os, 'listxattr', raising=False)
    before = sorted((p.relative_to(root), p.read_bytes()) for p in root.rglob('*') if p.is_file())
    with pytest.raises(CheckpointError, match='Unsupported extended metadata'):
        manager.capture_preimage(['a'])
    assert path.read_bytes() == b'original'
    assert sorted((p.relative_to(root), p.read_bytes()) for p in root.rglob('*') if p.is_file()) == before
    assert manager.records[-1]['paths'] == {}
