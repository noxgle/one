from __future__ import annotations

import struct

import pytest

from one.providers.openai_compatible import OpenAICompatibleAdapter
from one.providers.registry import build_provider_registry

# ---------------------------------------------------------------------------
# Test data helpers (shared)
# ---------------------------------------------------------------------------


def _make_png() -> bytes:
    """Minimal valid PNG (1×1 transparent pixel)."""
    sig = b"\x89PNG\r\n\x1a\n"
    width = struct.pack(">I", 1)
    height = struct.pack(">I", 1)
    bit_depth_color = b"\x08\x06"
    ihdr_data = width + height + bit_depth_color + b"\x00\x00"
    ihdr_crc = b"\x00\x00\x00\x00"
    ihdr = b"\x00\x00\x00\x0dIHDR" + ihdr_data + ihdr_crc
    idat = b"\x00\x00\x00\x03IDAT\x08\x99c\xfc\xcf\x00\x00\x00\x02\x00\x01"
    idat_crc = b"\x00\x00\x00\x00"
    idat += idat_crc
    iend = b"\x00\x00\x00\x00IEND" + b"\xaeB`\x82"
    return sig + ihdr + idat + iend


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


def test_openai_compatible_payload_without_images_works() -> None:
    """When no images are present, payload is built normally (phase-free behavior)."""
    adapter = OpenAICompatibleAdapter("openai", "https://api.openai.com")
    payload = adapter._build_payload(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hello"}],
        thinking_level="off",
    )
    assert payload["model"] == "gpt-4o"
    assert payload["messages"][0]["content"] == "hello"
    # No image_url parts should be present
    content = payload["messages"][0]["content"]
    if isinstance(content, list):
        assert all(p.get("type") == "text" for p in content)


# ---------------------------------------------------------------------------
# Codex Responses API payload tests
# ---------------------------------------------------------------------------


class TestCodexPayload:
    def test_codex_text_only_payload(self) -> None:
        """Text-only payload: input_text content part, no image parts."""
        from one.providers.codex_responses import CodexResponsesAdapter

        adapter = CodexResponsesAdapter()
        messages = [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        payload = adapter._build_payload("gpt-5.6-sol", messages, "high", stream=False, max_tokens=77)
        assert payload["model"] == "gpt-5.6-sol"
        assert payload["instructions"] == "be terse"
        assert payload["store"] is False and payload["stream"] is False
        assert payload["max_output_tokens"] == 77
        assert payload["reasoning"] == {"effort": "high"}
        # HOTFIX-5: backend requires Responses message items with explicit type.
        assert all(i["type"] == "message" for i in payload["input"])
        assert [i["content"][0]["type"] for i in payload["input"]] == ["input_text", "output_text"]
        assert [i["role"] for i in payload["input"]] == ["user", "assistant"]

    def test_codex_payload_instructions_fallback(self) -> None:
        """Empty instructions fall back to base Codex prompt."""
        from one.providers.codex_responses import CodexResponsesAdapter

        adapter = CodexResponsesAdapter()
        p = adapter._build_payload("gpt-5.6-sol", [{"role": "user", "content": "hi"}], "medium", stream=False)
        assert p["instructions"].startswith("You are Codex")

    def test_codex_text_only_no_images(self) -> None:
        """When no images are present, _build_payload works normally."""
        from one.providers.codex_responses import CodexResponsesAdapter

        adapter = CodexResponsesAdapter()
        payload = adapter._build_payload(
            "gpt-5.6-sol",
            [{"role": "user", "content": "hello"}],
            "off",
            stream=False,
            images=None,
            storage_dir="",
        )
        user_item = payload["input"][0]
        assert user_item["role"] == "user"
        assert user_item["content"][0]["type"] == "input_text"
        assert user_item["content"][0]["text"] == "hello"
        assert all(p.get("type") != "input_image" for p in user_item["content"])

    def test_codex_payload_with_images_as_input_image(self) -> None:
        """Images are serialized as input_image parts alongside input_text."""
        from one.providers.codex_responses import CodexResponsesAdapter

        adapter = CodexResponsesAdapter()
        # Mock _read_blob to return test PNG data.
        png = _make_png()
        adapter._read_blob = lambda storage_dir, blob_hash: png
        messages = [{"role": "user", "content": "look at this"}]
        images = [{"blobHash": "abc", "mime": "image/png"}]
        payload = adapter._build_payload(
            "gpt-5.6-sol", messages, "medium", stream=False,
            images=images, storage_dir="/tmp",
        )
        user_item = payload["input"][0]
        content = user_item["content"]
        assert isinstance(content, list)
        text_parts = [p for p in content if p.get("type") == "input_text"]
        image_parts = [p for p in content if p.get("type") == "input_image"]
        assert len(text_parts) == 1
        assert text_parts[0]["text"] == "look at this"
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"].startswith("data:image/png;base64,")

    def test_codex_payload_image_blob_missing_raises_no_http(self) -> None:
        """Missing blob raises MissingBlobError without making HTTP."""
        from one.providers.codex_responses import (
            CodexResponsesAdapter,
            MissingBlobError,
        )

        adapter = CodexResponsesAdapter()
        adapter._read_blob = lambda storage_dir, blob_hash: None
        messages = [{"role": "user", "content": "hello"}]
        images = [{"blobHash": "missing", "mime": "image/png"}]
        with pytest.raises(MissingBlobError, match="missing or corrupt before HTTP"):
            adapter._build_payload(
                "gpt-5.6-sol", messages, "medium", stream=False,
                images=images, storage_dir="/tmp",
            )

    def test_codex_payload_missing_blob_hash_raises(self) -> None:
        """Image ref without blob_hash raises MissingBlobError."""
        from one.providers.codex_responses import (
            CodexResponsesAdapter,
            MissingBlobError,
        )

        adapter = CodexResponsesAdapter()
        messages = [{"role": "user", "content": "hello"}]
        images = [{"mime": "image/png"}]  # no blobHash
        with pytest.raises(MissingBlobError, match="missing blob_hash"):
            adapter._build_payload(
                "gpt-5.6-sol", messages, "medium", stream=False,
                images=images, storage_dir="/tmp",
            )

    def test_codex_payload_assistant_output_text_unchanged(self) -> None:
        """Assistant turns keep output_text content parts."""
        from one.providers.codex_responses import CodexResponsesAdapter

        adapter = CodexResponsesAdapter()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        payload = adapter._build_payload("gpt-5.6-sol", messages, "off", stream=False)
        assistant_item = payload["input"][1]
        assert assistant_item["role"] == "assistant"
        assert assistant_item["content"][0]["type"] == "output_text"
        assert assistant_item["content"][0]["text"] == "a"
