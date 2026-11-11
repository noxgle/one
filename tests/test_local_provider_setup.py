# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from one.core.auth_storage import AuthStorage
from one.core.local_provider_setup import local_provider_operation
from one.core.model_registry import ModelRegistry
from one.core.settings_manager import SettingsManager
from one.providers.registry import build_provider_registry


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example.test:11434/v1")
    manager = SettingsManager(str(tmp_path), str(tmp_path / "state"))
    registry = ModelRegistry(AuthStorage.in_memory(), str(tmp_path / "state" / "models.json"))
    return SimpleNamespace(settings_manager=manager, model_registry=registry, providers=build_provider_registry(),
                           model=None, is_streaming=False, set_model=AsyncMock())


@pytest.mark.asyncio
async def test_config_environment_and_save_preserves_disk(session):
    config = await local_provider_operation(session, "local_provider_config", {"provider": "ollama"})
    assert config["url"] == "http://example.test:11434/v1"
    settings = session.settings_manager._global_path
    models = session.model_registry._models_path
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"custom": {"keep": True}}))
    models.write_text(json.dumps({"other": 1, "providers": {"ollama": [{"id": "m", "reasoning": False}]}}))
    result = await local_provider_operation(session, "local_provider_save",
                                            {"provider": "ollama", "url": "http://localhost:123/", "model": "m"})
    assert result["ok"]
    assert json.loads(settings.read_text())["custom"] == {"keep": True}
    assert json.loads(models.read_text())["providers"]["ollama"] == [
        {"id": "m", "reasoning": False, "url": "http://localhost:123/v1"}]
    assert session.model_registry.find("ollama", "m").base_url == "http://localhost:123/v1"
    assert session.set_model.await_args.args[0].base_url == "http://localhost:123/v1"
    assert session.settings_manager.get_default_model() == "m"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["http://user:secret@localhost", "http://localhost/?secret=x", "file:///tmp/x",
                                 "http://localhost/#x", "http://localhost:99999"])
async def test_reject_url(session, bad):
    result = await local_provider_operation(session, "local_provider_models", {"provider": "ollama", "url": bad})
    assert not result["ok"]
    assert "secret" not in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,url,path,response", [
    ("ollama", "http://host/v1/", "/api/tags", {"models": [{"name": "a"}]}),
    ("llama.cpp", "http://host/v1", "/v1/models", {"data": [{"id": "a"}]}),
])
async def test_discovery(session, monkeypatch, provider, url, path, response):
    original = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs == {"timeout": 5.0, "trust_env": False, "follow_redirects": False}
        def handler(request):
            assert request.url.path == path
            return httpx.Response(200, json=response)
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = await local_provider_operation(session, "local_provider_models", {"provider": provider, "url": url})
    assert result["models"] == ["a"]


@pytest.mark.asyncio
async def test_busy_malformed_and_project_conflict(session):
    payload = {"provider": "ollama", "url": "http://localhost", "model": "m"}
    session.is_streaming = True
    assert not (await local_provider_operation(session, "local_provider_save", payload))["ok"]
    session.is_streaming = False
    models = session.model_registry._models_path
    models.parent.mkdir(parents=True, exist_ok=True)
    malformed = '{"providers":{"unrelated":[{"id":42}]}}'
    models.write_text(malformed)
    assert not (await local_provider_operation(session, "local_provider_save", payload))["ok"]
    assert models.read_text() == malformed
    models.write_text('{}')
    project = session.settings_manager._project_path
    project.parent.mkdir(parents=True)
    project.write_text('{"defaultModel":"other"}')
    assert not (await local_provider_operation(session, "local_provider_save", payload))["ok"]
    assert models.read_text() == '{}'


@pytest.mark.asyncio
async def test_discovery_bounds_and_safe_failure(session, monkeypatch):
    original = httpx.AsyncClient
    def client(**kwargs):
        return original(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'x' * 1_000_001)), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = await local_provider_operation(session, "local_provider_models",
                                            {"provider": "ollama", "url": "http://localhost"})
    assert not result["ok"] and "too large" in result["error"]


