"""Focused tests for optional compaction file rehydration."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import hashlib
import os

import pytest

from one.core.compaction.constraints import Constraint
from one.core.compaction.rehydrate import MAX_BYTES, rehydrate_files, required_blocks


def checkpoint(paths: str) -> str:
    return ("# Objective\nGoal\n# Constraints\nNone\n# Work State\n## Completed\nNone\n"
            "## Active\nWorking\n## Blocked\nNone\n# Next Move\nContinue\n# Relevant Files\n" + paths + "\n")


def run(tmp_path, paths, **kwargs):
    options = dict(estimate_tokens=lambda text: len(text), read_allowed=lambda: True,
                   available_tokens=12000, enabled=True)
    options.update(kwargs)
    return rehydrate_files(checkpoint(paths), str(tmp_path), **options)


def test_safe_paths_policy_and_binary(tmp_path):
    (tmp_path / 'ok').write_text('hello')
    (tmp_path / 'binary').write_bytes(b'a\0b')
    (tmp_path / 'large').write_bytes(b'x' * (MAX_BYTES + 1))
    (tmp_path / 'link').symlink_to(tmp_path / 'ok')
    (tmp_path / 'dirlink').symlink_to(tmp_path, target_is_directory=True)
    result = run(tmp_path, 'ok\n../x\n/etc/passwd\n.env\n.git/config\nlink\ndirlink/ok\nbinary\nlarge\nmissing')
    assert result.files == 1 and 'hello' in result.blocks[0]['text']
    assert result.skipped == {'unsafe': 4, 'unavailable': 2, 'binary': 1, 'oversize': 1} or result.skipped == {
        'unsafe': 5, 'unavailable': 2, 'binary': 1, 'oversize': 1}
    denied = run(tmp_path, 'ok', read_allowed=lambda: False)
    assert denied.files == 0 and denied.skipped == {'denied': 1}


def test_caps_dedupe_and_known_content(tmp_path):
    for i in range(7):
        (tmp_path / str(i)).write_text('data')
    paths = '\n'.join(map(str, range(7))) + '\n0'
    first = run(tmp_path, paths)
    assert first.files == 5 and first.skipped == {'budget': 2, 'duplicate': 1}
    second = run(tmp_path, paths, state=first.state)
    assert second.files == 5 and second.blocks == first.blocks
    third = run(tmp_path, paths, state=second.state)
    assert third.blocks == first.blocks
    denied = run(tmp_path, '0', available_tokens=1)
    assert not denied.state
    assert run(tmp_path, '0', state=denied.state).files == 1
    known = run(tmp_path, '0', known_digests={'0': hashlib.sha256(b'data').hexdigest()})
    assert known.files == 0 and known.skipped == {'known': 1}
    assert run(tmp_path, '0', available_tokens=1).files == 0
    assert run(tmp_path, '0', enabled=False).files == 0
    assert run(tmp_path, '0', available_tokens=0).files == 0


def test_formats_configuration_and_checkpoint_limit(tmp_path):
    for name in ('a', 'b', 'c'):
        (tmp_path / name).write_text('content')
    result = run(tmp_path, '- `a`\n* b\n`c`\na\ncat b\n- `b` extra', max_files=2)
    assert result.files == 2
    assert result.skipped == {'budget': 1, 'duplicate': 1, 'unsafe': 2}
    assert run(tmp_path, 'a', max_files=0).files == 0
    assert run(tmp_path, 'a', max_tokens=10).files == 0
    assert run(tmp_path, 'a', max_output_tokens=1).skipped == {'invalid_checkpoint': 1}
    long_checkpoint = checkpoint('a') + 'x' * 3900
    assert rehydrate_files(long_checkpoint, str(tmp_path), estimate_tokens=len, read_allowed=lambda: True,
                           available_tokens=12000, enabled=True, max_output_tokens=5000).files == 1
    assert run(tmp_path, 'a', known_contents=frozenset({'a'})).skipped == {'known': 1}


def test_policy_before_each_open_and_hard_caps(tmp_path, monkeypatch):
    for i in range(6):
        (tmp_path / str(i)).write_text('x')
    assert run(tmp_path, '\n'.join(map(str, range(6))), max_files=99, max_tokens=99999).files == 5
    calls = []
    real_open = os.open

    def tracked_open(*args, **kwargs):
        calls.append('open')
        return real_open(*args, **kwargs)

    # Preserve the capability check while observing opens.
    monkeypatch.setattr(os, 'open', tracked_open)
    monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {tracked_open})
    def policy():
        calls.append('policy')
        return len(calls) < 3

    result = run(tmp_path, '0', read_allowed=policy)
    assert result.skipped == {'denied': 1}
    assert calls == ['policy', 'open', 'policy']


def test_no_follow_on_swap_after_stat(tmp_path, monkeypatch):
    target = tmp_path / 'target'
    target.write_text('safe')
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.write_text('outside-secret')
    real_stat = os.stat

    def swap(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == 'target' and kwargs.get('dir_fd') is not None:
            target.unlink()
            target.symlink_to(outside)
        return info

    monkeypatch.setattr(os, 'stat', swap)
    result = run(tmp_path, 'target')
    assert result.files == 0 and result.skipped == {'unavailable': 1}


def test_required_blocks_preserve_provenance_and_fail_closed():
    ledger = (Constraint('Never delete', 7, 'abc'),)
    blocks = required_blocks(ledger, '1. Inspect', estimate_tokens=len, available_tokens=1000)
    assert len(blocks) == 2
    assert '[7 id=abc] Never delete' in blocks[0]['text']
    assert '1. Inspect' in blocks[1]['text']
    assert all(block['type'] == 'text' and 'role' not in block for block in blocks)
    assert required_blocks((), '', estimate_tokens=len, available_tokens=0) == ()
    with pytest.raises(ValueError):
        required_blocks(ledger, '1. Inspect', estimate_tokens=len, available_tokens=1)
