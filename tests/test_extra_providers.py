from __future__ import annotations

import pytest

from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.providers.registry import build_provider_registry


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure provider base-url env vars are unset for deterministic tests."""
    for var in ("XAI_BASE_URL", "DEEPSEEK_BASE_URL", "MISTRAL_BASE_URL", "GROQ_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_extra_providers_registered_with_default_base_urls() -> None:
    registry = build_provider_registry()
    expected = {
        "xai": "https://api.x.ai/v1",
        "deepseek": "https://api.deepseek.com",
        "mistral": "https://api.mistral.ai/v1",
        "groq": "https://api.groq.com/openai/v1",
    }
    for name, base in expected.items():
        assert name in registry
        assert registry[name].base_url == base


def test_extra_providers_request_routes() -> None:
    """Full request URL = base_url.rstrip('/') + endpoint for all new providers."""
    registry = build_provider_registry()
    expected = {
        "xai": "https://api.x.ai/v1/chat/completions",
        "deepseek": "https://api.deepseek.com/v1/chat/completions",
        "mistral": "https://api.mistral.ai/v1/chat/completions",
        "groq": "https://api.groq.com/openai/v1/chat/completions",
        "ollama": "http://localhost:11434/v1/chat/completions",
    }
    for name, expected_route in expected.items():
        adapter = registry[name]
        route = adapter.base_url.rstrip("/") + adapter.endpoint
        assert route == expected_route, f"{name}: {route!r} != {expected_route!r}"


def test_extra_provider_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_BASE_URL", "http://localhost:9999/v1")
    registry = build_provider_registry()
    assert registry["groq"].base_url == "http://localhost:9999/v1"
    # Verify joined route: base_url + endpoint
    route = registry["groq"].base_url.rstrip("/") + registry["groq"].endpoint
    assert route == "http://localhost:9999/v1/chat/completions"


def test_extra_provider_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = AuthStorage.in_memory()
    assert auth.env_var_for_provider("xai") == "XAI_API_KEY"
    assert auth.env_var_for_provider("deepseek") == "DEEPSEEK_API_KEY"
    assert auth.env_var_for_provider("mistral") == "MISTRAL_API_KEY"
    assert auth.env_var_for_provider("groq") == "GROQ_API_KEY"
    monkeypatch.setenv("MISTRAL_API_KEY", "mm-key")
    assert auth.get_api_key("mistral") == "mm-key"


def test_extra_providers_builtin_models() -> None:
    registry = ModelRegistry.create(AuthStorage.in_memory())
    for provider, model_id in [
        ("xai", "grok-4"),
        ("deepseek", "deepseek-chat"),
        ("deepseek", "deepseek-reasoner"),
        ("mistral", "mistral-large-latest"),
        ("groq", "llama-3.3-70b-versatile"),
    ]:
        assert registry.find(provider, model_id) is not None


def test_extra_providers_available_when_key_configured() -> None:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("xai", "x-key")
    registry = ModelRegistry.create(auth)
    available = {m.provider for m in registry.get_available()}
    assert "xai" in available
