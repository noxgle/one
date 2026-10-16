"""Validated, privacy-safe model-capacity catalogue support.

The bundled snapshot is useful offline.  Network refresh is explicitly opted
into by runtime bootstrap, so importing/constructing a registry never calls a
remote service (including in tests and untrusted repositories).
"""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx

from one.core.persistence import atomic_write_text, ensure_private_dir, ensure_private_file, load_json_text_safe
from one.core.types import ModelInfo

CATALOG_URL = "https://models.dev/api.json"
CACHE_NAME = "model-catalog.json"
MAX_CACHE_BYTES = 4_000_000
MAX_ENTRIES = 10_000
MAX_DOWNLOAD_BYTES = 20_000_000
REFRESH_TTL_SECONDS = 300

# models.dev uses provider names that do not always match our adapters.  This
# table is intentionally one-way and exact; there is no model-name matching.
PROVIDER_ALIASES = {"gemini": "google", "azure-openai": "azure"}
SUPPORTED_PROVIDERS = frozenset({"openai", "azure-openai", "anthropic", "gemini", "chatgpt", "openrouter", "ollama-cloud", "llama.cpp", "ollama", "xai", "deepseek", "mistral", "groq"})


def _limit(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 10_000_000 else None


def _normalise(payload: Any) -> dict[tuple[str, str], dict[str, int]]:
    """Accept only the compact snapshot/cache shape and bounded integer facts."""
    if not isinstance(payload, dict):
        return {}
    providers = payload.get("providers", payload)
    if not isinstance(providers, dict):
        return {}
    out: dict[tuple[str, str], dict[str, int]] = {}
    for source_provider, raw_provider in providers.items():
        # models.dev source names are explicitly mapped to adapter names.  A
        # source provider not in this table is ignored rather than guessed.
        provider = next((ours for ours, source in PROVIDER_ALIASES.items() if source == source_provider), source_provider)
        models = raw_provider.get("models") if isinstance(raw_provider, dict) and isinstance(raw_provider.get("models"), dict) else raw_provider
        if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS or not isinstance(models, dict):
            continue
        for model_id, raw in models.items():
            if len(out) >= MAX_ENTRIES:
                return {}
            if not isinstance(model_id, str) or not model_id or len(model_id) > 300 or not isinstance(raw, dict):
                continue
            limit_value = raw.get("limit")
            limit: dict[str, Any] = limit_value if isinstance(limit_value, dict) else {}
            facts = {
                key: value for key, value in (
                    ("contextWindow", _limit(raw.get("contextWindow") or limit.get("context"))),
                    ("maxContextWindow", _limit(raw.get("maxContextWindow"))),
                    ("inputLimit", _limit(raw.get("inputLimit") or limit.get("input"))),
                    ("outputLimit", _limit(raw.get("outputLimit") or limit.get("output"))),
                ) if value is not None
            }
            if facts:
                out[(provider, model_id)] = facts
    return out


class ModelCatalog:
    def __init__(self, state_dir: str | Path, *, fetcher: Callable[[str], Awaitable[Any]] | None = None, enabled: bool = False) -> None:
        self.state_dir = Path(state_dir)
        self.cache_path = self.state_dir / CACHE_NAME
        self.fetcher = fetcher
        self.enabled = enabled
        self.updated_at: int | None = None
        self.snapshot_updated_at: int | None = None
        self._snapshot_facts = self._load_snapshot()
        self._cache_facts = self._load_cache()
        # A cache is the last successful authoritative response, not a delta
        # over the package snapshot.  Keeping the snapshot beneath it would
        # resurrect entries the authority explicitly removed.
        self.facts = self._cache_facts or self._snapshot_facts

    def _load_snapshot(self) -> dict[tuple[str, str], dict[str, int]]:
        try:
            payload = json.loads(files("one.assets").joinpath("model_catalog.json").read_text("utf-8"))
            retrieved = payload.get("retrievedAt") if isinstance(payload, dict) else None
            if isinstance(retrieved, str):
                try:
                    self.snapshot_updated_at = int(datetime.fromisoformat(retrieved.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    self.snapshot_updated_at = None
            return _normalise(payload)
        except (OSError, ValueError, TypeError):
            return {}

    def _load_cache(self) -> dict[tuple[str, str], dict[str, int]]:
        try:
            if not self.cache_path.is_file() or self.cache_path.stat().st_size > MAX_CACHE_BYTES:
                return {}
            data, error = load_json_text_safe(self.cache_path)
            if error is not None:
                return {}
            updated = data.get("updatedAt") if isinstance(data, dict) else None
            if not isinstance(updated, int) or isinstance(updated, bool) or updated <= 0:
                return {}
            self.updated_at = updated
            return _normalise(data)
        except OSError:
            return {}

    def facts_for(self, provider: str, model_id: str) -> dict[str, int] | None:
        return self.facts.get((provider, model_id))

    def apply(self, model: ModelInfo) -> ModelInfo:
        """Apply authoritative facts while reversibly tracking catalogue fields."""
        facts = self.facts_for(model.provider, model.id)
        names = {"contextWindow": "context_window", "maxContextWindow": "max_context_window", "inputLimit": "input_limit", "outputLimit": "output_limit"}
        manual = set(model.manual_fields)
        # Older direct callers used metadata_source alone; retain that contract.
        if model.metadata_source == "models.json" and not manual:
            manual = {attr for attr in names.values() if _limit(getattr(model, attr))}
        catalog_fields = set(model.catalog_fields)
        baseline = dict(model.catalog_baseline)
        catalog_values = dict(model.catalog_values)
        changes: dict[str, Any] = {}
        for fact_name, attr in names.items():
            current = getattr(model, attr)
            previous_catalog = catalog_values.get(attr)
            if attr in catalog_fields and (not facts or fact_name not in facts):
                # Remove only an unchanged catalogue overlay. An endpoint may
                # have legitimately superseded it since the prior application.
                if attr not in manual and attr not in model.endpoint_fields and current == previous_catalog:
                    changes[attr] = baseline.get(attr)
                catalog_fields.discard(attr)
                baseline.pop(attr, None)
                catalog_values.pop(attr, None)
                continue
            if not facts or fact_name not in facts or attr in manual or attr in model.endpoint_fields:
                continue
            if attr in catalog_fields and current != previous_catalog:
                catalog_fields.discard(attr)
                baseline.pop(attr, None)
                catalog_values.pop(attr, None)
            if attr not in catalog_fields:
                baseline[attr] = current
            value = facts[fact_name]
            changes[attr] = value
            catalog_fields.add(attr)
            catalog_values[attr] = value
        if not changes and catalog_fields == set(model.catalog_fields):
            return model
        return replace(
            model,
            **changes,
            manual_fields=frozenset(manual),
            catalog_fields=frozenset(catalog_fields),
            catalog_baseline=baseline,
            catalog_values=catalog_values,
            metadata_source=(
                model.metadata_source if model.metadata_source == "models.json"
                else ("catalog cache" if self.updated_at else "catalog snapshot")
            ) if catalog_fields else (
                None if model.metadata_source in {"catalog", "catalog snapshot", "catalog cache"} else model.metadata_source
            ),
            metadata_updated_at=(self.updated_at or self.snapshot_updated_at) if catalog_fields else model.metadata_updated_at,
        )

    async def refresh(self) -> bool:
        if not self.enabled:
            return False
        try:
            payload = await asyncio.wait_for(self._fetch(), timeout=10)
            facts = _normalise(payload)
            if not facts:
                return False
            self.updated_at = int(time.time())
            compact = {"updatedAt": self.updated_at, "providers": {}}
            for (provider, model), values in facts.items():
                compact["providers"].setdefault(provider, {})[model] = values
            encoded = json.dumps(compact, separators=(",", ":")) + "\n"
            if len(encoded.encode("utf-8")) > MAX_CACHE_BYTES:
                return False
            ensure_private_dir(self.state_dir)
            atomic_write_text(self.cache_path, encoded)
            ensure_private_file(self.cache_path, 0o600)
            self._cache_facts = facts
            # A successful response is authoritative, including deletions. The
            # bundled snapshot remains a cold-start/outage fallback only.
            self.facts = facts
            return True
        except (TimeoutError, OSError, ValueError, httpx.HTTPError):
            return False

    async def _fetch(self) -> Any:
        if self.fetcher is not None:
            return await self.fetcher(CATALOG_URL)
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            async with client.stream("GET", CATALOG_URL) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ValueError("model catalog download exceeds limit")
                    chunks.append(chunk)
            return json.loads(b"".join(chunks))
