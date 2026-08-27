from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from one.config import get_auth_path
from one.core.persistence import atomic_write_text, ensure_private_dir, ensure_private_file, load_json_text_safe

PROVIDER_ENV_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure-openai": "AZURE_OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "ollama-cloud": "OLLAMA_CLOUD_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "llama.cpp": "LLAMA_CPP_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "chatgpt": "CHATGPT_API_KEY",
}


class AuthStorage:
    @staticmethod
    def _default_env_var(provider: str) -> str:
        return provider.upper().replace("-", "_").replace(".", "_") + "_API_KEY"

    def __init__(self, path: str | None = None, in_memory: bool = False) -> None:
        self._path = Path(path or get_auth_path()) if not in_memory else None
        self._runtime: dict[str, str] = {}
        self._data: dict[str, Any] = {}
        self._errors: list[dict[str, Any]] = []
        self._in_memory = in_memory
        # Guard: refuse to overwrite a known-malformed file.
        self._data_is_locked: bool = False  # True when file is malformed/non-dict.
        if self._path and self._path.exists():
            data, err = load_json_text_safe(self._path)
            if err is not None:
                self._errors.append({"scope": "auth", "error": err})
                self._data_is_locked = True
            elif isinstance(data, dict):
                self._data = data
            else:
                # Valid JSON but not a dict (e.g. list/str/number).
                self._errors.append(
                    {"scope": "auth", "error": ValueError("auth state is not a JSON object")}
                )
                self._data_is_locked = True
            # Tighten existing file/dir on POSIX.
            ensure_private_dir(self._path.parent)
            ensure_private_file(self._path, 0o600)

    # --- Writability guard ---

    def _require_writable(self) -> None:
        """Raise before mutation if the on-disk file is malformed."""
        if self._data_is_locked:
            raise RuntimeError(
                "auth file is malformed or not a JSON object; "
                "repair it before the agent can persist state"
            )

    def set_runtime_api_key(self, provider: str, api_key: str) -> None:
        self._runtime[provider] = api_key

    def set_stored_api_key(self, provider: str, api_key: str) -> None:
        self._require_writable()
        self._data.setdefault("apiKeys", {})[provider] = api_key
        self._save()

    def remove_stored_api_key(self, provider: str) -> None:
        self._require_writable()
        keys = self._data.setdefault("apiKeys", {})
        if provider in keys:
            del keys[provider]
        self._save()

    # --- OAuth token records (Phase 18: subscription login) ---

    def get_oauth_record(self, provider: str) -> dict[str, Any] | None:
        """Stored subscription-OAuth record for the provider, when present."""
        record = self._data.get("oauth", {}).get(provider)
        return record if isinstance(record, dict) else None

    def set_oauth_record(self, provider: str, record: dict[str, Any]) -> None:
        self._require_writable()
        self._data.setdefault("oauth", {})[provider] = record
        self._save()

    def remove_oauth_record(self, provider: str) -> None:
        self._require_writable()
        records = self._data.setdefault("oauth", {})
        if provider in records:
            del records[provider]
            self._save()

    # --- Complete credential removal (Phase 30.4) ---

    def remove_provider_credentials(self, provider: str) -> None:
        """Remove ALL one-managed credentials for *provider* (runtime, stored, OAuth).

        Calls ``_require_writable()`` first so a malformed/locked auth file raises
        ``RuntimeError`` instead of silently clearing in-memory state and reporting
        a misleading "successful" partial logout.

        Validates BOTH ``apiKeys`` and ``oauth`` containers are dicts (or absent)
        BEFORE any mutation — this prevents partial logout when either container is
        malformed (e.g. valid ``apiKeys`` but invalid ``oauth``).  Only after all
        preflight validation succeeds does the method remove the runtime key,
        persisted records, and save at most once when disk data actually changed.

        Unrelated providers are preserved; empty valid maps are pruned.
        """
        # Enforce writability before touching anything.
        self._require_writable()

        # ── Preflight: validate BOTH containers BEFORE any mutation ─────────
        api_keys = self._data.get("apiKeys")
        if api_keys is not None and not isinstance(api_keys, dict):
            raise RuntimeError(
                f"auth.json apiKeys is not a dict (got {type(api_keys).__name__}); "
                "repair the file before removing credentials"
            )
        oauth = self._data.get("oauth")
        if oauth is not None and not isinstance(oauth, dict):
            raise RuntimeError(
                f"auth.json oauth is not a dict (got {type(oauth).__name__}); "
                "repair the file before removing credentials"
            )

        # ── Mutation: runtime key ─────────
        self._runtime.pop(provider, None)

        # ── Mutation: persisted containers ─────────
        disk_changed = False

        if api_keys is not None and provider in api_keys:
            del api_keys[provider]
            if not api_keys:
                del self._data["apiKeys"]
            disk_changed = True

        if oauth is not None and provider in oauth:
            del oauth[provider]
            if not oauth:
                del self._data["oauth"]
            disk_changed = True

        if disk_changed:
            self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        # Refuse to overwrite a known-malformed file: report the error,
        # do NOT silently replace it.
        if self._data_is_locked:
            raise RuntimeError(
                "auth file is malformed or not a JSON object; "
                "repair it before the agent can persist state"
            )
        atomic_write_text(self._path, json.dumps(self._data, indent=2) + "\n")

    def drain_errors(self) -> list[dict[str, Any]]:
        out = self._errors[:]
        self._errors = []
        return out

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
