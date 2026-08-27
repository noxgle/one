from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from one.config import get_models_path
from one.core.auth_storage import AuthStorage
from one.core.persistence import atomic_write_text, ensure_private_dir, ensure_private_file, load_json_text_safe
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
    # ChatGPT/Codex subscription backend (Responses API); the real slug list
    # is fetched live from /models after login. Current generation as of 2026-08;
    # catalog volatile — windows unpublished for Codex backend, gauge falls back.
    ModelInfo("chatgpt", "gpt-5.6-sol", reasoning=True, context_window=None),
    ModelInfo("chatgpt", "gpt-5.6-terra", reasoning=True, context_window=None),
    ModelInfo("chatgpt", "gpt-5.6-luna", reasoning=True, context_window=None),
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
        self._errors: list[dict[str, Any]] = []
        # Guard: refuse to overwrite a known-malformed models.json.
        self._models_path_is_locked: bool = False
        p = self._models_path
        if p.exists():
            data, err = load_json_text_safe(p)
            if err is not None:
                self._errors.append({"scope": "models", "error": err})
                self._models_path_is_locked = True
            elif isinstance(data, dict):
                providers = data.get("providers", {})
                if not isinstance(providers, dict):
                    self._errors.append(
                        {"scope": "models", "error": ValueError("models.json providers is not a dict")}
                    )
                else:
                    for provider, models in providers.items():
                        if not isinstance(models, list):
                            self._errors.append(
                                {
                                    "scope": "models",
                                    "error": ValueError(
                                        f"models.json providers.{provider} is not a list"
                                    ),
                                }
                            )
                            continue
                        for model in models:
                            if isinstance(model, dict) and model.get("id"):
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
            else:
                # Valid JSON but not a dict.
                self._errors.append(
                    {"scope": "models", "error": ValueError("models state is not a JSON object")}
                )
                self._models_path_is_locked = True
            # Tighten existing file/dir on POSIX.
            ensure_private_dir(p.parent)
            ensure_private_file(p, 0o600)
        self._models = self._dedupe_models(self._models)

    def drain_errors(self) -> list[dict[str, Any]]:
        out = self._errors[:]
        self._errors = []
        return out

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
        if self._auth.get_oauth_record(model.provider):
            # Subscription OAuth login (Phase 18) counts as configured auth.
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
        headers: dict[str, Any] = {}
        if not key or _is_placeholder_key(key):
            # Fall back to a subscription-OAuth access token when present
            # (Phase 18); ChatGPT additionally needs the account id header.
            record = self._auth.get_oauth_record(model.provider)
            if record and record.get("access"):
                key = str(record["access"])
                if record.get("accountId"):
                    headers["ChatGPT-Account-Id"] = str(record["accountId"])
        if not key or _is_placeholder_key(key):
            env_var = self._auth.env_var_for_provider(model.provider)
            hint = f" (set {env_var} or use /login)" if env_var else " (use /login)"
            return {"ok": False, "error": f"No API key found for {model.provider}{hint}"}
        return {"ok": True, "apiKey": key, "headers": headers}

    async def ensure_oauth_fresh(self, provider: str) -> None:
        """Refresh the provider's subscription OAuth token when near expiry.

        No-op for providers without an OAuth flow or without a stored record.
        Raises ``OAuthError`` when the refresh fails.
        """
        from one.core.oauth import ensure_fresh_token, is_oauth_provider

        if not is_oauth_provider(provider):
            return
        if not self._auth.get_oauth_record(provider):
            return
        await ensure_fresh_token(self._auth, provider)

    def set_oauth_record(self, provider: str, record: dict[str, Any]) -> None:
        """Store/replace the provider's OAuth record (delegates to auth storage).

        HOTFIX-4: ``provider_login.run_oauth_login`` receives only the registry
        (sessions do not expose auth storage), so the write path must be
        delegated here just like the reads above.
        """
        self._auth.set_oauth_record(provider, record)

    @staticmethod
    def _normalize_entries(model_ids: list[str | dict[str, Any]]) -> list[tuple[str, int | None]]:
        """Accept plain ids or {"id", "contextWindow"} dicts → (id, window) pairs."""
        out: list[tuple[str, int | None]] = []
        for entry in model_ids:
            if isinstance(entry, dict) and entry.get("id"):
                window = entry.get("contextWindow")
                out.append((entry["id"], window if isinstance(window, int) and window > 0 else None))
            elif isinstance(entry, str):
                out.append((entry, None))
        return out

    def register_models(self, provider: str, model_ids: list[str | dict[str, Any]]) -> int:
        """Register fetched models in memory (dedupe); returns how many were added.

        Entries may be plain ids or ``{"id", "contextWindow"}`` dicts. Existing
        registered entries without a context window get enriched when a real
        one arrives (e.g. on refresh).
        """
        added = 0
        for mid, window in self._normalize_entries(model_ids):
            existing = self.find(provider, mid)
            if existing is not None:
                if not existing.context_window and window:
                    existing.context_window = window
                continue
            self._models.append(
                ModelInfo(
                    provider=provider,
                    id=mid,
                    reasoning=True,
                    context_window=window,
                    base_url=None,
                    tool_parser=None,
                )
            )
            added += 1
        return added

    def persist_models(self, provider: str, model_ids: list[str | dict[str, Any]]) -> None:
        """Merge fetched models into models.json (providers.<provider>).

        Entries may be plain ids or ``{"id", "contextWindow"}`` dicts. Existing
        entries keep their fields (url/toolParser/contextWindow); only missing
        ids are added with defaults, and a known contextWindow is filled in
        when the stored entry lacks one. Creates the file when absent.

        Raises RuntimeError if the existing models.json is known to be malformed.
        """
        # If we already know the file is corrupt, refuse to overwrite it.
        if self._models_path_is_locked:
            raise RuntimeError(
                "models.json is malformed or not a JSON object; "
                "repair it before the agent can persist models"
            )
        data: dict[str, Any] = {}
        if self._models_path.exists():
            loaded_data, err = load_json_text_safe(self._models_path)
            if err is not None:
                # First time encountering the error during persist (lazy check).
                self._errors.append({"scope": "models", "error": err})
                self._models_path_is_locked = True
                raise RuntimeError(
                    "models.json is malformed or not a JSON object; "
                    "repair it before the agent can persist models"
                )
            if isinstance(loaded_data, dict):
                data = loaded_data
            else:
                self._errors.append(
                    {"scope": "models", "error": ValueError("models state is not a JSON object")}
                )
                self._models_path_is_locked = True
                raise RuntimeError(
                    "models.json is malformed or not a JSON object; "
                    "repair it before the agent can persist models"
                )
        providers = data.setdefault("providers", {})
        existing: dict[str, dict[str, Any]] = {}
        for m in providers.get(provider, []) or []:
            if isinstance(m, dict) and m.get("id"):
                existing[m["id"]] = m
        for mid, window in self._normalize_entries(model_ids):
            entry = existing.setdefault(mid, {"id": mid, "reasoning": True})
            if window and not entry.get("contextWindow"):
                entry["contextWindow"] = window
        providers[provider] = list(existing.values())
        atomic_write_text(self._models_path, json.dumps(data, indent=2) + "\n")
        # After a successful write the file is known-good again.
        self._models_path_is_locked = False

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
