from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from one import config
from one.core.agent_session_runtime import create_agent_session_runtime
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.sdk import create_agent_session
from one.core.session_manager import SessionManager, get_default_session_dir
from one.core.settings_manager import SettingsManager
from one.modes.run_mode import run_run_mode

# ======================================================================
# Helpers
# ======================================================================

class _Provider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


class _MockHost:
    def __init__(self, session: Any) -> None:
        self.session = session


def _write_sentinel(dirpath: Path, name: str, content: str) -> None:
    (dirpath / name).write_text(content, encoding="utf-8")


# ======================================================================
# A. Full custom-dir isolation
# ======================================================================

@pytest.mark.asyncio
async def test_full_custom_dir_isolation(monkeypatch, tmp_path: Path) -> None:
    """Custom SDK agentDir contains all state; global sentinel tree untouched."""
    custom = tmp_path / "custom"
    custom.mkdir()

    # Separate "global" sentinel directory to prove nothing is written there.
    global_dir = tmp_path / "global_sentinel"
    global_dir.mkdir()
    monkeypatch.setenv(config.ENV_AGENT_DIR, str(global_dir))

    # Seed global sentinels BEFORE SDK call — valid JSON so parse errors don't mask regression.
    _write_sentinel(global_dir, "auth.json", json.dumps({"state": "global-auth"}))
    _write_sentinel(global_dir, "models.json", json.dumps({"state": "global-models"}))
    _write_sentinel(global_dir, "settings.json", json.dumps({"state": "global-settings"}))
    g_sessions = global_dir / "sessions"
    g_sessions.mkdir()
    _write_sentinel(g_sessions, "old.jsonl", json.dumps({"state": "old-session"}))

    # Snapshot recursive file tree + POSIX modes BEFORE SDK call.
    def _snapshot_dir(dirpath: Path) -> dict[str, Any]:
        info: dict[str, Any] = {}
        for p in sorted(dirpath.rglob("*")):
            rel = str(p.relative_to(dirpath))
            st = p.stat()
            entry_type = "dir" if p.is_dir() else "file"
            if entry_type == "file":
                info[rel] = {
                    "type": entry_type,
                    "mode": oct(stat.S_IMODE(st.st_mode)),
                    "content": p.read_bytes(),
                }
            else:
                info[rel] = {
                    "type": entry_type,
                    "mode": oct(stat.S_IMODE(st.st_mode)),
                }
        return info

    before = _snapshot_dir(global_dir)

    # --- Create session via SDK with explicit agentDir ---
    result = await create_agent_session({"cwd": str(custom), "agentDir": str(custom)})
    session = result["session"]
    assert session.session_id

    # --- Mutate through public dependencies (these trigger file creation) ---
    session.model_registry.set_stored_api_key("openai", "test-key-for-persist")
    session.settings_manager.set_default_provider("openai")
    session.settings_manager.set_default_model("gpt-4.1")
    session.settings_manager.set_default_thinking_level("high")
    session.model_registry.register_models("test-provider", ["test-model-1"])
    session.model_registry.persist_models("test-provider", ["test-model-1"])
    session.session_manager.append_message({"role": "user", "content": "test message"})
    session.session_manager.append_message({"role": "assistant", "content": "test reply"})

    # Custom state files exist under custom agentDir (after writes).
    assert (custom / "auth.json").exists()
    assert (custom / "models.json").exists()
    assert (custom / "settings.json").exists()
    assert (custom / "sessions").exists()

    custom_models_content = json.loads((custom / "models.json").read_text(encoding="utf-8"))
    assert "test-provider" in custom_models_content.get("providers", {})

    custom_settings_content = json.loads((custom / "settings.json").read_text(encoding="utf-8"))
    assert custom_settings_content.get("defaultProvider") == "openai"

    # --- Run runtime host with fake provider / finish flow ---
    selected_model_provider = session.model.provider if session.model else "openai"
    agent = session
    agent.providers = {selected_model_provider: _Provider([
        '{"tool":"finish","args":{"summary":"done","goal_success":true}}',
        "DONE",
    ])}
    mock_host = _MockHost(agent)

    code = await run_run_mode(mock_host, {"task": "go", "agentDir": str(custom)})
    assert code == 0

    # Assert custom reports.jsonl was written.
    reports = custom / "reports.jsonl"
    assert reports.exists()
    report_lines = reports.read_text(encoding="utf-8").strip().splitlines()
    assert len(report_lines) >= 1
    report_entry = json.loads(report_lines[0])
    assert report_entry["goalSuccess"] is True
    assert report_entry["summary"] == "done"

    # --- Global sentinel tree MUST be unchanged (recursive bytes + modes) ---
    after = _snapshot_dir(global_dir)
    assert after == before, "global sentinel tree was modified"

    # Also assert sentinel file contents directly.
    assert json.loads((global_dir / "auth.json").read_text(encoding="utf-8")) == {"state": "global-auth"}
    assert json.loads((global_dir / "models.json").read_text(encoding="utf-8")) == {"state": "global-models"}
    assert json.loads((global_dir / "settings.json").read_text(encoding="utf-8")) == {"state": "global-settings"}
    assert json.loads((g_sessions / "old.jsonl").read_text(encoding="utf-8")) == {"state": "old-session"}
    # No extra files were added in global sessions.
    g_session_files = list(g_sessions.glob("*.jsonl"))
    assert len(g_session_files) == 1
    assert g_session_files[0].name == "old.jsonl"


