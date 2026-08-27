"""Tests for one.core.persistence and secure-file integration.

Covers:
- ensure_private_dir, atomic_write_text, append_private_text
- load_json_text_safe, ensure_private_file
- AuthStorage / SettingsManager / ModelRegistry integration
- SessionManager private dir/file mode
- run_mode report private dir/file
- Legacy migration mode tightening
- Locked auth/settings in-memory state not mutated
- append existing broad file mode tightening
- CLI stderr malformed-config warning
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.persistence import (
    append_private_text,
    atomic_write_text,
    ensure_private_dir,
    ensure_private_file,
    load_json_text_safe,
)
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


# ---------------------------------------------------------------------------
# ensure_private_dir
# ---------------------------------------------------------------------------


def test_ensure_private_dir_creates_tree(tmp_path: Path):
    p = tmp_path / "a" / "b" / "c"
    ensure_private_dir(p)
    assert p.is_dir()


def test_ensure_private_dir_tightens_new(tmp_path: Path):
    p = tmp_path / "secure_new"
    ensure_private_dir(p)
    if os.name == "posix":
        assert p.stat().st_mode & 0o777 == 0o700


def test_ensure_private_dir_tightens_existing(tmp_path: Path):
    """Issue #1: the exact target directory is always chmod 0700 on POSIX,
    even when it already existed."""
    p = tmp_path / "already_exists"
    p.mkdir()
    # Set a permissive mode first.
    if os.name == "posix":
        os.chmod(str(p), 0o755)
    ensure_private_dir(p)
    if os.name == "posix":
        assert p.stat().st_mode & 0o777 == 0o700


def test_ensure_private_dir_ignores_non_dir_parent(tmp_path: Path):
    """When the target path's parent is a file, mkdir raises."""
    f = tmp_path / "notadir"
    f.write_text("x", encoding="utf-8")
    with pytest.raises((FileExistsError, NotADirectoryError)):
        ensure_private_dir(f / "child")


# ---------------------------------------------------------------------------
# ensure_private_file
# ---------------------------------------------------------------------------


def test_ensure_private_file_tightens_dir_and_file(tmp_path: Path):
    p = tmp_path / "dir" / "f"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    if os.name == "posix":
        os.chmod(str(p.parent), 0o755)
        os.chmod(str(p), 0o644)
    ensure_private_file(p)
    if os.name == "posix":
        assert p.parent.stat().st_mode & 0o777 == 0o700
        assert p.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# atomic_write_text
# ---------------------------------------------------------------------------


def test_atomic_write_text_creates_and_overwrites(tmp_path: Path):
    p = tmp_path / "data.json"
    atomic_write_text(p, '{"a":1}')
    assert p.read_text(encoding="utf-8") == '{"a":1}'
    atomic_write_text(p, '{"b":2}')
    assert p.read_text(encoding="utf-8") == '{"b":2}'


def test_atomic_write_text_file_mode_on_posix(tmp_path: Path):
    p = tmp_path / "secret.txt"
    atomic_write_text(p, "shh")
    if os.name == "posix":
        assert p.stat().st_mode & 0o777 == 0o600


def test_atomic_write_text_parent_created_securely(tmp_path: Path):
    """Only the exact parent of the written file is tightened;
    intermediate dirs follow mkdir/umask."""
    p = tmp_path / "deep" / "nested" / "file.txt"
    atomic_write_text(p, "hello")
    assert p.is_file()
    if os.name == "posix":
        # The immediate parent (nested/) is the ensure_private_dir target.
        assert (tmp_path / "deep" / "nested").stat().st_mode & 0o777 == 0o700
        # Intermediate dir (deep/) may retain mkdir/umask mode.


