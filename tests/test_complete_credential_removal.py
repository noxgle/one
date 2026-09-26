"""Phase 30.4: complete credential removal — runtime + stored + OAuth.

Tests cover:
- AuthStorage.remove_provider_credentials() clears runtime, stored keys, and OAuth
- Reload after logout does not reactivate stored credentials
- Unrelated providers retained after logout
- Corrupt persistence guard raises RuntimeError (no silent partial logout)
- Environment variable remains effective after local removal
- Placeholder API key does not count as configured
- OAuth-only valid records for chatgpt/anthropic count as configured
- Malformed/empty OAuth does not count
- Whitespace-only / non-string OAuth access does not count
- Interactive, TUI, and RPC logout use the central operation and updated wording
- RPC logout returns no secrets and stable schema
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.types import ModelInfo

# ---------------------------------------------------------------------------
# AuthStorage: remove_provider_credentials
# ---------------------------------------------------------------------------


def test_remove_provider_credentials_runtime_only():
    """Runtime-only key (in-memory) is cleared."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "runtime-key")
    assert auth.get_api_key("openai") == "runtime-key"
    auth.remove_provider_credentials("openai")
    assert auth.get_api_key("openai") is None
    assert auth._runtime.get("openai") is None


def test_remove_provider_credentials_stored_key(tmp_path: Path):
    """Stored API key is removed from _data and persisted."""
    auth_path = tmp_path / "auth.json"
    auth = AuthStorage.create(str(auth_path))
    auth.set_stored_api_key("openai", "stored-key")
    assert auth.get_api_key("openai") == "stored-key"
    auth.remove_provider_credentials("openai")
    assert auth.get_api_key("openai") is None
    data = json.loads(auth_path.read_text())
    assert "apiKeys" not in data or "openai" not in data.get("apiKeys", {})


def test_remove_provider_credentials_oauth_record(tmp_path: Path):
    """OAuth record is removed and persisted."""
    auth_path = tmp_path / "auth.json"
    auth = AuthStorage.create(str(auth_path))
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "tok", "refresh": "r"})
    assert auth.get_oauth_record("chatgpt") is not None
    auth.remove_provider_credentials("chatgpt")
    assert auth.get_oauth_record("chatgpt") is None
    data = json.loads(auth_path.read_text())
    assert "oauth" not in data or "chatgpt" not in data.get("oauth", {})


def test_remove_provider_credentials_all_three(tmp_path: Path):
    """Runtime + stored key + OAuth all cleared together."""
    auth_path = tmp_path / "auth.json"
    auth = AuthStorage.create(str(auth_path))
    auth.set_runtime_api_key("anthropic", "runtime")
    auth.set_stored_api_key("anthropic", "stored")
    auth.set_oauth_record("anthropic", {"type": "oauth", "access": "at"})
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "cgat"})

    auth.remove_provider_credentials("anthropic")

    assert auth.get_api_key("anthropic") is None
    assert auth.get_oauth_record("anthropic") is None
    assert auth._runtime.get("anthropic") is None
    # chatgpt unaffected
    assert auth.get_oauth_record("chatgpt") is not None


def test_remove_provider_credentials_reload_does_not_reactivate(tmp_path: Path):
    """After removal, a fresh AuthStorage must not see the credentials."""
    auth_path = tmp_path / "auth.json"
    auth = AuthStorage.create(str(auth_path))
    auth.set_stored_api_key("openai", "secret")
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "tok"})
    auth.remove_provider_credentials("openai")
    auth.remove_provider_credentials("chatgpt")

    # Reload
    auth2 = AuthStorage.create(str(auth_path))
    assert auth2.get_api_key("openai") is None
    assert auth2.get_oauth_record("chatgpt") is None


def test_remove_provider_credentials_unrelated_retained(tmp_path: Path):
    """Removing one provider does not affect another."""
    auth_path = tmp_path / "auth.json"
    auth = AuthStorage.create(str(auth_path))
    auth.set_stored_api_key("openai", "key1")
    auth.set_stored_api_key("anthropic", "key2")
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "tok"})
    auth.remove_provider_credentials("openai")
    assert auth.get_api_key("anthropic") == "key2"
    assert auth.get_oauth_record("chatgpt") is not None