# ======================================================================
# B. Injected manager precedence
# ======================================================================

@pytest.mark.asyncio
async def test_injected_manager_precedence(monkeypatch, tmp_path: Path) -> None:
    """Explicitly injected managers win over defaults; no state files created."""
    # Isolate ONE_CODING_AGENT_DIR so SettingsManager.in_memory doesn't resolve real config.
    monkeypatch.setenv(config.ENV_AGENT_DIR, str(tmp_path / "isolated"))

    custom = tmp_path / "custom"
    custom.mkdir()

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")

    explicit_models_path = str(custom / "explicit_models.json")
    registry = ModelRegistry.create(auth, explicit_models_path)

    settings = SettingsManager.in_memory({"defaultProvider": "openai", "defaultModel": "gpt-4.1"})
    session_manager = SessionManager.in_memory(str(tmp_path))

    result = await create_agent_session({
        "cwd": str(custom),
        "agentDir": str(custom),
        "authStorage": auth,
        "modelRegistry": registry,
        "settingsManager": settings,
        "sessionManager": session_manager,
    })
    session = result["session"]

    # Object identities must match injected ones.
    assert session.session_manager is session_manager
    assert session.settings_manager is settings
    assert session.model_registry is registry

    # Auth is in-memory → no auth.json under custom.
    assert not (custom / "auth.json").exists()
    # Registry models_path is explicit, but no models persisted → no file.
    assert not Path(explicit_models_path).exists()
    # Settings is in-memory → no settings.json under custom.
    assert not (custom / "settings.json").exists()


# ======================================================================
# C. Mixed injected auth only — default registry uses injected auth
# ======================================================================

@pytest.mark.asyncio
async def test_mixed_injected_auth_default_registry(monkeypatch, tmp_path: Path) -> None:
    """Inject auth only; default registry shares auth object and persists models under custom."""
    monkeypatch.setenv(config.ENV_AGENT_DIR, str(tmp_path / "isolated"))

    custom = tmp_path / "custom"
    custom.mkdir()

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")

    result = await create_agent_session({
        "cwd": str(custom),
        "agentDir": str(custom),
        "authStorage": auth,
    })
    session = result["session"]

    custom_models = custom / "models.json"
    custom_settings = custom / "settings.json"
    assert not custom_models.exists()  # not yet persisted

    session.model_registry.register_models("test-auth-only", ["model-a"])
    session.model_registry.persist_models("test-auth-only", ["model-a"])
    assert custom_models.exists()
    models_data = json.loads(custom_models.read_text(encoding="utf-8"))
    assert "test-auth-only" in models_data.get("providers", {})

    assert not (custom / "auth.json").exists()
    session.settings_manager.set_default_provider("openai")
    assert custom_settings.exists()


# ======================================================================
# D. Configured sessionDir in custom settings overrides default
# ======================================================================

