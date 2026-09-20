from __future__ import annotations

import os

from .anthropic import AnthropicAdapter
from .base import ProviderAdapter
from .codex_responses import CodexResponsesAdapter
from .gemini import GeminiAdapter
from .ollama import OllamaCloudAdapter
from .openai_compatible import OpenAICompatibleAdapter


def build_provider_registry() -> dict[str, ProviderAdapter]:
    openai_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com")
    azure_base = os.getenv("AZURE_OPENAI_BASE_URL")
    openrouter_base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api")
    ollama_cloud_base = os.getenv("OLLAMA_CLOUD_BASE_URL", "https://ollama.com")
    llama_cpp_base = os.getenv("LLAMA_CPP_BASE_URL", "http://127.0.0.1:8080")
    ollama_base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    xai_base = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
    deepseek_base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    mistral_base = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
    groq_base = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

    registry: dict[str, ProviderAdapter] = {
        "openai": OpenAICompatibleAdapter("openai", openai_base),
        "anthropic": AnthropicAdapter(),
        "gemini": GeminiAdapter(),
        # ChatGPT/Codex subscription backend (Responses API, OAuth only).
        "chatgpt": CodexResponsesAdapter(),
        "openrouter": OpenAICompatibleAdapter(
            "openrouter", openrouter_base, reasoning_mode="openrouter"
        ),
        "ollama-cloud": OllamaCloudAdapter(ollama_cloud_base),
        # llama.cpp server mode (OpenAI-compatible endpoint, typically local).
        "llama.cpp": OpenAICompatibleAdapter(
            "llama.cpp",
            llama_cpp_base,
            supports_reasoning_effort=False,
            default_temperature=None,
        ),
        # Local Ollama server (OpenAI-compatible endpoint, no API key).
        "ollama": OpenAICompatibleAdapter(
            "ollama",
            ollama_base,
            endpoint="/chat/completions",
            supports_reasoning_effort=False,
            default_temperature=None,
        ),
        "xai": OpenAICompatibleAdapter("xai", xai_base, endpoint="/chat/completions"),
        "deepseek": OpenAICompatibleAdapter("deepseek", deepseek_base),
        "mistral": OpenAICompatibleAdapter("mistral", mistral_base, endpoint="/chat/completions"),
        "groq": OpenAICompatibleAdapter("groq", groq_base, endpoint="/chat/completions"),
    }

    if azure_base:
        registry["azure-openai"] = OpenAICompatibleAdapter("azure-openai", azure_base)

    return registry