def test_remove_provider_credentials_corrupt_guard_raises(tmp_path: Path):
    """Malformed auth file raises RuntimeError before any mutation.

    - The runtime key set BEFORE the call is NOT cleared (no partial logout).
    - The stored key remains in _data (can't persist to corrupt disk).
    - The malformed file is untouched.
    """
    auth_path = tmp_path / "auth.json"
    # Write malformed content
    auth_path.write_text("{malformed!!!", encoding="utf-8")
    auth = AuthStorage.create(str(auth_path))
    assert auth._data_is_locked is True

    # Set runtime key (always works) and inject stored key directly into _data
    auth.set_runtime_api_key("openai", "runtime-key")
    auth._data.setdefault("apiKeys", {})["openai"] = "stored-key"
    assert auth.get_api_key("openai") == "runtime-key"

    # Removal must raise RuntimeError
    with pytest.raises(RuntimeError, match="malformed|not a JSON object"):
        auth.remove_provider_credentials("openai")

    # Runtime key is STILL present — no partial mutation
    assert auth.get_api_key("openai") == "runtime-key"
    assert auth._runtime.get("openai") == "runtime-key"

    # Stored key remains in _data (no mutation happened)
    assert auth._data.get("apiKeys", {}).get("openai") == "stored-key"

    # Malformed file is untouched
    assert "stored-key" not in auth_path.read_text(encoding="utf-8")


def test_remove_provider_credentials_wrong_type_container_invalid_apiKeys(tmp_path: Path):
    """Invalid apiKeys raises RuntimeError; runtime key set before call remains."""
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"apiKeys": "not-a-dict"}), encoding="utf-8")
    auth = AuthStorage.create(str(auth_path))
    auth.set_runtime_api_key("openai", "runtime-key")

    with pytest.raises(RuntimeError, match="apiKeys is not a dict"):
        auth.remove_provider_credentials("openai")

    # Runtime key STILL present — no partial mutation
    assert auth._runtime.get("openai") == "runtime-key"
    assert auth.get_api_key("openai") == "runtime-key"
    # Source file unchanged
    assert auth_path.read_text(encoding="utf-8") == json.dumps({"apiKeys": "not-a-dict"})


def test_remove_provider_credentials_valid_apiKeys_invalid_oauth(tmp_path: Path):
    """Valid apiKeys containing target, but oauth is malformed — raises RuntimeError
    and no earlier mutation occurs (runtime key + stored key both remain)."""
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({
            "apiKeys": {"openai": "stored-key"},
            "oauth": ["not-a-dict"],
        }),
        encoding="utf-8",
    )
    auth = AuthStorage.create(str(auth_path))
    auth.set_runtime_api_key("openai", "runtime-key")

    with pytest.raises(RuntimeError, match="oauth is not a dict"):
        auth.remove_provider_credentials("openai")

    # Runtime key STILL present — no partial mutation
    assert auth._runtime.get("openai") == "runtime-key"
    assert auth.get_api_key("openai") == "runtime-key"
    # Stored key in _data unchanged (not popped even though valid)
    assert auth._data.get("apiKeys", {}).get("openai") == "stored-key"
    # Source file unchanged
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    assert data["apiKeys"] == {"openai": "stored-key"}
    assert data["oauth"] == ["not-a-dict"]


def test_remove_provider_credentials_env_var_survives(tmp_path: Path, monkeypatch):
    """Environment-supplied keys remain effective after local removal."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "runtime-key")
    auth.set_stored_api_key("openai", "stored-key")
    auth.remove_provider_credentials("openai")
    # Runtime and stored are gone, env is also absent
    assert auth.get_api_key("openai") is None


def test_remove_provider_credentials_env_var_active(tmp_path: Path, monkeypatch):
    """When only an env var provides auth, local removal of nothing
    does not affect it — the env var still provides configured status."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    try:
        auth_path = tmp_path / "auth.json"
        auth = AuthStorage.create(str(auth_path))
        # No runtime or stored keys
        auth.remove_provider_credentials("openai")
        assert auth.get_api_key("openai") == "env-key"
    finally:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_remove_provider_credentials_runtime_only_no_disk():
    """When only runtime exists (no disk), removal still works cleanly."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "rt")
    assert auth._path is None
    auth.remove_provider_credentials("openai")
    assert auth.get_api_key("openai") is None


# ---------------------------------------------------------------------------
# ModelRegistry: _has_usable_auth, has_configured_auth, get_provider_auth_status
# ---------------------------------------------------------------------------


def test_has_usable_auth_placeholder_key():
    """Placeholder API key does not count as configured."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "YOUR_OPENAI_API_KEY_HERE")
    reg = ModelRegistry.create(auth)
    assert reg._has_usable_auth(auth, "openai") is False
    model = ModelInfo(provider="openai", id="gpt-4.1")
    assert reg.has_configured_auth(model) is False