@pytest.mark.asyncio
async def test_configured_session_dir_overrides_default(monkeypatch, tmp_path: Path) -> None:
    """When settings specifies sessionDir, that path is used instead of <agentDir>/sessions."""
    monkeypatch.setenv(config.ENV_AGENT_DIR, str(tmp_path / "isolated"))

    custom = tmp_path / "custom"
    custom.mkdir()

    custom_session_dir = tmp_path / "custom_session_dir"
    custom_session_dir.mkdir()

    settings = SettingsManager.in_memory(
        {"defaultProvider": "openai", "defaultModel": "gpt-4.1", "sessionDir": str(custom_session_dir)}
    )

    result = await create_agent_session({
        "cwd": str(custom),
        "agentDir": str(custom),
        "settingsManager": settings,
    })
    session = result["session"]

    assert session.session_manager.session_dir == str(custom_session_dir)

    session.session_manager.append_message({"role": "user", "content": "hello"})
    sm_file = session.session_manager.session_file
    assert sm_file is not None
    assert str(custom_session_dir) in sm_file
    assert not (custom / "sessions").exists()


@pytest.mark.asyncio
async def test_absent_config_uses_agent_dir_sessions(monkeypatch, tmp_path: Path) -> None:
    """When settings has no sessionDir, default is <agentDir>/sessions/<cwd-hash>/."""
    monkeypatch.setenv(config.ENV_AGENT_DIR, str(tmp_path / "isolated"))

    custom = tmp_path / "custom"
    custom.mkdir()

    result = await create_agent_session({
        "cwd": str(custom),
        "agentDir": str(custom),
    })
    session = result["session"]

    assert "sessions" in session.session_manager.session_dir
    assert str(custom) in session.session_manager.session_dir

    session.session_manager.append_message({"role": "user", "content": "hi"})
    sm_file = session.session_manager.session_file
    assert sm_file is not None
    assert str(custom) in sm_file


# ======================================================================
# E. No-global-helper / migration path with explicit custom agentDir
# ======================================================================

@pytest.mark.asyncio
async def test_explicit_agentDir_ignores_global_helpers(monkeypatch, tmp_path: Path) -> None:
    """With explicit custom agentDir, SDK succeeds and global helpers are NOT called."""
    custom = tmp_path / "custom"
    custom.mkdir()

    # Patch the imported aliases actually used in sdk.py and agent_session_runtime.py
    def _raising_get_agent_dir() -> str:
        raise RuntimeError("get_agent_dir should NOT be called when agentDir is explicit")

    monkeypatch.setattr("one.core.sdk.get_agent_dir", _raising_get_agent_dir)
    monkeypatch.setattr("one.core.agent_session_runtime.get_agent_dir", _raising_get_agent_dir)

    # Patch config-path helpers to raise — proves explicit paths avoid global fallback.
    monkeypatch.setattr("one.core.auth_storage.get_auth_path", _raising_get_agent_dir)
    monkeypatch.setattr("one.core.model_registry.get_models_path", _raising_get_agent_dir)

    # Wrap get_default_session_dir to record calls; do NOT forbid it (custom-aware helper).
    gdsd_calls: list[tuple[str, str | None]] = []
    original_gdsd = get_default_session_dir

    def _wrapping_gdsd(cwd: str, agent_dir: str | None = None) -> str:
        gdsd_calls.append((cwd, agent_dir))
        return original_gdsd(cwd, agent_dir)

    import one.core.sdk as sdk_mod
    monkeypatch.setattr(sdk_mod, "get_default_session_dir", _wrapping_gdsd)

    # SDK creation with explicit agentDir must succeed.
    result = await create_agent_session({
        "cwd": str(custom),
        "agentDir": str(custom),
    })
    assert result["session"].session_id

    # Assertions: global helpers never called; gdsd called with explicit custom dir.
    assert gdsd_calls == [(str(custom), str(custom))], f"unexpected gdsd calls: {gdsd_calls}"


# ======================================================================
# F. No real API calls — SDK is fully offline with injected managers
# ======================================================================

