from __future__ import annotations

import os
from typing import Any

from .anthropic import AnthropicAdapter
from .base import ProviderAdapter
from .gemini import GeminiAdapter
from .openai_compatible import OpenAICompatibleAdapter


def build_provider_registry() -> dict[str, ProviderAdapter]:
    openai_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com")
    azure_base = os.getenv("AZURE_OPENAI_BASE_URL")
    openrouter_base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api")
    ollama_cloud_base = os.getenv("OLLAMA_CLOUD_BASE_URL", "https://ollama.com")

    registry: dict[str, ProviderAdapter] = {
        "openai": OpenAICompatibleAdapter("openai", openai_base),
        "anthropic": AnthropicAdapter(),
        "gemini": GeminiAdapter(),
        "openrouter": OpenAICompatibleAdapter("openrouter", openrouter_base),
        # Ollama Cloud rejects some OpenAI-specific fields like reasoning_effort.
        "ollama-cloud": OpenAICompatibleAdapter(
            "ollama-cloud",
            ollama_cloud_base,
            supports_reasoning_effort=False,
            default_temperature=None,
        ),
    }

    if azure_base:
        registry["azure-openai"] = OpenAICompatibleAdapter("azure-openai", azure_base)

    return registry
