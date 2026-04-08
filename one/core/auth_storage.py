from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from one.config import get_auth_path

PROVIDER_ENV_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure-openai": "AZURE_OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama-cloud": "OLLAMA_CLOUD_API_KEY",
    "llama.cpp": "LLAMA_CPP_API_KEY",
}


class AuthStorage:
    @staticmethod
    def _default_env_var(provider: str) -> str:
        return provider.upper().replace("-", "_").replace(".", "_") + "_API_KEY"

    def __init__(self, path: str | None = None, in_memory: bool = False) -> None:
        self._path = Path(path or get_auth_path()) if not in_memory else None
        self._runtime: dict[str, str] = {}
        self._data: dict[str, Any] = {}
        self._in_memory = in_memory
        if self._path and self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def set_runtime_api_key(self, provider: str, api_key: str) -> None:
        self._runtime[provider] = api_key

    def set_stored_api_key(self, provider: str, api_key: str) -> None:
        self._data.setdefault("apiKeys", {})[provider] = api_key
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def get_api_key(self, provider: str) -> str | None:
        if provider in self._runtime:
            return self._runtime[provider]
        stored = self._data.get("apiKeys", {}).get(provider)
        if stored:
            return stored
        env = PROVIDER_ENV_MAP.get(provider) or self._default_env_var(provider)
        val = os.getenv(env)
        if val:
            return val
        return None

    def env_var_for_provider(self, provider: str) -> str | None:
        return PROVIDER_ENV_MAP.get(provider) or self._default_env_var(provider)

    @classmethod
    def create(cls, path: str | None = None) -> "AuthStorage":
        return cls(path=path)

    @classmethod
    def in_memory(cls) -> "AuthStorage":
        return cls(in_memory=True)
