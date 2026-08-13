"""P2-3: Auth precedence matrix & provider fallback — missing cases.

Distinct from tests/test_auth_and_cli.py:
  1. test_stored_key_persists_across_instances          — NOT covered (no file round-trip)
  2. test_env_key_used_when_nothing_stored              — NOT covered (no "env-only" scenario)
  3. test_stored_key_beats_env                          — NOT covered (file-vs-env without runtime)
  4. test_runtime_key_not_persisted_to_file             — NOT covered (no cross-instance check)
  5. test_placeholder_keys_are_not_configured             — PARTIALLY covered (only "YOUR_OPENAI_API_KEY_HERE"
  6. test_select_default_exact_provider_missing_model_falls_back_within_provider
       → already covered by test_select_default_falls_back_to_default_provider;
         here we use an env key (not set_runtime_api_key) for a distinct angle.
  7. test_get_api_key_and_headers_ok_for_configured_provider  — NOT covered (only ok=False path tested)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry


# ---------------------------------------------------------------------------
# 1. Stored key persists across AuthStorage instances (file round-trip)
# ---------------------------------------------------------------------------

def test_stored_key_persists_across_instances(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"

    first = AuthStorage.create(str(auth_path))
    first.set_stored_api_key("openai", "persisted")
    assert first.get_api_key("openai") == "persisted"

    # Create a SECOND instance pointing at the same file.
    second = AuthStorage.create(str(auth_path))
    assert second.get_api_key("openai") == "persisted"


# ---------------------------------------------------------------------------
# 2. Env key used when nothing stored (no file, no runtime)
# ---------------------------------------------------------------------------

def test_env_key_used_when_nothing_stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-only")
    # File does NOT exist — AuthStorage.create loads from a non-existent path.
    auth = AuthStorage.create(str(tmp_path / "no-such-file" / "auth.json"))
    assert auth.get_api_key("openai") == "env-only"


# ---------------------------------------------------------------------------
# 3. Stored key beats env key; runtime beats both
# ---------------------------------------------------------------------------

def test_stored_key_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"apiKeys": {"openai": "file-key"}}), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")

    auth = AuthStorage.create(str(auth_path))
    assert auth.get_api_key("openai") == "file-key"

    # Runtime key takes precedence over stored (redundant matrix cell).
    auth.set_runtime_api_key("openai", "rt-key")
    assert auth.get_api_key("openai") == "rt-key"


# ---------------------------------------------------------------------------
# 4. Runtime key is NOT persisted to file
# ---------------------------------------------------------------------------

def test_runtime_key_not_persisted_to_file(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"

    first = AuthStorage.create(str(auth_path))
    first.set_runtime_api_key("openai", "rt-key")
    assert first.get_api_key("openai") == "rt-key"

    # New instance should NOT see the runtime key (runtime is process-local).
    second = AuthStorage.create(str(auth_path))
    assert second.get_api_key("openai") is None


# ---------------------------------------------------------------------------
# 5. All _is_placeholder_key values are NOT configured
# ---------------------------------------------------------------------------

def test_placeholder_keys_are_not_configured(tmp_path: Path) -> None:
    # Values that _is_placeholder_key treats as placeholders
    # (implementation: starts-with "your_", exact match of set,
    #  "your" AND "here", or wrapped in <>).
    placeholder_values = [
        "sk-xxx",
        "changeme",
        "none",
        "null",
        "placeholder",
        "enter",
        "insert",
        "xxx",
        "your_api_key_here",
        "YOUR_OPENAI_API_KEY_HERE",
        "<enter-key>",
        "<api-key>",
    ]

    for val in placeholder_values:
        auth = AuthStorage.in_memory()
        auth.set_runtime_api_key("openai", val)
        registry = ModelRegistry.create(auth, str(tmp_path / "no" / "models.json"))
        model = registry.find("openai", "gpt-4.1")
        assert model is not None
        assert registry.has_configured_auth(model) is False, (
            f"Placeholder {val!r} should not be configured auth"
        )
        assert registry.get_api_key_and_headers(model)["ok"] is False, (
            f"Placeholder {val!r} should return ok=False"
        )

    # A real key MUST be recognized as configured.
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "sk-real-key-123")
    registry = ModelRegistry.create(auth, str(tmp_path / "no" / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    assert registry.has_configured_auth(model) is True
    assert registry.get_api_key_and_headers(model)["ok"] is True


# ---------------------------------------------------------------------------
# 6. select_default with exact provider + missing model falls back within
#    provider.  (Distinct variant: uses env key instead of set_runtime_api_key.)
# ---------------------------------------------------------------------------

def test_select_default_exact_provider_missing_model_falls_back_within_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key-123")
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth, str(tmp_path / "no" / "models.json"))
    # Non-existent model -> falls back to first available model within openai.
    model = registry.select_default("openai", "no-such-model")
    assert model is not None
    assert model.provider == "openai"
    # Verify it returns the first openai model (gpt-4.1) not some other provider.
    assert model.id == "gpt-4.1"


# ---------------------------------------------------------------------------
# 7. get_api_key_and_headers returns ok=True for configured provider
# ---------------------------------------------------------------------------

def test_get_api_key_and_headers_ok_for_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-abc")
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    result = registry.get_api_key_and_headers(model)
    assert result["ok"] is True
    assert result["apiKey"] == "sk-abc"
    assert result["headers"] == {}
