from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from one.config import get_models_path
from one.core.auth_storage import AuthStorage
from one.core.types import ModelInfo

BUILTIN_MODELS: list[ModelInfo] = [
    ModelInfo("openai", "gpt-4.1", reasoning=True, context_window=1_000_000),
    ModelInfo("openai", "gpt-4o", reasoning=True, context_window=128_000),
    ModelInfo("anthropic", "claude-3-7-sonnet-latest", reasoning=True, context_window=200_000),
    ModelInfo("anthropic", "claude-3-5-haiku-latest", reasoning=True, context_window=200_000),
    ModelInfo("gemini", "gemini-2.5-pro", reasoning=True, context_window=1_000_000),
    ModelInfo("gemini", "gemini-2.5-flash", reasoning=True, context_window=1_000_000),
    ModelInfo("openrouter", "openai/gpt-4.1", reasoning=True, context_window=1_000_000),
    ModelInfo("ollama-cloud", "glm-5:cloud", reasoning=True, context_window=128_000),
]


class ModelRegistry:
    def __init__(self, auth_storage: AuthStorage, models_path: str | None = None) -> None:
        self._auth = auth_storage
        self._models: list[ModelInfo] = list(BUILTIN_MODELS)
        p = Path(models_path or get_models_path())
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                for provider, models in data.get("providers", {}).items():
                    for model in models:
                        self._models.append(
                            ModelInfo(
                                provider=provider,
                                id=model["id"],
                                reasoning=model.get("reasoning", True),
                                context_window=model.get("contextWindow"),
                            )
                        )
            except Exception:
                pass

    def all(self) -> list[ModelInfo]:
        return list(self._models)

    def providers(self) -> list[str]:
        return sorted({m.provider for m in self._models})

    def models_for_provider(self, provider: str) -> list[ModelInfo]:
        return [m for m in self._models if m.provider == provider]

    def find(self, provider: str, model_id: str) -> ModelInfo | None:
        for m in self._models:
            if m.provider == provider and m.id == model_id:
                return m
        return None

    def has_configured_auth(self, model: ModelInfo) -> bool:
        return bool(self._auth.get_api_key(model.provider))

    def get_available(self) -> list[ModelInfo]:
        return [m for m in self._models if self.has_configured_auth(m)]

    def get_api_key_and_headers(self, model: ModelInfo) -> dict[str, Any]:
        key = self._auth.get_api_key(model.provider)
        if not key:
            env_var = self._auth.env_var_for_provider(model.provider)
            hint = f" (set {env_var} or use /login)" if env_var else " (use /login)"
            return {"ok": False, "error": f"No API key found for {model.provider}{hint}"}
        return {"ok": True, "apiKey": key, "headers": {}}

    def set_stored_api_key(self, provider: str, api_key: str) -> None:
        self._auth.set_stored_api_key(provider, api_key)

    def register_provider(self, _name: str, _config: dict[str, Any]) -> None:
        return

    @classmethod
    def create(cls, auth_storage: AuthStorage, models_path: str | None = None) -> "ModelRegistry":
        return cls(auth_storage, models_path)
