"""Private, session-scoped checkpoint engine for opt-in file tool recording.

Callers must serialize operations and enforce policy before using this engine.
Only the descriptor transaction methods make mutations safe; capture_preimage /
record_postimage are low-level bookkeeping, not a wrapper for unchecked tools.
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import secrets
import stat
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX implementation only
    fcntl = None


class CheckpointError(Exception):
    """A checkpoint cannot be safely recorded or restored."""


class CheckpointManager:
    MAX_FILE = 1024 * 1024
    MAX_TOTAL = 10 * MAX_FILE
    MAX_PATHS = 100
    MAX_STORAGE = 50 * MAX_FILE
    MAX_DIFF = 64 * 1024

    def __init__(self, storage: Path, workspace: Path, session_id: str):
        if (fcntl is None or not all(hasattr(os, name) for name in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_NONBLOCK'))
                or not all(fn in os.supports_dir_fd for fn in (os.open, os.stat, os.unlink, os.rename))):
            raise CheckpointError("Safe descriptor traversal is unavailable")
        if not isinstance(session_id, str) or not session_id:
            raise CheckpointError('A non-empty session ID is required')
        self.session_id = session_id
        self.workspace = Path(os.path.abspath(workspace))
        self.root = self._open_directory(self.workspace)
        self.storage_fd = None
        try:
            root_stat = os.fstat(self.root)
            self.identity = [root_stat.st_dev, root_stat.st_ino]
            self.storage = Path(os.path.abspath(storage))
            if self.storage.is_relative_to(self.workspace):
                raise CheckpointError("Checkpoint storage must be outside the workspace")
            parent = self._open_directory(self.storage.parent)
            try:
                try:
                    os.mkdir(self.storage.name, mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
                self.storage_fd = os.open(self.storage.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                          dir_fd=parent)
            finally:
                os.close(parent)
            info = os.fstat(self.storage_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CheckpointError("Checkpoint storage must be a private owned directory")
            self.file = hashlib.sha256(session_id.encode()).hexdigest() + '.json'
            self.tokens: dict[str, tuple[str, tuple[str, ...], float]] = {}
            self.active: str | None = None
            self._revision = None
            self.records = self._load()
            changed = False
            for record in self.records:
                if record['status'] in ('active', 'intent', 'undoing'):
                    record['status'] = 'incomplete'
                    changed = True
            if changed:
                self._save()
        except BaseException:
            if self.storage_fd is not None:
                os.close(self.storage_fd)
            os.close(self.root)
            raise

    @staticmethod
    def _open_directory(path: Path) -> int:
        """Open every component without following symlinks, including ancestors."""
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _private_file(fd: int) -> None:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CheckpointError('Unsafe checkpoint file')

    def close(self) -> None:
        if self.root is not None:
            os.close(self.root)
            self.root = None
        if self.storage_fd is not None:
            os.close(self.storage_fd)
            self.storage_fd = None
        self.tokens.clear()

    def _load(self) -> list[dict]:
        try:
            fd = os.open(self.file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.storage_fd)
        except FileNotFoundError:
            return []
        try:
            self._private_file(fd)
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(self.MAX_STORAGE + 1)
            if len(raw) > self.MAX_STORAGE:
                raise CheckpointError('Checkpoint storage limit exceeded')
            self._revision = hashlib.sha256(raw).digest()
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or set(envelope) != {'payload', 'sha256'}:
                raise ValueError('envelope')
            payload = envelope['payload']
            if not isinstance(payload, dict) or set(payload) != {'version', 'session', 'workspace', 'records'}:
                raise ValueError('payload')
            encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
            if envelope['sha256'] != hashlib.sha256(encoded).hexdigest():
                raise CheckpointError('Corrupt checkpoint manifest')
            if type(payload['version']) is not int:
                raise ValueError('version')
            if payload['session'] != self.session_id or payload['workspace'] != self.identity or payload['version'] != 2:
                raise CheckpointError('Checkpoint session or workspace mismatch')
            records = payload['records']
            if not isinstance(records, list):
                raise ValueError('records')
            self._validate_records(records)
            return records
        except (ValueError, KeyError, TypeError) as exc:
            raise CheckpointError('Corrupt checkpoint manifest') from exc
        finally:
            os.close(fd)

    def _save(self, reserve: int = 0) -> None:
        info = os.fstat(self.storage_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CheckpointError('Checkpoint storage must remain private and owned')
        try:
            self._validate_records(self.records)
        except (ValueError, KeyError, TypeError) as exc:
            raise CheckpointError('Corrupt checkpoint data') from exc
        payload = {'version': 2, 'session': self.session_id, 'workspace': self.identity, 'records': self.records}
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        data = json.dumps({'payload': payload, 'sha256': hashlib.sha256(encoded).hexdigest()}).encode()
        if len(data) + reserve > self.MAX_STORAGE:
            raise CheckpointError('Checkpoint storage limit exceeded')
        lock_name = self.file + '.lock'
        lock = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.storage_fd)
        try:
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CheckpointError('Unsafe checkpoint lock')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CheckpointError('Checkpoint storage is busy; retry when idle') from exc
            try:
                current = os.open(self.file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.storage_fd)
            except FileNotFoundError:
                revision = None
            else:
                try:
                    self._private_file(current)
                    with os.fdopen(current, 'rb', closefd=False) as stream:
                        revision = hashlib.sha256(stream.read(self.MAX_STORAGE + 1)).digest()
                finally:
                    os.close(current)
            if revision != self._revision:
                raise CheckpointError('Checkpoint storage changed concurrently; reopen manager')
            self._write_manifest(data)
            self._revision = hashlib.sha256(data).digest()
        finally:
            os.close(lock)

    def _write_manifest(self, data: bytes) -> None:
        name = '.checkpoint-' + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.storage_fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.file, src_dir_fd=self.storage_fd, dst_dir_fd=self.storage_fd)
            os.fsync(self.storage_fd)
        finally:
            try:
                os.unlink(name, dir_fd=self.storage_fd)
            except FileNotFoundError:
                pass

    @staticmethod
    def _valid_path(path: object) -> bool:
        return (isinstance(path, str) and bool(path) and not path.startswith('/')
                and '\x00' not in path and all(p not in ('', '.', '..') for p in path.split('/')))

    def _validate_image(self, image: object) -> None:
        if not isinstance(image, dict) or type(image.get('exists')) is not bool:
            raise ValueError('image')
        parent = image.get('parent')
        if (not isinstance(parent, list) or len(parent) != 2
                or any(type(n) is not int or n < 0 for n in parent)):
            raise ValueError('parent identity')
        if not image['exists']:
            if set(image) != {'exists', 'parent'}:
                raise ValueError('image')
            return
        if set(image) != {'exists', 'mode', 'data', 'size', 'sha256', 'identity', 'parent'}:
            raise ValueError('image')
        if (type(image['mode']) is not int or not 0 <= image['mode'] <= 0o777
                or type(image['size']) is not int or not 0 <= image['size'] <= self.MAX_FILE
                or not isinstance(image['data'], str) or not isinstance(image['sha256'], str)):
            raise ValueError('image')
        if (not isinstance(image['identity'], list) or len(image['identity']) != 2
                or any(type(n) is not int or n < 0 for n in image['identity'])):
            raise ValueError('identity')
        data = base64.b64decode(image['data'], validate=True)
        if len(data) != image['size'] or hashlib.sha256(data).hexdigest() != image['sha256']:
            raise ValueError('image hash')

    def _validate_records(self, records: list) -> None:
        ids = set()
        for record in records:
            if not isinstance(record, dict) or set(record) not in (
                {'id', 'status', 'paths', 'pairs'}, {'id', 'status', 'paths', 'pairs', 'pending'}
            ):
                raise ValueError('record')
            task_id = record['id']
            if not isinstance(task_id, str) or not task_id or task_id in ids:
                raise ValueError('id')
            ids.add(task_id)
            if record['status'] not in ('active', 'intent', 'undoing', 'incomplete', 'complete', 'undone'):
                raise ValueError('status')
            paths = record['paths']
            if not isinstance(paths, dict) or len(paths) > self.MAX_PATHS:
                raise ValueError('paths')
            for path, entry in paths.items():
                if not self._valid_path(path) or not isinstance(entry, dict) or set(entry) != {'pre', 'post', 'undone'}:
                    raise ValueError('entry')
                if type(entry['undone']) is not bool:
                    raise ValueError('undone')
                self._validate_image(entry['pre'])
                self._validate_image(entry['post'])
            if sum(entry['pre'].get('size', 0) for entry in paths.values()) > self.MAX_TOTAL:
                raise ValueError('preimage limit')
            pairs = record['pairs']
            if not isinstance(pairs, list) or any(not isinstance(pair, list) or len(pair) != 2
                    or any(not isinstance(p, str) or p not in paths for p in pair) or pair[0] == pair[1]
                    for pair in pairs):
                raise ValueError('pairs')
            if 'pending' in record:
                pending = record['pending']
                if (not isinstance(pending, list) or not pending
                        or any(not isinstance(p, str) or p not in paths for p in pending)
                        or len(pending) != len(set(pending))):
                    raise ValueError('pending')
            if record['status'] == 'intent' and 'pending' not in record:
                raise ValueError('missing intent')
            if record['status'] in ('active', 'complete', 'undone', 'undoing') and 'pending' in record:
                raise ValueError('unexpected intent')
            if record['status'] == 'undone' and any(not e['undone'] for e in paths.values()):
                raise ValueError('undone state')

    def _parent(self, path: str) -> tuple[int, str]:
        parts = path.split('/')
        if not self._valid_path(path):
            raise CheckpointError('Invalid relative path')
        try:
            current = self._open_directory(self.workspace)
            try:
                info = os.fstat(current)
                if [info.st_dev, info.st_ino] != self.identity:
                    raise CheckpointError('Workspace identity changed')
            finally:
                os.close(current)
        except OSError as exc:
            raise CheckpointError('Workspace identity changed') from exc
        fd = os.dup(self.root)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd, parts[-1]
        except OSError as exc:
            os.close(fd)
            raise CheckpointError(f'Unsafe path: {path}') from exc

    def _image(self, path: str) -> dict:
        parent, name = self._parent(path)
        parent_stat = os.fstat(parent)
        parent_id = [parent_stat.st_dev, parent_stat.st_ino]
        try:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                return {'exists': False, 'parent': parent_id}
            except OSError as exc:
                raise CheckpointError(f'Unsafe path: {path}') from exc
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > self.MAX_FILE
                        or stat.S_IMODE(info.st_mode) & ~0o777):
                    raise CheckpointError(f'Unsupported file or size: {path}')
                if info.st_uid != os.getuid() or info.st_gid != os.getgid():
                    raise CheckpointError(f'Unsupported ownership: {path}')
                if not hasattr(os, 'listxattr') or os.listxattr(fd):
                    raise CheckpointError(f'Unsupported extended metadata: {path}')
                data = b''
                while len(data) <= self.MAX_FILE:
                    chunk = os.read(fd, min(65536, self.MAX_FILE + 1 - len(data)))
                    if not chunk:
                        break
                    data += chunk
                after = os.fstat(fd)
                try:
                    linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except OSError as exc:
                    raise CheckpointError(f'File changed while reading: {path}') from exc
                def signature(st):
                    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                if signature(info) != signature(after) or signature(after) != signature(linked) or len(data) != info.st_size:
                    raise CheckpointError(f'File changed while reading: {path}')
                if len(data) > self.MAX_FILE:
                    raise CheckpointError(f'File too large: {path}')
                return {'exists': True, 'mode': stat.S_IMODE(info.st_mode),
                        'data': base64.b64encode(data).decode(), 'size': len(data),
                        'sha256': hashlib.sha256(data).hexdigest(), 'identity': [info.st_dev, info.st_ino], 'parent': parent_id}
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    def begin_task(self, task_id: str) -> None:
        if not isinstance(task_id, str) or not task_id or self.active is not None or any(r['id'] == task_id for r in self.records):
            raise CheckpointError('Task already active or duplicate task ID')
        self.tokens.clear()
        self.records.append({'id': task_id, 'status': 'active', 'paths': {}, 'pairs': []})
        self.active = task_id
        try:
            self._save()
        except Exception:
            self.records.pop()
            self.active = None
            raise

    def _active(self) -> dict:
        if self.active is None:
            raise CheckpointError('No active task')
        return next(r for r in self.records if r['id'] == self.active)

    def capture_preimage(self, paths: list[str], moves: list[tuple[str, str]] | None = None) -> None:
        record = self._active()
        if record['status'] != 'active' or len(paths) != len(set(paths)) or not paths:
            raise CheckpointError('Invalid mutation or pending intent')
        pairs = moves or []
        if any(a == b or a not in paths or b not in paths for a, b in pairs):
            raise CheckpointError('Invalid move pair')
        images = {p: self._image(p) for p in paths}
        for path, image in images.items():
            if path in record['paths'] and record['paths'][path]['post'] != image:
                record['status'] = 'incomplete'
                self._save()
                raise CheckpointError(f'Intervening change: {path}')
        if len(set(record['paths']) | set(paths)) > self.MAX_PATHS:
            raise CheckpointError('Too many paths')
        additions = {p: {'pre': image, 'post': image, 'undone': False} for p, image in images.items() if p not in record['paths']}
        total = sum(x['pre'].get('size', 0) for x in list(record['paths'].values()) + list(additions.values()))
        if total > self.MAX_TOTAL:
            raise CheckpointError('Preimage limit exceeded')
        old = json.loads(json.dumps(record))
        record['paths'].update(additions)
        record['pairs'].extend([list(pair) for pair in pairs])
        record['pending'] = paths[:]
        record['status'] = 'intent'
        # Reserve for worst-case encoded postimages before the caller may mutate.
        reserve = sum(max(0, ((self.MAX_FILE + 2) // 3) * 4 + 256
                          - len(json.dumps(record['paths'][p]['post']))) for p in paths)
        try:
            self._save(reserve=reserve)
        except Exception:
            record.clear()
            record.update(old)
            raise

    def apply_patch(self, patchText: str) -> list[str]:
        """Apply strict OpenCode Add/Update/Delete/Move sections as one transaction.

        Paths must be canonical workspace-relative names; parents must exist.
        Returns affected paths (both move endpoints). No file bytes are returned.
        """
        from one.tools.apply_patch import _parse_hunks, _validate_hunks

        if not isinstance(patchText, str) or len(patchText.encode()) > self.MAX_STORAGE:
            raise CheckpointError('Invalid patch or patch size limit exceeded')
        lines = patchText.replace('\r\n', '\n').splitlines()
        if len(lines) < 3 or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
            raise CheckpointError('Invalid patch envelope or empty patch')
        outputs = {}
        expected = {}
        moves = []
        i = 1
        while i < len(lines) - 1:
            header = lines[i]
            kind = next((k for k in ('Add', 'Update', 'Delete') if header.startswith(f'*** {k} File: ')), None)
            if kind is None:
                raise CheckpointError('Invalid patch section')
            path = header.split(': ', 1)[1]
            i += 1
            dest = None
            if kind == 'Update' and i < len(lines) - 1 and lines[i].startswith('*** Move to: '):
                dest = lines[i][len('*** Move to: '):]
                i += 1
            endpoints = [path] if dest is None else [path, dest]
            for name in endpoints:
                if not self._valid_path(name) or name in expected:
                    raise CheckpointError('Invalid or duplicate patch path')
                expected[name] = self._image(name)
            image = expected[path]
            if kind == 'Add' and image['exists'] or kind != 'Add' and not image['exists']:
                raise CheckpointError(f'Patch existence conflict: {path}')
            if dest is not None and expected[dest]['exists']:
                raise CheckpointError(f'Move destination exists: {dest}')
            body = []
            while i < len(lines) - 1 and not lines[i].startswith('*** '):
                body.append(lines[i])
                i += 1
            if kind == 'Delete':
                if body:
                    raise CheckpointError('Delete section must not contain hunks')
                outputs[path] = None
            elif kind == 'Add':
                if any(not line.startswith('+') for line in body):
                    raise CheckpointError('Invalid Add section')
                outputs[path] = ('\n'.join(line[1:] for line in body) + ('\n' if body else '')).encode()
            else:
                try:
                    if not body or not body[0].startswith('@@'):
                        raise ValueError('Missing hunks')
                    old = base64.b64decode(image['data']).decode('utf-8')
                    new = _validate_hunks(_parse_hunks(body), old, path).encode('utf-8')
                except (ValueError, UnicodeError):
                    raise CheckpointError(f'Patch hunk validation failed: {path}') from None
                outputs[path] = new if dest is None else None
                if dest is not None:
                    outputs[dest] = new
                    moves.append((path, dest))
        self._transaction(outputs, expected, moves)
        return list(outputs)

    def _transaction(self, outputs: dict[str, bytes | None], expected: dict[str, dict],
                     moves: list[tuple[str, str]]) -> None:
        """Stage all outputs and rollback preimages on held, revalidated parents.

        Failed transactions remain incomplete even after successful rollback.
        There is no atomic multi-file CAS against external filesystem writers.
        """
        if not outputs or len(outputs) > self.MAX_PATHS:
            raise CheckpointError('Empty transaction or too many paths')
        if any(data is not None and (not isinstance(data, bytes) or len(data) > self.MAX_FILE)
               for data in outputs.values()):
            raise CheckpointError('Unsupported output size or type')
        record = self._active()
        if record['status'] != 'active':
            raise CheckpointError('Task is incomplete or has a pending intent')
        for path in outputs:
            if path in record['paths'] and record['paths'][path]['post'] != expected[path]:
                self.fail_mutation()
                raise CheckpointError(f'Intervening change: {path}')
        outputs = {p: data for p, data in outputs.items()
                   if data is None or not expected[p]['exists']
                   or base64.b64decode(expected[p]['data']) != data
                   or any(p in pair for pair in moves)}
        if not outputs:
            return
        handles = {}
        staged = {}
        backups = {}
        installed = {}
        predicted = {}
        intent = False

        def stage(path, data, mode):
            parent, _ = handles[path]
            name = '.one-write-' + secrets.token_hex(16)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    if os.fstat(stream.fileno()).st_gid != os.getgid():
                        raise CheckpointError('Unsupported inherited group')
                    os.fsync(stream.fileno())
            except BaseException:
                os.unlink(name, dir_fd=parent)
                raise
            return name

        def revalidate(path, image):
            parent, _ = handles[path]
            current, _ = self._parent(path)
            try:
                a, b = os.fstat(parent), os.fstat(current)
                if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                    raise CheckpointError(f'Parent changed: {path}')
                if self._image(path) != image:
                    raise CheckpointError(f'Intervening change: {path}')
            finally:
                os.close(current)

        try:
            for path in outputs:
                handles[path] = self._parent(path)
                revalidate(path, expected[path])
            self.capture_preimage(list(outputs), moves)
            intent = True
            for path in outputs:
                if self._active()['paths'][path]['post'] != expected[path]:
                    raise CheckpointError(f'Intervening change: {path}')
            for path, data in outputs.items():
                image = expected[path]
                revalidate(path, image)
                if image['exists']:
                    backups[path] = stage(path, base64.b64decode(image['data']), image['mode'])
                if data is not None:
                    mode = image.get('mode', 0o600)
                    source = next((a for a, b in moves if b == path), None)
                    if source is not None:
                        mode = expected[source]['mode']
                    staged[path] = stage(path, data, mode)
                    info = os.stat(staged[path], dir_fd=handles[path][0], follow_symlinks=False)
                    predicted[path] = {'exists': True, 'mode': mode, 'size': len(data),
                                       'data': base64.b64encode(data).decode(),
                                       'sha256': hashlib.sha256(data).hexdigest(),
                                       'identity': [info.st_dev, info.st_ino], 'parent': image['parent']}
                else:
                    predicted[path] = {'exists': False, 'parent': image['parent']}
            # Check the entire set again before the first mutation.
            for path in outputs:
                revalidate(path, expected[path])
            for path, data in outputs.items():
                revalidate(path, expected[path])
                parent, name = handles[path]
                if data is None:
                    os.unlink(name, dir_fd=parent)
                else:
                    os.replace(staged[path], name, src_dir_fd=parent, dst_dir_fd=parent)
                # Track a mutation even if fsync or postimage reading subsequently fails.
                installed[path] = predicted[path]
                if self._image(path) != predicted[path]:
                    raise CheckpointError(f'Intervening change: {path}')
                os.fsync(parent)
            self.record_postimage(predicted)
        except BaseException as exc:
            rollback_failed = []
            for path, image in reversed(list(installed.items())):
                try:
                    if image is None:
                        raise CheckpointError('Unobservable output')
                    revalidate(path, image)
                    parent, name = handles[path]
                    if expected[path]['exists']:
                        os.replace(backups[path], name, src_dir_fd=parent, dst_dir_fd=parent)
                    else:
                        os.unlink(name, dir_fd=parent)
                    os.fsync(parent)
                except (OSError, CheckpointError):
                    rollback_failed.append(path)
            if intent:
                for path in installed:
                    try:
                        self._active()['paths'][path]['post'] = self._image(path)
                    except (OSError, CheckpointError):
                        pass  # Intent and incomplete status remain authoritative.
            if intent or isinstance(exc, CheckpointError) and 'Intervening change' in str(exc):
                try:
                    self.fail_mutation()
                except Exception:
                    # Persisted intent still fails closed on restart.
                    self._active()['status'] = 'incomplete'
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            detail = '; rollback incomplete: ' + ', '.join(rollback_failed) if rollback_failed else ''
            reason = f': {exc}' if isinstance(exc, CheckpointError) else ''
            raise CheckpointError('Checkpoint transaction failed' + reason + detail) from exc
        finally:
            for path, (parent, _) in handles.items():
                try:
                    for temp in (staged.get(path), backups.get(path)):
                        if temp is not None:
                            try:
                                os.unlink(temp, dir_fd=parent)
                            except FileNotFoundError:
                                pass
                finally:
                    os.close(parent)

    def replace_file(self, path: str, data: bytes, expected_image: dict | None = None) -> None:
        """Replace/create a regular file via the descriptor transaction."""
        if not isinstance(data, bytes) or len(data) > self.MAX_FILE:
            raise CheckpointError('Unsupported output size or type')
        image = self._image(path)
        if expected_image is not None and image != expected_image:
            self.fail_mutation()
            raise CheckpointError(f'Intervening change: {path}')
        self._transaction({path: data}, {path: image}, [])

    def edit_file(self, path: str, edits: list[dict[str, str]]) -> tuple[str, str]:
        from one.tools.edit import apply_edits

        if not isinstance(edits, list) or not edits:
            raise ValueError('Edit tool input is invalid. edits must contain at least one replacement.')
        image = self._image(path)
        if not image['exists']:
            raise FileNotFoundError(f'File not found: {path}')
        old = base64.b64decode(image['data']).decode('utf-8')
        new = apply_edits(old, edits)
        self.replace_file(path, new.encode('utf-8'), expected_image=image)
        return old, new

    def delete_file(self, path: str) -> None:
        """Delete an existing regular file via the descriptor transaction."""
        image = self._image(path)
        if not image['exists']:
            raise CheckpointError(f'File not found: {path}')
        self._transaction({path: None}, {path: image}, [])

    def delete_checkpoint(self, task_id: str) -> None:
        """Explicitly discard a non-active checkpoint and invalidate undo tokens.

        This does not provide secure erasure of previously stored data.
        """
        record = next((r for r in self.records if r['id'] == task_id), None)
        if record is None:
            raise CheckpointError('Unknown checkpoint')
        if self.active is not None:
            raise CheckpointError('Cannot delete a checkpoint during an active task')
        index = self.records.index(record)
        self.records.pop(index)
        try:
            self._save()
        except BaseException:
            self.records.insert(index, record)
            raise
        self.tokens.clear()

    def record_postimage(self, expected_images: dict[str, dict] | None = None) -> None:
        record = self._active()
        if record['status'] != 'intent':
            raise CheckpointError('No pending mutation')
        try:
            images = {p: self._image(p) for p in record['pending']}
            if expected_images is not None and images != expected_images:
                raise CheckpointError('Intervening change while recording postimages')
            for path, image in images.items():
                record['paths'][path]['post'] = image
            del record['pending']
            record['status'] = 'active'
            self._save()
        except Exception:
            record['status'] = 'incomplete'
            self._save()
            raise

    def fail_mutation(self) -> None:
        record = self._active()
        record['status'] = 'incomplete'
        self._save()

    def end_task(self) -> None:
        record = self._active()
        if record['status'] == 'active':
            record['status'] = 'complete'
        else:
            record['status'] = 'incomplete'
        try:
            self._save()
        except BaseException:
            record['status'] = 'incomplete'
            raise
        finally:
            self.active = None

    def list_checkpoints(self) -> list[dict]:
        return [{'id': r['id'], 'status': r['status'], 'paths': [
            {'path': p, 'pre_size': v['pre'].get('size', 0), 'post_size': v['post'].get('size', 0),
             'undone': v['undone']} for p, v in r['paths'].items()]} for r in self.records]

    def diff(self, task_id: str, path: str) -> str:
        """Return a bounded UTF-8 diff of the stored images, never the live file.

        Binary images (NUL-containing or invalid UTF-8) are described by size
        and SHA-256 instead. Only completed checkpoints have reliable images.
        The returned string is at most MAX_DIFF UTF-8 bytes, including the
        truncation marker.
        """
        record = next((r for r in self.records if r['id'] == task_id), None)
        if record is None or record['status'] not in ('complete', 'undone'):
            raise CheckpointError('Unknown or incomplete checkpoint')
        if path not in record['paths']:
            raise CheckpointError('Unknown checkpoint path')
        entry = record['paths'][path]
        pre, post = entry['pre'], entry['post']
        raw = [base64.b64decode(image['data']) if image['exists'] else b'' for image in (pre, post)]
        try:
            if any(b'\x00' in data for data in raw):
                raise UnicodeError('binary')
            before, after = (data.decode('utf-8') for data in raw)
        except UnicodeError:
            def describe(image):
                if not image['exists']:
                    return 'absent'
                return f"size={image['size']} sha256={image['sha256']} mode={image['mode']:04o}"
            return f"Binary images: pre {describe(pre)}; post {describe(post)}\n"

        label = json.dumps(path, ensure_ascii=True)
        old_name = 'a/' + label if pre['exists'] else '/dev/null'
        new_name = 'b/' + label if post['exists'] else '/dev/null'
        marker = b'\n... diff truncated ...\n'
        output = bytearray()
        if pre['exists'] and post['exists'] and pre['mode'] != post['mode']:
            output.extend(f"Mode: {pre['mode']:04o} -> {post['mode']:04o}\n".encode())
        for line in difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                         fromfile=old_name, tofile=new_name):
            encoded = line.encode('utf-8')
            if len(output) + len(encoded) > self.MAX_DIFF:
                available = max(0, self.MAX_DIFF - len(marker))
                output.extend(encoded[:max(0, available - len(output))])
                return output[:available].decode('utf-8', errors='ignore') + marker.decode()
            output.extend(encoded)
        return output.decode('utf-8')

    def prepare_undo(self, task_id: str, paths: list[str]) -> dict:
        record = next((r for r in reversed(self.records)
                       if r['status'] != 'complete' or r['paths'] or r['pairs'] or 'pending' in r), None)
        if self.active is not None or record is None or record['id'] != task_id:
            raise CheckpointError('Only the latest task can be undone while idle')
        selected = set(paths)
        if record['status'] != 'complete' or not selected or len(selected) != len(paths):
            raise CheckpointError('Invalid undo selection')
        if any(p not in record['paths'] or record['paths'][p]['undone'] for p in selected):
            raise CheckpointError('Unknown or already undone path')
        if any((a in selected) != (b in selected) for a, b in record['pairs']):
            raise CheckpointError('Move endpoints must be selected together')
        try:
            self._validate_records(self.records)
        except (ValueError, KeyError, TypeError) as exc:
            raise CheckpointError('Corrupt checkpoint data') from exc
        for path in selected:
            if self._image(path) != record['paths'][path]['post']:
                raise CheckpointError(f'Conflict: {path}')
        token = secrets.token_urlsafe(32)
        self.tokens[token] = (task_id, tuple(sorted(selected)), time.monotonic() + 120)
        return {'token': token, 'paths': sorted(selected), 'expires_in': 120}

    def confirm_undo(self, token: str) -> list[str]:
        request = self.tokens.pop(token, None)
        if request is None or request[2] < time.monotonic():
            raise CheckpointError('Invalid or expired token')
        task_id, paths, _ = request
        # Reuse all selection and conflict validation; discard its newly issued token.
        fresh = self.prepare_undo(task_id, list(paths))
        self.tokens.pop(fresh['token'])
        record = next(r for r in self.records if r['id'] == task_id)
        record['status'] = 'undoing'
        self._save()
        restored = []
        staged = {}
        try:
            # Stage every preimage before replacing or deleting any target.
            for path in paths:
                pre = record['paths'][path]['pre']
                self._validate_image(pre)
                if not pre['exists']:
                    continue
                parent, _ = self._parent(path)
                tmp = '.one-undo-' + secrets.token_hex(16)
                try:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    staged[path] = (parent, tmp)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(base64.b64decode(pre['data'], validate=True))
                        os.fchmod(stream.fileno(), pre['mode'])
                        stream.flush()
                        os.fsync(stream.fileno())
                except BaseException:
                    if path not in staged:
                        os.close(parent)
                    raise
            for path in paths:
                current, _ = self._parent(path)
                try:
                    if path in staged:
                        a, b = os.fstat(staged[path][0]), os.fstat(current)
                        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                            raise CheckpointError(f'Parent changed: {path}')
                    if self._image(path) != record['paths'][path]['post']:
                        raise CheckpointError(f'Conflict: {path}')
                finally:
                    os.close(current)
            for path in paths:
                entry = record['paths'][path]
                if path in staged:
                    current, _ = self._parent(path)
                    try:
                        a, b = os.fstat(staged[path][0]), os.fstat(current)
                        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                            raise CheckpointError(f'Parent changed: {path}')
                    finally:
                        os.close(current)
                if self._image(path) != entry['post']:
                    raise CheckpointError(f'Conflict: {path}')
                parent, name = self._parent(path)
                try:
                    if path in staged:
                        source, tmp = staged[path]
                        # Ensure the staged parent is still the current parent.
                        a, b = os.fstat(source), os.fstat(parent)
                        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                            raise CheckpointError(f'Parent changed: {path}')
                        os.replace(tmp, name, src_dir_fd=source, dst_dir_fd=parent)
                    elif entry['post']['exists']:
                        os.unlink(name, dir_fd=parent)
                    os.fsync(parent)
                finally:
                    os.close(parent)
                entry['undone'] = True
                restored.append(path)
            record['status'] = 'complete' if any(not v['undone'] for v in record['paths'].values()) else 'undone'
            self._save()
            return restored
        except BaseException as exc:
            record['status'] = 'incomplete'
            try:
                self._save()
            except Exception:
                pass  # Durable undoing intent already disables retry after restart.
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise CheckpointError(f'Undo incomplete; restored: {", ".join(restored)}') from exc
        finally:
            for parent, tmp in staged.values():
                try:
                    os.unlink(tmp, dir_fd=parent)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(parent)
