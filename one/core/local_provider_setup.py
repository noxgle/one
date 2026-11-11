# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from one.config import get_agent_dir
from one.core.model_registry import ModelRegistry
from one.core.persistence import atomic_write_text
from one.core.types import ModelInfo

_MAX_BODY = 1_000_000
_MAX_MODELS = 200
_MAX_TEXT = 1024
_MAX_IDS_JSON_BYTES = 24 * 1024


def _url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or value != value.strip():
        raise ValueError("Enter a valid HTTP(S) URL without credentials, query, or fragment.")
    try:
        parts = urlsplit(value)
        if (parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment or "?" in value or "#" in value
                or any(ord(c) < 33 or ord(c) == 127 for c in value)):
            raise ValueError
        _ = parts.port
    except ValueError:
        raise ValueError("Enter a valid HTTP(S) URL without credentials, query, or fragment.") from None
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path.rstrip("/"), "", ""))


def _read_object(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> None:
        raise ValueError("Invalid JSON constant")

    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Cannot read valid JSON configuration; repair the file before saving.") from None
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object; repair the file before saving.")
    return data


def _paths(session: Any) -> tuple[Path, Path]:
    settings = session.settings_manager
    return (Path(settings._global_path or Path(getattr(settings, "_agent_dir", get_agent_dir())) / "settings.json"),
            Path(session.model_registry._models_path))


async def _discover(provider: str, url: str) -> list[str]:
    base = url.rstrip("/")
    if provider == "ollama":
        if base.endswith("/v1"):
            base = base[:-3]
        endpoint = base + "/api/tags"
    else:
        endpoint = base + ("/models" if base.endswith("/v1") else "/v1/models")
    try:
        async with asyncio.timeout(6), httpx.AsyncClient(timeout=5.0, trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", endpoint) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_BODY:
                        raise ValueError("Model response is too large; check the server.")
                    body.extend(chunk)
        data = json.loads(body)
    except (httpx.HTTPError, httpx.InvalidURL, OSError, TimeoutError):
        raise ValueError(
            "Could not discover models; ensure the server is running: start Ollama or llama-server "
            "with a loaded model. Check the URL; in a container use the host URL "
            "(e.g. http://host.docker.internal:11434 for Ollama or :8080 for llama-server), not localhost."
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Server returned invalid model data; check the server.") from None
    entries = data.get("models" if provider == "ollama" else "data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("Server returned invalid model data; check the server.")
    result: list[str] = []
    serialized_bytes = 2  # JSON array brackets; budget the larger ensure_ascii representation.
    for entry in entries:
        name = entry.get("name" if provider == "ollama" else "id") if isinstance(entry, dict) else None
        if (isinstance(name, str) and name.strip() and len(name) <= _MAX_TEXT
                and not any(ord(c) < 32 or ord(c) == 127 for c in name) and name not in result):
            size = len(json.dumps(name, ensure_ascii=True)) + (2 if result else 0)
            if serialized_bytes + size > _MAX_IDS_JSON_BYTES:
                break
            serialized_bytes += size
            result.append(name)
            if len(result) == _MAX_MODELS:
                break
    return result


async def local_provider_operation(
    session: Any, operation: str, payload: dict[str, Any], *, is_current: Callable[[], bool] = lambda: True,
) -> dict[str, Any]:
    """Configure local providers. Returns {'ok': False, 'error': ...} on failure."""
    try:
        if not isinstance(payload, dict) or payload.get("provider") not in ("ollama", "llama.cpp"):
            raise ValueError("Choose ollama or llama.cpp as the provider.")
        provider = payload["provider"]
        settings_path, models_path = _paths(session)
        if operation == "local_provider_config":
            current = session.model
            model_id = current.id if current and current.provider == provider else None
            if not model_id and session.settings_manager.get_default_provider() == provider:
                model_id = session.settings_manager.get_default_model()
            registered = session.model_registry.find(provider, model_id) if model_id else None
            base = ((registered.base_url if registered else None)
                    or (current.base_url if current and current.provider == provider else None)
                    or session.providers[provider].base_url)
            return {"ok": True, "provider": provider, "url": base, "model": model_id or "",
                    "settingsPath": str(settings_path), "modelsPath": str(models_path)}
        if operation not in {"local_provider_models", "local_provider_save"}:
            raise ValueError("Unknown local provider operation.")
        url = _url(payload.get("url"))
        if operation == "local_provider_models":
            return {"ok": True, "provider": provider, "url": url, "models": await _discover(provider, url)}
        # Match the adapters' chat paths: Ollama appends /chat/completions,
        # llama.cpp appends /v1/chat/completions.
        if provider == "ollama" and not url.endswith("/v1"):
            url += "/v1"
        elif provider == "llama.cpp" and url.endswith("/v1"):
            url = url[:-3]
        model_id = payload.get("model")
        if (not isinstance(model_id, str) or not model_id.strip() or len(model_id) > _MAX_TEXT
                or model_id != model_id.strip() or any(ord(c) < 32 or ord(c) == 127 for c in model_id)):
            raise ValueError("Enter a valid model ID.")
        if session.is_streaming or getattr(session, "_is_compacting", False):
            raise ValueError("Wait for the current response or compaction to finish before saving.")
        settings = _read_object(settings_path)
        if session.settings_manager._in_memory and not settings_path.exists():
            settings = dict(session.settings_manager._global)
        models = _read_object(models_path)
        providers = models.get("providers", {})
        if not isinstance(providers, dict) or any(
            not isinstance(key, str) or not isinstance(entries, list) or any(
                not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip()
                for entry in entries) for key, entries in providers.items()
        ):
            raise ValueError("Models configuration is malformed; repair it before saving.")
        for key in ("defaultProvider", "defaultModel"):
            if key in settings and settings[key] is not None and not isinstance(settings[key], str):
                raise ValueError("Settings defaults are malformed; repair them before saving.")
        manager = session.settings_manager
        project = _read_object(Path(manager._project_path)) if manager._project_path else manager._project
        if any(key in project and project[key] != value for key, value in
               (("defaultProvider", provider), ("defaultModel", model_id))):
            raise ValueError("Project defaults conflict; update project settings before saving.")
        entries = providers.setdefault(provider, [])
        matches = [entry for entry in entries if entry["id"] == model_id]
        if len(matches) > 1:
            raise ValueError("Duplicate model IDs in configuration; repair it before saving.")
        if matches:
            matches[0]["url"] = url
        else:
            entries.append({"id": model_id, "url": url})
        models["providers"] = providers
        settings.update(defaultProvider=provider, defaultModel=model_id)
        # No awaits from this guard through writes and offline activation. All
        # work runs on the session owner loop, so start/switch cannot interleave.
        if not is_current():
            raise ValueError("Session changed; reopen local provider setup. Nothing was saved.")
        if session.is_streaming or getattr(session, "_is_compacting", False):
            raise ValueError("Wait for the current response or compaction to finish before saving.")
        try:
            atomic_write_text(models_path, json.dumps(models, indent=2) + "\n")
        except OSError:
            raise ValueError("Could not save models configuration; check file permissions.") from None
        try:
            atomic_write_text(settings_path, json.dumps(settings, indent=2) + "\n")
        except OSError:
            raise ValueError("Models were saved, but settings could not be saved; check file permissions.") from None
        try:
            manager._global = settings
            manager._project = project
            if manager._initial is not None:
                manager._initial = dict(settings)
            registry = session.model_registry
            fresh_registry = ModelRegistry(registry._auth, str(models_path), read_only=True)
            old = fresh_registry.find(provider, model_id)
            selected = replace(old, base_url=url) if old else ModelInfo(provider=provider, id=model_id, base_url=url)
            registry._models = [m for m in registry._models if not (m.provider == provider and m.id == model_id)]
            registry._models.append(selected)
            await session.set_model(selected, enrich=False)
        except Exception:
            raise ValueError("Configuration was saved, but switching models failed; select the model again.") from None
        return {"ok": True, "provider": provider, "url": url, "model": model_id,
                "settingsPath": str(settings_path), "modelsPath": str(models_path)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