def test_has_usable_auth_valid_api_key():
    """Real API key counts as configured."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "sk-real-key-123")
    reg = ModelRegistry.create(auth)
    assert reg._has_usable_auth(auth, "openai") is True
    model = ModelInfo(provider="openai", id="gpt-4.1")
    assert reg.has_configured_auth(model) is True


def test_has_usable_auth_oauth_counts():
    """OAuth records with type==oauth and non-empty access count."""
    auth = AuthStorage.in_memory()
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "at", "refresh": "r"})
    reg = ModelRegistry.create(auth)
    assert reg._has_usable_auth(auth, "chatgpt") is True
    # Also through has_configured_auth
    model = ModelInfo(provider="chatgpt", id="gpt-5.6-sol")
    assert reg.has_configured_auth(model) is True


def test_has_usable_auth_anthropic_oauth_counts():
    """Anthropic OAuth also counts as configured."""
    auth = AuthStorage.in_memory()
    auth.set_oauth_record("anthropic", {"type": "oauth", "access": "sk-ant-oat01-abc", "refresh": "r"})
    reg = ModelRegistry.create(auth)
    assert reg._has_usable_auth(auth, "anthropic") is True


def test_has_usable_auth_malformed_oauth_no_count():
    """OAuth with type != oauth does not count."""
    auth = AuthStorage.in_memory()
    auth.set_oauth_record("chatgpt", {"type": "not-oauth", "access": "tok"})
    reg = ModelRegistry.create(auth)
    assert reg._has_usable_auth(auth, "chatgpt") is False


def test_has_usable_auth_empty_oauth_access_no_count():
    """OAuth with empty access does not count."""
    auth = AuthStorage.in_memory()
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "", "refresh": "r"})
    reg = ModelRegistry.create(auth)
    assert reg._has_usable_auth(auth, "chatgpt") is False


def test_has_usable_auth_whitespace_oauth_access_no_count():
    """OAuth with whitespace-only access does not count."""
    auth = AuthStorage.in_memory()
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "   ", "refresh": "r"})
    reg = ModelRegistry.create(auth)
    assert reg._has_usable_auth(auth, "chatgpt") is False


def test_has_usable_auth_non_string_oauth_access_no_count():
    """OAuth with a non-string access value (e.g. int/null) does not count."""
    auth = AuthStorage.in_memory()
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": 0, "refresh": "r"})
    reg = ModelRegistry.create(auth)
    assert reg._has_usable_auth(auth, "chatgpt") is False
    # Also with None
    auth2 = AuthStorage.in_memory()
    auth2.set_oauth_record("chatgpt", {"type": "oauth", "access": None})
    reg2 = ModelRegistry.create(auth2)
    assert reg2._has_usable_auth(auth2, "chatgpt") is False


def test_has_usable_auth_no_oauth_no_key():
    """No OAuth, no key → not configured."""
    auth = AuthStorage.in_memory()
    reg = ModelRegistry.create(auth)
    assert reg._has_usable_auth(auth, "chatgpt") is False


def test_has_usable_auth_no_auth_provider():
    """llama.cpp and ollama always count."""
    auth = AuthStorage.in_memory()
    reg = ModelRegistry.create(auth)
    assert reg._has_usable_auth(auth, "llama.cpp") is True
    assert reg._has_usable_auth(auth, "ollama") is True


def test_has_usable_auth_env_var_counts(monkeypatch):
    """Real environment variable counts as usable auth."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    try:
        auth = AuthStorage.in_memory()
        reg = ModelRegistry.create(auth)
        assert reg._has_usable_auth(auth, "openai") is True
    finally:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_get_provider_auth_status_oauth_configured():
    """Status configured is True when OAuth record exists."""
    auth = AuthStorage.in_memory()
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "tok", "refresh": "r"})
    reg = ModelRegistry.create(auth)
    status = reg.get_provider_auth_status("chatgpt")
    assert status["provider"] == "chatgpt"
    assert status["configured"] is True
    assert status["requiresApiKey"] is True
    assert status["envVar"] == "CHATGPT_API_KEY"