@pytest.mark.asyncio
async def test_sdk_no_real_api_calls(monkeypatch, tmp_path: Path) -> None:
    """SDK creation with all-injected managers must work offline; registry points at temp."""
    # Isolate ONE_CODING_AGENT_DIR so no real global state can leak in.
    monkeypatch.setenv(config.ENV_AGENT_DIR, str(tmp_path / "isolated"))

    custom = tmp_path / "custom"
    custom.mkdir()

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")

    # Explicit tmp models path so registry cannot read real global state.
    explicit_models_path = str(custom / "test_models.json")
    registry = ModelRegistry.create(auth, explicit_models_path)

    settings = SettingsManager.in_memory({"defaultProvider": "openai", "defaultModel": "gpt-4.1"})
    session_manager = SessionManager.in_memory(str(custom))

    result = await create_agent_session({
        "cwd": str(custom),
        "agentDir": str(custom),
        "authStorage": auth,
        "modelRegistry": registry,
        "settingsManager": settings,
        "sessionManager": session_manager,
    })
    assert result["session"].session_id
    assert result["modelFallbackMessage"] is None  # auth configured for openai/gpt-4.1


# ======================================================================
# G. Direct runtime test — no injected defaults, files land under custom dir
# ======================================================================

@pytest.mark.asyncio
async def test_create_runtime_with_custom_agent_dir(monkeypatch, tmp_path: Path) -> None:
    """Runtime creates auth/models/settings/sessions under custom agentDir when no injected defaults."""
    custom = tmp_path / "custom"
    custom.mkdir()

    bootstrap = {"agentDir": str(custom)}

    def _raising_get_agent_dir() -> str:
        raise RuntimeError("get_agent_dir should NOT be called — bootstrap has explicit agentDir")

    monkeypatch.setattr("one.core.agent_session_runtime.get_agent_dir", _raising_get_agent_dir)
    monkeypatch.setattr("one.config.get_agent_dir", _raising_get_agent_dir)
    monkeypatch.setattr("one.core.auth_storage.get_auth_path", _raising_get_agent_dir)
    monkeypatch.setattr("one.core.model_registry.get_models_path", _raising_get_agent_dir)

    # No injected managers — direct runtime creates its own under custom dir.
    options = {
        "cwd": str(custom),
    }
    runtime = await create_agent_session_runtime(bootstrap, options)
    assert runtime.session.session_id

    # Trigger writes so files get created (constructors only read).
    runtime.session.model_registry.set_stored_api_key("openai", "test-key")
    runtime.session.settings_manager.set_default_provider("openai")
    runtime.session.model_registry.register_models("test", ["t1"])
    runtime.session.model_registry.persist_models("test", ["t1"])

    # Auth, models, settings files must land under custom.
    assert (custom / "auth.json").exists()
    assert (custom / "models.json").exists()
    assert (custom / "settings.json").exists()

    # Session file must be under custom/sessions/.
    runtime.session.session_manager.append_message({"role": "user", "content": "task message"})
    sm_file = runtime.session.session_manager.session_file
    assert sm_file is not None
    assert Path(sm_file).is_relative_to(custom / "sessions"), f"session file {sm_file} not under custom/sessions"


# ======================================================================
# H. Config path tests — existing tests preserved + new ones
# ======================================================================

def test_get_agent_dir_prefers_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(config.ENV_AGENT_DIR, str(tmp_path / "custom-agent-dir"))
    out = config.get_agent_dir()
    assert out == str(tmp_path / "custom-agent-dir")


def test_get_agent_dir_defaults_to_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(config.ENV_AGENT_DIR, raising=False)
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    out = config.get_agent_dir()
    assert out == str(tmp_path / "xdg" / "one")


def test_get_agent_dir_migrates_legacy_to_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(config.ENV_AGENT_DIR, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    legacy = tmp_path / ".one" / "agent"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "settings.json").write_text('{"a":1}', encoding="utf-8")
    (legacy / "models.json").write_text('{"providers":{}}', encoding="utf-8")
    out = config.get_agent_dir()
    xdg = tmp_path / "xdg" / "one"
    assert out == str(xdg)
    assert (xdg / "settings.json").exists()
    assert (xdg / "models.json").exists()