def test_atomic_write_text_temp_cleaned_on_replace_failure(tmp_path: Path):
    """If os.replace fails (injected), temp must be cleaned up
    and the original file must be untouched."""
    p = tmp_path / "target.txt"
    atomic_write_text(p, "original")
    assert p.read_text(encoding="utf-8") == "original"
    # Inject os.replace to raise — the temp file should be cleaned.
    with mock.patch("os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError, match="boom"):
            atomic_write_text(p, "fail")
    # Original file is untouched.
    assert p.read_text(encoding="utf-8") == "original"
    # No leftover temp files in the parent directory.
    leftover = list(p.parent.glob(".tmp_*"))
    assert len(leftover) == 0


def test_atomic_write_text_content_not_corrupted_on_failure(tmp_path: Path):
    """Original content must survive an in-flight error."""
    p = tmp_path / "survive.txt"
    atomic_write_text(p, "original")
    with mock.patch("os.replace", side_effect=OSError("boom")):
        with pytest.raises(OSError):
            atomic_write_text(p, "replaced")
    assert p.read_text(encoding="utf-8") == "original"


def test_atomic_write_text_partial_os_write(tmp_path: Path):
    """If os.write returns a partial write, _write_all must retry until
    every byte is written."""
    import one.core.persistence as pmod
    original_write = os.write

    call_count = [0]

    def partial_write(fd: int, data: bytes) -> int:
        call_count[0] += 1
        if call_count[0] == 1:
            # First call: actually write 2 bytes, return 2 (partial).
            n = original_write(fd, data[:2])
            return n  # should be 2
        # Subsequent calls: delegate to real os.write for the remainder.
        return original_write(fd, data)

    saved_write = pmod.os.write
    pmod.os.write = partial_write  # type: ignore[assignment]
    try:
        p = tmp_path / "partial.txt"
        atomic_write_text(p, "hello world!")
        assert p.read_text(encoding="utf-8") == "hello world!"
        # Confirm the retry loop was exercised.
        assert call_count[0] >= 2
    finally:
        pmod.os.write = saved_write


# ---------------------------------------------------------------------------
# append_private_text
# ---------------------------------------------------------------------------


def test_append_private_text_appends(tmp_path: Path):
    p = tmp_path / "log.txt"
    append_private_text(p, "line1\n")
    append_private_text(p, "line2\n")
    assert p.read_text(encoding="utf-8") == "line1\nline2\n"


def test_append_private_text_creates_if_missing(tmp_path: Path):
    p = tmp_path / "new.log"
    assert not p.exists()
    append_private_text(p, "first\n")
    assert p.read_text(encoding="utf-8") == "first\n"


def test_append_private_text_file_mode_on_posix(tmp_path: Path):
    p = tmp_path / "perm.log"
    append_private_text(p, "data")
    if os.name == "posix":
        assert p.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# load_json_text_safe
# ---------------------------------------------------------------------------


def test_load_json_text_safe_valid(tmp_path: Path):
    p = tmp_path / "ok.json"
    p.write_text('{"a":1}', encoding="utf-8")
    data, err = load_json_text_safe(p)
    assert err is None
    assert data == {"a": 1}


def test_load_json_text_safe_malformed_returns_error(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{not json}", encoding="utf-8")
    data, err = load_json_text_safe(p)
    assert data is None
    assert err is not None
    # File is untouched.
    assert p.read_text(encoding="utf-8") == "{not json}"


# ---------------------------------------------------------------------------
# AuthStorage: error collection, locking, and atomic write integration
# ---------------------------------------------------------------------------


def test_auth_storage_loads_valid_json(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"apiKeys": {"openai": "file-key"}}), encoding="utf-8")
    auth = AuthStorage.create(str(auth_path))
    assert auth.get_api_key("openai") == "file-key"
    assert auth.drain_errors() == []