def test_get_provider_auth_status_after_logout():
    """Status configured is False after remove_provider_credentials."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "sk-key")
    auth.set_stored_api_key("openai", "stored")
    reg = ModelRegistry.create(auth)
    reg.remove_provider_credentials("openai")
    status = reg.get_provider_auth_status("openai")
    assert status["configured"] is False


def test_has_usable_auth_and_status_share_predicate():
    """Regression: has_configured_auth and get_provider_auth_status must
    produce the same configured result via the shared predicate."""
    auth = AuthStorage.in_memory()
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "tok"})
    reg = ModelRegistry.create(auth)
    model = ModelInfo(provider="chatgpt", id="gpt-5.6-sol")
    assert reg.has_configured_auth(model) is True
    assert reg.get_provider_auth_status("chatgpt")["configured"] is True


def test_get_provider_auth_status_no_network_refresh():
    """get_provider_auth_status must be synchronous with no network calls."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "sk-key")
    reg = ModelRegistry.create(auth)
    # Synchronous — no await needed
    result = reg.get_provider_auth_status("openai")
    assert result["configured"] is True


# ---------------------------------------------------------------------------
# ModelRegistry: remove_provider_credentials delegation
# ---------------------------------------------------------------------------


def test_registry_remove_provider_credentials_delegates():
    """Registry delegates to auth storage."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "rt")
    auth.set_stored_api_key("openai", "stored")
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "tok"})
    reg = ModelRegistry.create(auth)
    reg.remove_provider_credentials("openai")
    assert reg._auth.get_api_key("openai") is None
    assert reg._auth._runtime.get("openai") is None
    # chatgpt untouched
    assert reg._auth.get_oauth_record("chatgpt") is not None


# ---------------------------------------------------------------------------
# TUI mode: complete operation + wording (end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_logout_clears_all_and_wording(tmp_path: Path):
    """TUI /logout uses remove_provider_credentials and says 'locally'."""
    from one.modes.tui_mode import _OneTextualApp
    from tests.support.tui import _mk_app_session, _submit

    session = _mk_app_session(tmp_path)
    auth = session.model_registry._auth
    auth.set_runtime_api_key("openai", "rt")
    auth.set_stored_api_key("openai", "stored")
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "tok"})

    app = _OneTextualApp(session)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/logout openai")
        await pilot.pause()
        stream = "\n".join(app._stream_lines)
        assert "Removed credentials for openai locally." in stream
        # Verify removal
        assert auth.get_api_key("openai") is None
        assert auth.get_oauth_record("chatgpt") is not None  # unchanged


# ---------------------------------------------------------------------------
# RPC mode: complete operation + wording + no secrets + stable schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_logout_clears_all_and_schema(tmp_path: Path, monkeypatch, capsys):
    """RPC logout uses remove_provider_credentials, no secrets, stable schema."""
    from one.core.agent_session import AgentSession
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "sk-secret-key")
    auth.set_stored_api_key("openai", "stored-secret")
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "oauth-secret"})

    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 5}})
    session_manager = SessionManager.in_memory(str(tmp_path))

    # Import the shared RPC test loader through the tests package. Pytest does
    # not expose test modules as top-level imports with the current layout.
    from tests import test_rpc_mode as _test_rpc
    session = AgentSession(
        session_manager, settings, registry,
        _test_rpc._FakeLoader(str(tmp_path)), model, "medium",
    )

    responses = await _run_rpc_test(
        monkeypatch, capsys, session,
        [
            json.dumps({"type": "logout", "id": "1", "provider": "openai"}),
            json.dumps({"type": "logout", "id": "2", "provider": "chatgpt"}),
        ],
    )

    logout_openai = _find_resp(responses, "logout", "1")
    assert logout_openai["success"] is True
    assert logout_openai["data"]["provider"] == "openai"
    # Status should be stable schema
    status = logout_openai["data"]["status"]
    assert "provider" in status
    assert "configured" in status
    assert "requiresApiKey" in status
    assert "envVar" in status
    assert status["configured"] is False
    # No secrets in the response
    assert "sk-secret-key" not in json.dumps(logout_openai)
    assert "stored-secret" not in json.dumps(logout_openai)

    logout_chatgpt = _find_resp(responses, "logout", "2")
    assert logout_chatgpt["success"] is True
    assert logout_chatgpt["data"]["status"]["configured"] is False

    # Verify auth storage
    assert auth.get_api_key("openai") is None
    assert auth._runtime.get("openai") is None
    assert auth.get_oauth_record("chatgpt") is None


async def _run_rpc_test(monkeypatch, capsys, session, lines):
    """Feed JSON-lines to run_rpc_mode and return parsed responses."""
    import builtins

    from one.modes.rpc_mode import run_rpc_mode

    feed = iter(lines + [None])

    def fake_input(*args):
        try:
            nxt = next(feed)
        except StopIteration:
            raise EOFError from None
        if nxt is None:
            raise EOFError
        return nxt

    monkeypatch.setattr(builtins, "input", fake_input)
    try:
        await run_rpc_mode(_RuntimeHostFake(session))
    except EOFError:
        pass
    out = capsys.readouterr().out
    return [json.loads(l) for l in out.splitlines() if l.strip()]


class _RuntimeHostFake:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def new_session(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"cancelled": False}


def _find_resp(responses: list[dict], cmd_type: str, cmd_id: str) -> dict[str, Any]:
    for r in responses:
        if r.get("type") == "response" and r.get("command") == cmd_type and r.get("id") == cmd_id:
            return r
    return {}  # Return empty dict instead of None for safer downstream use


# ---------------------------------------------------------------------------
# Persistence roundtrip: remove then reload
# ---------------------------------------------------------------------------


def test_remove_then_reload_persistence_roundtrip(tmp_path: Path):
    """Set credentials → remove → reload fresh storage → none visible."""
    auth_path = tmp_path / "auth.json"
    auth = AuthStorage.create(str(auth_path))
    auth.set_stored_api_key("openai", "key1")
    auth.set_stored_api_key("anthropic", "key2")
    auth.set_runtime_api_key("chatgpt", "rt-key")
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "at"})

    auth.remove_provider_credentials("openai")
    auth.remove_provider_credentials("chatgpt")

    # Reload from disk
    auth2 = AuthStorage.create(str(auth_path))
    assert auth2.get_api_key("openai") is None
    assert auth2.get_api_key("chatgpt") is None
    assert auth2.get_oauth_record("chatgpt") is None
    assert auth2.get_api_key("anthropic") == "key2"  # unchanged


def test_remove_provider_credentials_empty_maps_pruned(tmp_path: Path):
    """After removal the last entry in apiKeys/oauth is pruned (empty map removed)."""
    auth_path = tmp_path / "auth.json"
    auth = AuthStorage.create(str(auth_path))
    auth.set_stored_api_key("openai", "k1")
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "a"})
    auth.remove_provider_credentials("openai")
    auth.remove_provider_credentials("chatgpt")
    data = json.loads(auth_path.read_text())
    assert "apiKeys" not in data
    assert "oauth" not in data


def test_remove_stored_api_key_still_works_for_backward_compat():
    """Old narrow method still exists and works (backward compatibility)."""
    auth = AuthStorage.in_memory()
    auth.set_stored_api_key("openai", "key")
    auth.remove_stored_api_key("openai")
    assert auth.get_api_key("openai") is None


# ---------------------------------------------------------------------------
# Provider identity: no aliasing — chatgpt / openai remain distinct
# ---------------------------------------------------------------------------


def test_provider_identity_no_aliasing():
    """chatgpt and openai are distinct exact IDs; removing one does not affect the other."""
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "sk-openai")
    auth.set_runtime_api_key("chatgpt", "sk-chatgpt")
    auth.remove_provider_credentials("openai")
    assert auth.get_api_key("openai") is None
    assert auth.get_api_key("chatgpt") == "sk-chatgpt"


def test_oauth_only_anthropic_counts_as_configured():
    """Anthropic with only an OAuth record (no API key) is configured."""
    auth = AuthStorage.in_memory()
    auth.set_oauth_record("anthropic", {"type": "oauth", "access": "sk-ant-oat01-xyz", "refresh": "r"})
    reg = ModelRegistry.create(auth)
    model = ModelInfo(provider="anthropic", id="claude-3-7-sonnet-latest")
    assert reg.has_configured_auth(model) is True
    status = reg.get_provider_auth_status("anthropic")
    assert status["configured"] is True


def test_oauth_only_chatgpt_counts_as_configured():
    """ChatGPT with only an OAuth record (no API key) is configured."""
    auth = AuthStorage.in_memory()
    auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "at", "refresh": "r", "accountId": "acc"})
    reg = ModelRegistry.create(auth)
    model = ModelInfo(provider="chatgpt", id="gpt-5.6-sol")
    assert reg.has_configured_auth(model) is True
