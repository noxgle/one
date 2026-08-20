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
    ModelInfo(
        "llama.cpp",
        "local",
        reasoning=False,
        context_window=32_768,
        # No hardcoded base URL: default to the provider registry, which reads
        # LLAMA_CPP_BASE_URL / --llama-cpp-url. A models.json `url` still wins.
        base_url=None,
        tool_parser=[{"type": "raw-function-call"}, {"type": "json"}],
    ),
    ModelInfo(
        "ollama",
        "llama3.1",
        reasoning=False,
        context_window=32_768,
        # No hardcoded base URL: default to the provider registry, which reads
        # OLLAMA_BASE_URL / --ollama-url. A models.json `url` still wins.
        base_url=None,
        tool_parser=[{"type": "raw-function-call"}, {"type": "json"}],
    ),
    ModelInfo("xai", "grok-4", reasoning=True, context_window=256_000),
    ModelInfo("deepseek", "deepseek-chat", reasoning=False, context_window=128_000),
    ModelInfo("deepseek", "deepseek-reasoner", reasoning=True, context_window=128_000),
    ModelInfo("mistral", "mistral-large-latest", reasoning=True, context_window=128_000),
    ModelInfo("groq", "llama-3.3-70b-versatile", reasoning=False, context_window=128_000),
]

NO_AUTH_PROVIDERS: set[str] = {"llama.cpp", "ollama"}


def _is_placeholder_key(key: str | None) -> bool:
    """True when a key is missing or is a template placeholder (e.g. from /login defaults).

    Placeholder keys must not count as "configured auth", otherwise providers
    like openai become "available" and get selected before working local models.
    """
    if not key:
        return True
    low = key.strip().lower()
    if (
        low.startswith("your_")
        or low in {"changeme", "sk-xxx", "none", "null", "placeholder", "enter", "insert", "xxx"}
        or ("your" in low and "here" in low)
        or (low.startswith("<") and low.endswith(">"))
    ):
        return True
    return False


class ModelRegistry:
    def __init__(self, auth_storage: AuthStorage, models_path: str | None = None) -> None:
        self._auth = auth_storage
        self._models: list[ModelInfo] = list(BUILTIN_MODELS)
        self._models_path = Path(models_path or get_models_path())
        p = self._models_path
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
                                base_url=model.get("url") or model.get("baseUrl"),
                                tool_parser=model.get("toolParser"),
                            )
                        )
            except Exception:
                pass
        self._models = self._dedupe_models(self._models)

    @staticmethod
    def _dedupe_models(models: list[ModelInfo]) -> list[ModelInfo]:
        out: list[ModelInfo] = []
        index_by_key: dict[tuple[str, str], int] = {}
        for m in models:
            key = (m.provider, m.id)
            idx = index_by_key.get(key)
            if idx is None:
                index_by_key[key] = len(out)
                out.append(m)
            else:
                # Later definitions (e.g. models.json) override builtin entries.
                out[idx] = m
        return out

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

    def resolve(self, provider: str, model_id: str, *, allow_dynamic: bool = False) -> ModelInfo | None:
        found = self.find(provider, model_id)
        if found:
            return found
        if allow_dynamic and provider.strip() and model_id.strip():
            return ModelInfo(
                provider=provider,
                id=model_id,
                reasoning=True,
                context_window=None,
                base_url=None,
                tool_parser=None,
            )
        return None

    def has_configured_auth(self, model: ModelInfo) -> bool:
        if model.provider in NO_AUTH_PROVIDERS:
            return True
        return not _is_placeholder_key(self._auth.get_api_key(model.provider))

    def get_available(self) -> list[ModelInfo]:
        return [m for m in self._models if self.has_configured_auth(m)]

    def select_default(self, default_provider: str | None, default_model: str | None) -> ModelInfo | None:
        """Pick the model to use when none was requested explicitly.

        Prefer an exact provider+model match, then the default provider's first
        registered model, then the first available model. Never silently jumps
        to a different provider just because the default model id is stale.
        """
        avail = self.get_available()
        if not avail:
            allm = self.all()
            return allm[0] if allm else None
        if default_provider and default_model:
            found = next((m for m in avail if m.provider == default_provider and m.id == default_model), None)
            if found:
                return found
        if default_provider:
            found = next((m for m in avail if m.provider == default_provider), None)
            if found:
                return found
        return avail[0]

    def get_api_key_and_headers(self, model: ModelInfo) -> dict[str, Any]:
        if model.provider in NO_AUTH_PROVIDERS:
            return {"ok": True, "apiKey": "", "headers": {}}
        key = self._auth.get_api_key(model.provider)
        if _is_placeholder_key(key):
            env_var = self._auth.env_var_for_provider(model.provider)
            hint = f" (set {env_var} or use /login)" if env_var else " (use /login)"
            return {"ok": False, "error": f"No API key found for {model.provider}{hint}"}
        return {"ok": True, "apiKey": key, "headers": {}}

    def register_models(self, provider: str, model_ids: list[str]) -> int:
        """Register fetched model ids in memory (dedupe); returns how many were added."""
        added = 0
        for mid in model_ids:
            if not self.find(provider, mid):
                self._models.append(
                    ModelInfo(
                        provider=provider,
                        id=mid,
                        reasoning=True,
                        context_window=None,
                        base_url=None,
                        tool_parser=None,
                    )
                )
                added += 1
        return added

    def persist_models(self, provider: str, model_ids: list[str]) -> None:
        """Merge fetched model ids into models.json (providers.<provider>).

        Existing entries keep their fields (url/toolParser/contextWindow);
        only missing ids are added with defaults. Creates the file when absent.
        """
        data: dict[str, Any] = {}
        if self._models_path.exists():
            try:
                data = json.loads(self._models_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        providers = data.setdefault("providers", {})
        existing: dict[str, dict[str, Any]] = {}
        for m in providers.get(provider, []) or []:
            if isinstance(m, dict) and m.get("id"):
                existing[m["id"]] = m
        for mid in model_ids:
            existing.setdefault(mid, {"id": mid, "reasoning": True})
        providers[provider] = list(existing.values())
        self._models_path.parent.mkdir(parents=True, exist_ok=True)
        self._models_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def set_stored_api_key(self, provider: str, api_key: str) -> None:
        self._auth.set_stored_api_key(provider, api_key)

    def remove_stored_api_key(self, provider: str) -> None:
        self._auth.remove_stored_api_key(provider)

    def requires_api_key(self, provider: str) -> bool:
        return provider not in NO_AUTH_PROVIDERS

    def get_provider_auth_status(self, provider: str) -> dict[str, Any]:
        return {
            "provider": provider,
            "requiresApiKey": self.requires_api_key(provider),
            "configured": bool(self._auth.get_api_key(provider)),
            "envVar": self._auth.env_var_for_provider(provider),
        }

    def register_provider(self, _name: str, _config: dict[str, Any]) -> None:
        return

    @classmethod
    def create(cls, auth_storage: AuthStorage, models_path: str | None = None) -> "ModelRegistry":
        return cls(auth_storage, models_path)
