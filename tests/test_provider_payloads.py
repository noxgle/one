from __future__ import annotations

from one.providers.openai_compatible import OpenAICompatibleAdapter


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
