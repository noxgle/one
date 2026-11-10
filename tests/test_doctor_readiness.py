# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
import socket

import pytest

from one.core.auth_storage import AuthStorage
from one.core.doctor.readiness import assess_provider_model
from one.core.model_registry import ModelRegistry
from one.core.settings_manager import SettingsManager


@pytest.fixture
def registry(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    return ModelRegistry(AuthStorage(in_memory=True), str(tmp_path / "models.json"), read_only=True)


def test_effective_settings_and_local_model(tmp_path, registry):
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    agent.mkdir()
    (project / ".one").mkdir(parents=True)
    (agent / "settings.json").write_text(json.dumps({"defaultProvider": "openai", "defaultModel": "gpt-4o"}))
    (project / ".one" / "settings.json").write_text(
        json.dumps({"defaultProvider": "ollama", "defaultModel": "local"})
    )
    settings = SettingsManager.read_only(str(project), str(agent))
    result = assess_provider_model(settings, registry)
    assert (result.provider, result.model, result.status) == ("ollama", "local", "configured")
    assert result.model_source == "registry" and result.default_selectable
    assert result.availability == "unverified" and result.auth == "not_required"
    assert (result.selected_provider, result.selected_model, result.selected_auth) == ("ollama", "local", "not_required")


@pytest.mark.parametrize(("provider", "model", "status"), [
    (None, None, "configured"), ("openai", None, "configured"),
    ("", "model", "invalid"), (42, "model", "invalid"),
    ("unknown", "model", "unsupported_provider"),
])
def test_missing_and_invalid(registry, provider, model, status):
    settings = SettingsManager("", "", in_memory=True, initial={"defaultProvider": provider, "defaultModel": model})
    assert assess_provider_model(settings, registry).status == status


def test_dynamic_and_catalog(registry):
    settings = SettingsManager("", "", in_memory=True, initial={"defaultProvider": "openai", "defaultModel": "new-id"})
    result = assess_provider_model(settings, registry)
    assert result.status == "missing" and result.model_source == "dynamic" and not result.default_selectable
    registry.catalog.facts[("openai", "new-id")] = {"contextWindow": 1234}
    result = assess_provider_model(settings, registry)
    assert result.model_source == "catalog" and not result.default_selectable


def test_conditional_provider(registry, monkeypatch):
    settings = SettingsManager("", "", in_memory=True, initial={"defaultProvider": "azure-openai", "defaultModel": "x"})
    monkeypatch.delenv("AZURE_OPENAI_BASE_URL", raising=False)
    assert assess_provider_model(settings, registry).status == "unsupported_provider"
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "http://localhost")
    assert assess_provider_model(settings, registry).status == "missing"


def test_persisted_model_read_only(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"providers": {"ollama": [{"id": "custom"}]}}))
    path.chmod(0o644)
    registry = ModelRegistry(AuthStorage(in_memory=True), str(path), read_only=True)
    settings = SettingsManager("", "", in_memory=True, initial={"defaultProvider": "ollama", "defaultModel": "custom"})
    assert assess_provider_model(settings, registry).default_selectable
    assert path.stat().st_mode & 0o777 == 0o644


def test_auth_precedence_and_runtime_fallback(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"apiKeys": {"openai": "placeholder"}}))
    path.chmod(0o644)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-env-secret")
    auth = AuthStorage(str(path), read_only=True)
    registry = ModelRegistry(auth, str(tmp_path / "models.json"), read_only=True)
    settings = SettingsManager("", "", in_memory=True, initial={"defaultProvider": "openai", "defaultModel": "gpt-4o"})
    result = assess_provider_model(settings, registry)
    assert result.auth == "missing"  # stored placeholder masks environment
    assert result.selected_provider in {"ollama", "llama.cpp"}
    assert result.selected_auth == "not_required"
    auth.set_runtime_api_key("openai", "synthetic-runtime-secret")
    result = assess_provider_model(settings, registry)
    assert result.auth == "configured" and result.selected_provider == "openai"
    assert "synthetic" not in repr(result)
    assert path.stat().st_mode & 0o777 == 0o644


def test_oauth_and_stale_default(registry):
    registry._auth.set_oauth_record("openai", {"type": "oauth", "access": "synthetic-token"})
    settings = SettingsManager("", "", in_memory=True, initial={"defaultProvider": "openai", "defaultModel": "stale"})
    result = assess_provider_model(settings, registry)
    assert result.auth == "configured"
    assert result.selected_provider == "openai" and result.selected_model != "stale"
    assert result.status == "missing"
    assert "synthetic-token" not in repr(result)


def test_no_available_auth_and_no_models(registry, monkeypatch):
    settings = SettingsManager("", "", in_memory=True, initial={})
    monkeypatch.setattr(registry, "select_default", lambda *_: None)
    result = assess_provider_model(settings, registry)
    assert result.status == "missing" and result.selection == "none"


def test_selected_without_auth_is_not_ready(registry, monkeypatch):
    settings = SettingsManager("", "", in_memory=True, initial={})
    model = registry.find("openai", "gpt-4o")
    monkeypatch.setattr(registry, "select_default", lambda *_: model)
    monkeypatch.setattr(registry, "get_provider_auth_status", lambda _: {"requiresApiKey": True, "configured": False})
    result = assess_provider_model(settings, registry)
    assert result.status == "missing" and result.selected_auth == "missing"


def test_doctor_readiness_outcomes_offline(tmp_path, monkeypatch):
    from one.core.doctor import DoctorEngine

    agent = tmp_path / "agent"
    agent.mkdir()
    monkeypatch.setenv("ONE_CODING_AGENT_DIR", str(agent))
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("network access"))
    settings = agent / "settings.json"

    def check():
        report = DoctorEngine().check(tmp_path)
        ids = {issue.id for issue in report.issues}
        return report, ids

    report, ids = check()
    assert report.result == "ok" and "readiness.unverified" in ids
    settings.write_text(json.dumps({"defaultProvider": "ollama", "defaultModel": "private-local"}))
    report, ids = check()
    assert report.result == "ok" and "readiness.unverified" in ids
    settings.write_text(json.dumps({"defaultProvider": "openai", "defaultModel": "gpt-4o"}))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    report, ids = check()
    assert report.result == "action_required" and "readiness.fallback" in ids
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-canary")
    report, ids = check()
    assert report.result == "ok" and "readiness.unverified" in ids
    assert "synthetic-secret-canary" not in report.to_json() + report.to_text()
    monkeypatch.setattr(ModelRegistry, "select_default", lambda *a: None)
    report, ids = check()
    assert report.result == "action_required" and "readiness.unavailable" in ids
    monkeypatch.setattr(ModelRegistry, "select_default", lambda *a: (_ for _ in ()).throw(ValueError("secret-canary")))
    report, ids = check()
    assert report.result == "action_required" and "readiness.unknown" in ids
    assert "secret-canary" not in report.to_json() + report.to_text()