def test_auth_storage_malformed_json_collects_error(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{malformed!!!", encoding="utf-8")
    auth = AuthStorage.create(str(auth_path))
    errors = auth.drain_errors()
    assert len(errors) == 1
    assert errors[0]["scope"] == "auth"
    assert auth.get_api_key("openai") is None


def test_auth_storage_refuses_save_when_malformed(tmp_path: Path):
    """Issue #5: malformed auth file must not be silently overwritten."""
    auth_path = tmp_path / "auth.json"
    original_bytes = b"{CORRUPT!!!"
    auth_path.write_bytes(original_bytes)
    auth = AuthStorage.create(str(auth_path))
    errors = auth.drain_errors()
    assert len(errors) == 1
    with pytest.raises(RuntimeError, match="malformed"):
        auth.set_stored_api_key("openai", "key")
    # Original file is unchanged.
    assert auth_path.read_bytes() == original_bytes


def test_auth_storage_refuses_save_when_non_dict_json(tmp_path: Path):
    """Issue #6: valid JSON but not a dict is recorded and blocks save."""
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('[1, 2, 3]', encoding="utf-8")
    auth = AuthStorage.create(str(auth_path))
    errors = auth.drain_errors()
    assert any("not a JSON object" in str(e.get("error", "")) for e in errors)
    with pytest.raises(RuntimeError, match="malformed"):
        auth.set_stored_api_key("openai", "key")


def test_auth_storage_atomic_write_preserves_content(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    auth = AuthStorage.create(str(auth_path))
    auth.set_stored_api_key("anthropic", "sk-123")
    assert auth_path.exists()
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    assert data["apiKeys"]["anthropic"] == "sk-123"


def test_auth_storage_file_mode_after_write(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    auth = AuthStorage.create(str(auth_path))
    auth.set_stored_api_key("openai", "key")
    if os.name == "posix":
        assert auth_path.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# SettingsManager: agent_dir, locking, and project isolation
# ---------------------------------------------------------------------------


def test_settings_global_path_uses_agent_dir(tmp_path: Path):
    """SettingsManager global path must use agent_dir."""
    agent_dir = str(tmp_path / "my-agent")
    sm = SettingsManager(cwd=str(tmp_path), agent_dir=agent_dir)
    assert sm._global_path == Path(agent_dir) / "settings.json"


def test_settings_saves_global_to_agent_dir(tmp_path: Path):
    agent_dir = str(tmp_path / "agent")
    sm = SettingsManager(cwd=str(tmp_path), agent_dir=agent_dir)
    sm.set_default_provider("openai")
    settings_file = Path(agent_dir) / "settings.json"
    assert settings_file.exists()
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["defaultProvider"] == "openai"


def test_settings_malformed_global_collects_error(tmp_path: Path):
    agent_dir = str(tmp_path / "agent")
    settings_file = Path(agent_dir) / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("<<< not json >>>", encoding="utf-8")
    sm = SettingsManager(cwd=str(tmp_path), agent_dir=agent_dir)
    errors = sm.drain_errors()
    assert any(e["scope"] == "global" for e in errors)


def test_settings_project_malformed_collects_error(tmp_path: Path):
    project_file = tmp_path / ".one" / "settings.json"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text("[not an object", encoding="utf-8")
    sm = SettingsManager(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent"))
    errors = sm.drain_errors()
    assert any(e["scope"] == "project" for e in errors)


def test_settings_project_malformed_does_not_chmod_project_file(tmp_path: Path):
    """Issue #4: project scope must never be chmod'd — it is repo content."""
    project_file = tmp_path / ".one" / "settings.json"
    project_file.parent.mkdir(parents=True, exist_ok=True)
    # Set a specific repo-friendly mode (e.g. 0644).
    project_file.write_text("[not an object", encoding="utf-8")
    if os.name == "posix":
        os.chmod(str(project_file), 0o644)
    original_mode = project_file.stat().st_mode & 0o777
    sm = SettingsManager(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent"))
    if os.name == "posix":
        # Project file mode must remain unchanged (0644), not tightened to 0600.
        assert project_file.stat().st_mode & 0o777 == original_mode


def test_settings_refuses_save_global_when_malformed(tmp_path: Path):
    """Issue #5: malformed global settings must not be silently overwritten."""
    agent_dir = str(tmp_path / "agent")
    settings_file = Path(agent_dir) / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    original_bytes = b"<<<CORRUPT>>>"
    settings_file.write_bytes(original_bytes)
    sm = SettingsManager(cwd=str(tmp_path), agent_dir=agent_dir)
    errors = sm.drain_errors()
    assert any(e["scope"] == "global" for e in errors)
    with pytest.raises(RuntimeError, match="malformed"):
        sm.set_default_provider("openai")
    # Original file unchanged.
    assert settings_file.read_bytes() == original_bytes


def test_settings_non_dict_global_recorded_as_error(tmp_path: Path):
    """Issue #6: valid JSON but not a dict is recorded as recoverable error."""
    agent_dir = str(tmp_path / "agent")
    settings_file = Path(agent_dir) / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text('[1, 2]', encoding="utf-8")
    sm = SettingsManager(cwd=str(tmp_path), agent_dir=agent_dir)
    errors = sm.drain_errors()
    assert any("not a JSON object" in str(e.get("error", "")) for e in errors)


# ---------------------------------------------------------------------------
# ModelRegistry: error collection, locking, and atomic persist
# ---------------------------------------------------------------------------


def test_model_registry_malformed_models_collects_error(tmp_path: Path):
    models_path = tmp_path / "models.json"
    models_path.write_text("{broken!!", encoding="utf-8")
    auth = AuthStorage.in_memory()
    reg = ModelRegistry.create(auth, str(models_path))
    errors = reg.drain_errors()
    assert len(errors) == 1
    assert errors[0]["scope"] == "models"
    assert any(m.provider == "openai" and m.id == "gpt-4.1" for m in reg.all())


def test_model_registry_atomic_persist(tmp_path: Path):
    auth = AuthStorage.in_memory()
    models_path = tmp_path / "models.json"
    reg = ModelRegistry.create(auth, str(models_path))
    reg.persist_models("test", ["model-x"])
    assert models_path.exists()
    data = json.loads(models_path.read_text(encoding="utf-8"))
    assert data["providers"]["test"][0]["id"] == "model-x"


def test_model_registry_persist_file_mode_on_posix(tmp_path: Path):
    auth = AuthStorage.in_memory()
    models_path = tmp_path / "models.json"
    reg = ModelRegistry.create(auth, str(models_path))
    reg.persist_models("test", ["m1"])
    if os.name == "posix":
        assert models_path.stat().st_mode & 0o777 == 0o600


def test_model_registry_refuses_persist_when_malformed(tmp_path: Path):
    """Issue #5: persist_models must not catch malformed and overwrite."""
    models_path = tmp_path / "models.json"
    models_path.write_text("{CORRUPT!!!", encoding="utf-8")
    auth = AuthStorage.in_memory()
    reg = ModelRegistry.create(auth, str(models_path))
    errors = reg.drain_errors()
    assert len(errors) == 1
    with pytest.raises(RuntimeError, match="malformed"):
        reg.persist_models("test", ["m1"])
    # Original file unchanged.
    assert models_path.read_text(encoding="utf-8") == "{CORRUPT!!!"


def test_model_registry_non_dict_models_recorded_as_error(tmp_path: Path):
    """Issue #6: valid JSON array is recorded, not silently accepted."""
    models_path = tmp_path / "models.json"
    models_path.write_text('["list", "not", "object"]', encoding="utf-8")
    auth = AuthStorage.in_memory()
    reg = ModelRegistry.create(auth, str(models_path))
    errors = reg.drain_errors()
    assert any("not a JSON object" in str(e.get("error", "")) for e in errors)


def test_model_registry_persist_creates_file_with_mode_and_contents(tmp_path: Path):
    """persist_models on missing file creates 0600 file with valid JSON."""
    models_path = tmp_path / "models.json"
    assert not models_path.exists()
    auth = AuthStorage.in_memory()
    reg = ModelRegistry.create(auth, str(models_path))
    reg.persist_models("test", ["m1", "m2"])
    assert models_path.exists()
    if os.name == "posix":
        assert models_path.stat().st_mode & 0o777 == 0o600
    data = json.loads(models_path.read_text(encoding="utf-8"))
    assert data == {
        "providers": {
            "test": [{"id": "m1", "reasoning": True}, {"id": "m2", "reasoning": True}]
        }
    }


def test_model_registry_persist_refuses_non_dict_and_preserves_file(tmp_path: Path):
    """persist_models on valid non-dict JSON raises RuntimeError, file unchanged."""
    models_path = tmp_path / "models.json"
    original_text = '"just a string"'
    models_path.write_text(original_text, encoding="utf-8")
    auth = AuthStorage.in_memory()
    reg = ModelRegistry.create(auth, str(models_path))
    errors = reg.drain_errors()
    assert any("not a JSON object" in str(e.get("error", "")) for e in errors)
    with pytest.raises(RuntimeError, match="malformed"):
        reg.persist_models("test", ["m1"])
    # File remains unchanged.
    assert models_path.read_text(encoding="utf-8") == original_text


# ---------------------------------------------------------------------------
# AuthStorage: locked in-memory state not mutated
# ---------------------------------------------------------------------------


def test_auth_storage_locked_does_not_mutate_in_memory(tmp_path: Path):
    """Malformed auth + set key raises and get_api_key does not return key."""
    auth_path = tmp_path / "auth.json"
    original_bytes = b"{CORRUPT!!!"
    auth_path.write_bytes(original_bytes)
    auth = AuthStorage.create(str(auth_path))
    with pytest.raises(RuntimeError, match="malformed"):
        auth.set_stored_api_key("openai", "new-key")
    # In-memory state is unchanged — no key stored.
    assert auth.get_api_key("openai") is None
    assert auth.get_api_key("openai2") is None  # unchanged


def test_auth_storage_locked_oauth_not_mutated(tmp_path: Path):
    """Malformed auth + set oauth raises and in-memory state unchanged."""
    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{bad json", encoding="utf-8")
    auth = AuthStorage.create(str(auth_path))
    with pytest.raises(RuntimeError):
        auth.set_oauth_record("openai", {"access": "tok"})
    assert auth.get_oauth_record("openai") is None


# ---------------------------------------------------------------------------
# SettingsManager: locked in-memory state not mutated
# ---------------------------------------------------------------------------


def test_settings_manager_locked_does_not_mutate_global(tmp_path: Path):
    """Malformed global + setter raises and _global stays unchanged."""
    agent_dir = str(tmp_path / "agent")
    sf = Path(agent_dir) / "settings.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_bytes(b"<<<<<CORRUPT>>>>>")
    sm = SettingsManager(cwd=str(tmp_path), agent_dir=agent_dir)
    assert sm._global == {}  # defaults from malformed load
    with pytest.raises(RuntimeError, match="malformed"):
        sm.set_default_provider("openai")
    # Global state must still be empty — mutation was blocked.
    assert sm._global == {}


def test_settings_manager_locked_set_bash_show_output(tmp_path: Path):
    """Test through set_bash_show_output setter with persist=True."""
    agent_dir = str(tmp_path / "agent")
    sf = Path(agent_dir) / "settings.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_bytes(b"not json!!!")
    sm = SettingsManager(cwd=str(tmp_path), agent_dir=agent_dir)
    with pytest.raises(RuntimeError, match="malformed"):
        sm.set_bash_show_output(True, persist=True)
    # _global must be unchanged.
    assert sm._global == {}


def test_settings_manager_locked_set_bash_show_output_no_persist(tmp_path: Path):
    """persist=False bypasses the lock — in-memory change allowed."""
    agent_dir = str(tmp_path / "agent")
    sf = Path(agent_dir) / "settings.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_bytes(b"not json!!!")
    sm = SettingsManager(cwd=str(tmp_path), agent_dir=agent_dir)
    # persist=False should work in-memory.
    sm.set_bash_show_output(True, persist=False)
    assert sm._global.get("bash", {}).get("showOutput") is True
    # File was not touched.
    assert sf.read_bytes() == b"not json!!!"


# ---------------------------------------------------------------------------
# SessionManager: private dir/file mode
# ---------------------------------------------------------------------------


def test_session_manager_dir_mode_on_create(tmp_path: Path):
    """SessionManager create uses ensure_private_dir (0700 on POSIX)."""
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    if os.name == "posix":
        assert (tmp_path / "sessions").stat().st_mode & 0o777 == 0o700


def test_session_manager_file_mode_on_create(tmp_path: Path):
    """First session file uses atomic_write_text (0600 on POSIX)."""
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    assert sm.session_file is not None
    # create() sets the path but _new_session doesn't write; first append triggers write.
    sm.append_message({"role": "user", "content": "init"})
    sf = Path(sm.session_file)
    assert sf.exists()
    if os.name == "posix":
        assert sf.stat().st_mode & 0o777 == 0o600


def test_session_manager_append_tightens_existing_file_mode(tmp_path: Path):
    """append_private_text tightens an existing 0644 file to 0600."""
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    assert sm.session_file is not None
    sf = Path(sm.session_file)
    # First write triggers atomic_write_text.
    sm.append_message({"role": "user", "content": "init"})
    if os.name == "posix":
        sf.chmod(0o644)
    sm.append_message({"role": "user", "content": "hi"})
    if os.name == "posix":
        assert sf.stat().st_mode & 0o777 == 0o600


def test_session_manager_rewrite_atomic(tmp_path: Path):
    """Migration rewrite uses atomic_write_text (0600 on POSIX)."""
    session_file = tmp_path / "legacy.jsonl"
    lines = [
        json.dumps({"type": "session", "id": "s1", "timestamp": "2020-01-01T00:00:00Z", "cwd": str(tmp_path)}),
        json.dumps({"type": "message", "timestamp": "2020-01-01T00:00:01Z", "message": {"role": "hookMessage", "content": "x"}}),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name == "posix":
        session_file.chmod(0o644)
    sm = SessionManager.open(str(session_file), str(tmp_path))
    assert sm.get_header()["version"] == 4
    if os.name == "posix":
        # Migration rewrite uses atomic_write_text — should be 0600.
        assert session_file.stat().st_mode & 0o777 == 0o600


def test_session_manager_existing_file_tightened_on_open(tmp_path: Path):
    """Opening an existing 0644 session file tightens it to 0600."""
    session_file = tmp_path / "existing.jsonl"
    lines = [
        json.dumps({"type": "session", "id": "s1", "timestamp": "2024-01-01T00:00:00Z", "cwd": str(tmp_path)}),
        json.dumps({"type": "message", "id": "m1", "parentId": None, "timestamp": "2024-01-01T00:00:01Z", "message": {"role": "user", "content": "hi"}}),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name == "posix":
        session_file.chmod(0o644)
    sm = SessionManager.open(str(session_file), str(tmp_path))
    assert len(sm.get_entries()) == 1
    if os.name == "posix":
        assert session_file.stat().st_mode & 0o777 == 0o600


def test_session_manager_branch_rewrites_to_new_private_file(tmp_path: Path):
    """Branched session creates a new 0600 file."""
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_message({"role": "user", "content": "a"})
    sm.append_message({"role": "assistant", "content": "b"})
    target = sm.get_leaf_id()
    assert target is not None
    sm.create_branched_session(target)
    assert sm.session_file is not None
    new_file = Path(sm.session_file)
    if os.name == "posix":
        assert new_file.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# run_mode: report dir/file mode
# ---------------------------------------------------------------------------


def test_run_mode_report_dir_and_file_mode(tmp_path: Path, capsys):
    """run_mode creates report dir 0700 and reports.jsonl 0600."""
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.settings_manager import SettingsManager
    from one.modes.run_mode import run_run_mode

    class _Loader:
        def get_system_prompt(self, selected_tools=None):
            return "test"

    class _Provider:
        async def chat(self, api_key, model, messages, thinking_level, headers=None):
            from one.providers.base import ChatResult
            return ChatResult(text='{"tool":"finish","args":{"summary":"done","goal_success":true}}', raw={}, usage={}, stop_reason="stop")

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session = SessionManager.in_memory(str(tmp_path))
    agent = AgentSession(session, settings, registry, _Loader(), model, "medium")
    agent.providers = {"openai": _Provider()}

    class _MockHost:
        def __init__(self, s):
            self.session = s

    agent_dir = tmp_path / "agentdir"
    host = _MockHost(agent)

    import asyncio
    code = asyncio.run(run_run_mode(host, {"task": "go", "agentDir": str(agent_dir)}))
    assert code == 0

    if os.name == "posix":
        # Report dir is 0700.
        assert agent_dir.stat().st_mode & 0o777 == 0o700
        report = agent_dir / "reports.jsonl"
        assert report.exists()
        # Report file is 0600.
        assert report.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# Legacy migration mode tightening
# ---------------------------------------------------------------------------


def test_legacy_migration_tightens_agent_dir(tmp_path: Path):
    """Legacy migration creates and tightens the XDG agent dir to 0700."""
    from one.config import get_agent_dir

    home = tmp_path / "home"
    home.mkdir()
    legacy = home / ".one" / "agent"
    legacy.mkdir(parents=True)
    (legacy / "auth.json").write_text("{}", encoding="utf-8")
    (legacy / "settings.json").write_text("{}", encoding="utf-8")

    old_home = os.environ.get("HOME")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    try:
        os.environ["HOME"] = str(home)
        if "XDG_CONFIG_HOME" in os.environ:
            del os.environ["XDG_CONFIG_HOME"]
        xdg_dir = Path(get_agent_dir())
        assert xdg_dir.exists()
        if os.name == "posix":
            assert xdg_dir.stat().st_mode & 0o777 == 0o700
            # Sensitive files are 0600.
            assert (xdg_dir / "auth.json").stat().st_mode & 0o777 == 0o600
            assert (xdg_dir / "settings.json").stat().st_mode & 0o777 == 0o600
    finally:
        if old_home:
            os.environ["HOME"] = old_home
        elif "HOME" in os.environ:
            del os.environ["HOME"]
        if old_xdg:
            os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_legacy_migration_tightens_session_and_report_jsonl(tmp_path: Path):
    """Legacy migration also tightens session JSONL and report files to 0600."""
    from one.config import get_agent_dir

    home = tmp_path / "home"
    home.mkdir()
    legacy = home / ".one" / "agent"
    legacy.mkdir(parents=True)
    (legacy / "auth.json").write_text("{}", encoding="utf-8")
    legacy_sessions = legacy / "sessions"
    legacy_sessions.mkdir()
    (legacy_sessions / "2024-01-01.jsonl").write_text("{}", encoding="utf-8")
    if os.name == "posix":
        (legacy_sessions / "2024-01-01.jsonl").chmod(0o644)
    (legacy / "reports.jsonl").write_text("{}", encoding="utf-8")
    if os.name == "posix":
        (legacy / "reports.jsonl").chmod(0o644)

    old_home = os.environ.get("HOME")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    try:
        os.environ["HOME"] = str(home)
        if "XDG_CONFIG_HOME" in os.environ:
            del os.environ["XDG_CONFIG_HOME"]
        xdg_dir = Path(get_agent_dir())
        assert xdg_dir.exists()
        if os.name == "posix":
            # XDG copies of session and report files should be tightened.
            xdg_sessions = xdg_dir / "sessions"
            assert (xdg_sessions / "2024-01-01.jsonl").stat().st_mode & 0o777 == 0o600
            assert (xdg_dir / "reports.jsonl").stat().st_mode & 0o777 == 0o600
    finally:
        if old_home:
            os.environ["HOME"] = old_home
        elif "HOME" in os.environ:
            del os.environ["HOME"]
        if old_xdg:
            os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_legacy_migration_chmod_failure_returns_xdg(tmp_path: Path, monkeypatch):
    """Permission tightening failure after successful copy still returns XDG."""
    from one.config import get_agent_dir

    home = tmp_path / "home"
    home.mkdir()
    legacy = home / ".one" / "agent"
    legacy.mkdir(parents=True)
    (legacy / "auth.json").write_text("{}", encoding="utf-8")

    old_home = os.environ.get("HOME")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    try:
        os.environ["HOME"] = str(home)
        if "XDG_CONFIG_HOME" in os.environ:
            del os.environ["XDG_CONFIG_HOME"]

        # Inject a permission error during _tighten_migrated_state.
        original_ensure = None

        import one.config as config_mod

        original_ensure = config_mod._tighten_migrated_state

        def failing_tighten(agent_dir: Path) -> None:
            raise OSError("boom — permission denied")

        monkeypatch.setattr(config_mod, "_tighten_migrated_state", failing_tighten)

        # Still returns XDG and files remain.
        xdg_dir = Path(get_agent_dir())
        assert xdg_dir.exists()
        assert (xdg_dir / "auth.json").exists()
    finally:
        if old_home:
            os.environ["HOME"] = old_home
        elif "HOME" in os.environ:
            del os.environ["HOME"]
        if old_xdg:
            os.environ["XDG_CONFIG_HOME"] = old_xdg


def test_legacy_migration_nested_sessions_tightened(tmp_path: Path):
    """Nested session directories and JSONL files are recursively tightened."""
    from one.config import get_agent_dir

    home = tmp_path / "home"
    home.mkdir()
    legacy = home / ".one" / "agent"
    legacy.mkdir(parents=True)
    (legacy / "auth.json").write_text("{}", encoding="utf-8")

    # Nested session layout.
    nested = legacy / "sessions" / "project-a" / "sub"
    nested.mkdir(parents=True)
    (nested / "deep.jsonl").write_text("{}", encoding="utf-8")
    if os.name == "posix":
        nested.chmod(0o755)
        (nested / "deep.jsonl").chmod(0o644)
    # Also a top-level sessions dir.
    top_sessions = legacy / "sessions" / "top-level"
    top_sessions.mkdir(parents=True)
    (top_sessions / "top.jsonl").write_text("{}", encoding="utf-8")
    if os.name == "posix":
        top_sessions.chmod(0o755)
        (top_sessions / "top.jsonl").chmod(0o644)

    old_home = os.environ.get("HOME")
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    try:
        os.environ["HOME"] = str(home)
        if "XDG_CONFIG_HOME" in os.environ:
            del os.environ["XDG_CONFIG_HOME"]
        xdg_dir = Path(get_agent_dir())
        assert xdg_dir.exists()
        if os.name == "posix":
            # All nested session dirs should be 0700.
            xdg_nested = xdg_dir / "sessions" / "project-a" / "sub"
            assert xdg_nested.stat().st_mode & 0o777 == 0o700
            xdg_top = xdg_dir / "sessions" / "top-level"
            assert xdg_top.stat().st_mode & 0o777 == 0o700
            # All nested JSONL files should be 0600.
            assert (xdg_nested / "deep.jsonl").stat().st_mode & 0o777 == 0o600
            assert (xdg_top / "top.jsonl").stat().st_mode & 0o777 == 0o600
    finally:
        if old_home:
            os.environ["HOME"] = old_home
        elif "HOME" in os.environ:
            del os.environ["HOME"]
        if old_xdg:
            os.environ["XDG_CONFIG_HOME"] = old_xdg


# ---------------------------------------------------------------------------
# CLI startup stderr malformed-config warning
# ---------------------------------------------------------------------------


def test_cli_startup_stderr_warning(tmp_path: Path, monkeypatch):
    """CLI emits load errors to stderr, not stdout."""
    import subprocess
    import sys

    env = os.environ.copy()
    agent_dir = str(tmp_path / ".one" / "agent")
    env["ONE_CODING_AGENT_DIR"] = agent_dir
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    # Create malformed global settings.
    sf = Path(agent_dir) / "settings.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text("<<<not json>>>", encoding="utf-8")

    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "--list-models"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    # Error should appear on stderr.
    assert "[warn]" in res.stderr
    assert "global" in res.stderr
    # Should NOT appear on stdout (RPC JSON uses stdout).
    assert "[warn]" not in res.stdout
    # Startup must still succeed with defaults.
    assert res.returncode == 0
    assert "llama.cpp" in res.stdout


# ---------------------------------------------------------------------------
# ModelRegistry: shape errors preserve builtins
# ---------------------------------------------------------------------------


def test_model_registry_bad_providers_type_preserves_builtins(tmp_path: Path):
    """When providers is not a dict, builtins are preserved."""
    models_path = tmp_path / "models.json"
    models_path.write_text('{"providers": "not-a-dict"}', encoding="utf-8")
    auth = AuthStorage.in_memory()
    reg = ModelRegistry.create(auth, str(models_path))
    errors = reg.drain_errors()
    assert any("providers is not a dict" in str(e.get("error", "")) for e in errors)
    # Builtins must still be present.
    assert any(m.provider == "openai" and m.id == "gpt-4.1" for m in reg.all())


def test_model_registry_bad_models_list_preserves_builtins(tmp_path: Path):
    """When a provider's models is not a list, error recorded, builtins preserved."""
    models_path = tmp_path / "models.json"
    models_path.write_text(
        '{"providers": {"myprovider": "not-a-list"}}',
        encoding="utf-8",
    )
    auth = AuthStorage.in_memory()
    reg = ModelRegistry.create(auth, str(models_path))
    errors = reg.drain_errors()
    assert any("myprovider" in str(e.get("error", "")) for e in errors)
    assert any(m.provider == "openai" and m.id == "gpt-4.1" for m in reg.all())


# ---------------------------------------------------------------------------
# append_private_text: existing broad file mode tightening
# ---------------------------------------------------------------------------


def test_append_private_text_tightens_existing_file(tmp_path: Path):
    """Existing 0644 file is tightened to 0600 by append_private_text."""
    p = tmp_path / "existing.log"
    p.write_text("old\n", encoding="utf-8")
    if os.name == "posix":
        p.chmod(0o644)
    append_private_text(p, "new\n")
    if os.name == "posix":
        assert p.stat().st_mode & 0o777 == 0o600
    assert p.read_text(encoding="utf-8") == "old\nnew\n"