@pytest.mark.asyncio
async def test_partial_write_and_switch_failure(session, monkeypatch):
    import one.core.local_provider_setup as setup

    payload = {"provider": "ollama", "url": "http://localhost", "model": "m"}
    original = setup.atomic_write_text
    def fail_settings(path, text):
        if path == session.settings_manager._global_path:
            raise OSError("secret")
        original(path, text)
    monkeypatch.setattr(setup, "atomic_write_text", fail_settings)
    result = await local_provider_operation(session, "local_provider_save", payload)
    assert not result["ok"] and "Models were saved" in result["error"] and "secret" not in result["error"]
    assert session.model_registry._models_path.exists()
    monkeypatch.setattr(setup, "atomic_write_text", original)
    session.set_model.side_effect = RuntimeError("secret")
    result = await local_provider_operation(session, "local_provider_save", payload)
    assert not result["ok"] and "Configuration was saved" in result["error"] and "secret" not in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("contents", ['[]', '{broken', '{"defaultModel":42}', '{"a":1,"a":2}'])
async def test_malformed_settings_never_overwritten(session, contents):
    path = session.settings_manager._global_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    result = await local_provider_operation(session, "local_provider_save", {
        "provider": "ollama", "url": "http://host/v1", "model": "m"})
    assert not result["ok"]
    assert path.read_text() == contents
    assert not session.model_registry._models_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 401, 404, 500])
async def test_discovery_http_errors_are_actionable_and_redacted(session, monkeypatch, status):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            status, headers={"location": "http://secret.test"}, text="secret server details")), **kwargs))
    result = await local_provider_operation(session, "local_provider_models", {
        "provider": "llama.cpp", "url": "http://host"})
    assert not result["ok"]
    assert "server is running" in result["error"]
    assert "start Ollama or llama-server" in result["error"]
    assert "loaded model" in result["error"]
    assert "container" in result["error"] and "host.docker.internal" in result["error"]
    assert "secret" not in result["error"]


@pytest.mark.asyncio
async def test_discovery_count_bound_and_invalid_data(session, monkeypatch):
    original = httpx.AsyncClient
    response = {"data": [{"id": str(i)} for i in range(300)]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)), **kwargs))
    result = await local_provider_operation(session, "local_provider_models", {
        "provider": "llama.cpp", "url": "http://host"})
    assert len(result["models"]) == 200
    response = {"data": "not a list"}
    result = await local_provider_operation(session, "local_provider_models", {
        "provider": "llama.cpp", "url": "http://host"})
    assert not result["ok"] and "invalid model data" in result["error"]

@pytest.mark.asyncio
async def test_stale_save_has_no_disk_or_memory_mutation(session):
    before = list(session.model_registry._models)
    result = await local_provider_operation(session, "local_provider_save", {
        "provider": "ollama", "url": "http://host", "model": "m"}, is_current=lambda: False)
    assert not result["ok"] and "Nothing was saved" in result["error"]
    assert not session.settings_manager._global_path.exists()
    assert not session.model_registry._models_path.exists()
    assert session.model_registry._models == before
    session.set_model.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("character", ['a', '"', '\\', '界', '😀'])
async def test_discovery_serialized_budget(session, monkeypatch, character):
    from one.core.threaded_agent_runtime import DEFAULT_COMMAND_DTO_MAX_BYTES, _json_dto

    original = httpx.AsyncClient
    response = {"data": [{"id": str(i) + character * 1020} for i in range(200)]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)), **kwargs))
    result = await local_provider_operation(session, "local_provider_models", {
        "provider": "llama.cpp", "url": "http://host/" + '界' * 1900})
    assert result["ok"] and 0 < len(result["models"]) < 200
    assert len(json.dumps(result["models"]).encode()) <= 24 * 1024
    for ascii_only in (True, False):
        assert len(json.dumps(result, ensure_ascii=ascii_only).encode()) < DEFAULT_COMMAND_DTO_MAX_BYTES
    assert _json_dto(result, label="result", maximum=DEFAULT_COMMAND_DTO_MAX_BYTES) == result

@pytest.mark.asyncio
@pytest.mark.parametrize("busy_field", ["is_streaming", "_is_compacting"])
async def test_idle_is_rechecked_at_commit_boundary(session, busy_field):
    def still_current():
        setattr(session, busy_field, True)
        return True

    result = await local_provider_operation(session, "local_provider_save", {
        "provider": "ollama", "url": "http://host", "model": "m"}, is_current=still_current)
    assert not result["ok"] and "Wait" in result["error"]
    assert not session.settings_manager._global_path.exists()
    assert not session.model_registry._models_path.exists()
    session.set_model.assert_not_awaited()
