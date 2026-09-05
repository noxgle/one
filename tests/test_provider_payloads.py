from __future__ import annotations

from one.providers.openai_compatible import OpenAICompatibleAdapter
from one.providers.registry import build_provider_registry


def test_openai_compatible_payload_with_reasoning() -> None:
    adapter = OpenAICompatibleAdapter("openai", "https://api.openai.com")
    payload = adapter._build_payload(
        model="gpt-4.1",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="high",
    )
    assert payload["model"] == "gpt-4.1"
    assert payload["messages"][0]["content"] == "hi"
    assert payload["temperature"] == 0.1
    assert payload["reasoning_effort"] == "high"


def test_openai_compatible_payload_without_reasoning() -> None:
    adapter = OpenAICompatibleAdapter(
        "ollama-cloud",
        "https://ollama.com",
        supports_reasoning_effort=False,
        default_temperature=None,
    )
    payload = adapter._build_payload(
        model="glm-5:cloud",
        messages=[{"role": "user", "content": "hi"}],
        thinking_level="xhigh",
    )
    assert payload["model"] == "glm-5:cloud"
    assert payload["messages"][0]["content"] == "hi"
    assert "reasoning_effort" not in payload
    assert "temperature" not in payload


def test_openai_compatible_headers_without_api_key() -> None:
    adapter = OpenAICompatibleAdapter("llama.cpp", "http://127.0.0.1:8080")
    headers = adapter._build_headers("")
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_provider_registry_includes_llama_cpp() -> None:
    registry = build_provider_registry()
    assert "llama.cpp" in registry


def test_provider_registry_llama_cpp_uses_env_base_url(monkeypatch) -> None:
    monkeypatch.setenv("LLAMA_CPP_BASE_URL", "http://192.0.2.38:8089")
    registry = build_provider_registry()
    provider = registry["llama.cpp"]
    assert isinstance(provider, OpenAICompatibleAdapter)
    assert provider.base_url == "http://192.0.2.38:8089"


def test_openai_compatible_with_base_url_returns_reconfigured_adapter() -> None:
    adapter = OpenAICompatibleAdapter(
        "llama.cpp",
        "http://127.0.0.1:8080",
        supports_reasoning_effort=False,
        default_temperature=None,
    )
    changed = adapter.with_base_url("http://192.0.2.38:8089")
    assert changed.base_url == "http://192.0.2.38:8089"
    assert changed.supports_reasoning_effort is False
    assert changed.default_temperature is None
