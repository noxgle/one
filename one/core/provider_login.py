from __future__ import annotations

import re
import webbrowser
from collections.abc import Awaitable, Callable
from typing import Any

from one.core.oauth import (
    OAUTH_FLOWS,
    run_login,
)

# ---------------------------------------------------------------------------
# Credential sanitizer
# ---------------------------------------------------------------------------
# Sanitizer for exception-derived login messages.  Must be applied *before*
# any message is returned or raised so that no caller leaks secrets.
#
# Strategy (ordered — earlier patterns match first):
#   1. Remove the exact submitted secret (full substring).
#   2. Redact Bearer/Basic authorization header values.
#   3. Redact common credential query parameters (key, api_key, access,
#      access_token, refresh, refresh_token, id_token, apiKey, client_secret,
#      token).
#   4. Redact common credential tokens in JSON-ish / dict-ish fragments
#      (both double-quoted JSON and single-quoted Python-dict style).
# Preserves useful HTTP status codes and reason text.


def _redact_credentials(message: str | None, *secrets: str) -> str:
    """Return *message* with every secret value redacted from credential contexts.

    Steps (applied in order):
      1. Exact substring replacement of each secret (full redaction).
      2. Bearer / Basic authorization header value redaction.
      3. Query-parameter value redaction (key=, api_key=, access=, access_token=,
         refresh=, refresh_token=, id_token=, apiKey=, client_secret=, token=, …).
      4. JSON-ish / dict-ish token value redaction in common credential fields
         (double-quoted JSON + single-quoted Python-dict style).

    If no secrets are provided only the structured redaction rules (2-4)
    are applied — useful for general sanitization of adapter errors.
    """
    if not message:
        return ""

    text = message

    # 1. Exact secret removal — always redacts the raw secret anywhere.
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "<REDACTED>")

    # 2. Bearer / Basic header value redaction.
    text = re.sub(
        r'(Bearer\s+)[^\s,"\']+',
        r'\1<REDACTED>',
        text,
    )
    text = re.sub(
        r'(Basic\s+)[^\s,"\']+',
        r'\1<REDACTED>',
        text,
    )

    # 3. Query-param credential redaction — comprehensive key names.
    text = re.sub(
        r'((?:key|api_key|access|access_token|refresh|refresh_token|'
        r'id_token|apiKey|client_secret|token)\s*=\s*)[^\s"&]+',
        r'\1<REDACTED>',
        text,
        flags=re.IGNORECASE,
    )

    # 4a. JSON-ish token value redaction (double-quoted keys/values).
    text = re.sub(
        r'("(?:access|access_token|refresh|refresh_token|id_token|'
        r'apiKey|api_key|client_secret|token)"\s*:\s*")([^\"]+)(")',
        r'\g<1><REDACTED>\3',
        text,
        flags=re.IGNORECASE,
    )

    # 4b. Single-quoted Python-dict-like redaction (same fields).
    text = re.sub(
        r"('(?:access|access_token|refresh|refresh_token|id_token|"
        r"apiKey|api_key|client_secret|token)'\s*:\s*')([^']+)'",
        r"\g<1><REDACTED>'",
        text,
        flags=re.IGNORECASE,
    )

    return text


def _entry_ids(entries: list[dict[str, Any]] | None) -> list[str] | None:
    """Extract plain ids from detailed model entries (None passes through)."""
    return [e["id"] for e in entries] if entries is not None else None


def entry_id(entry: str | dict[str, Any]) -> str:
    """Model id of a fetched entry (dict) or the id itself (plain string)."""
    return entry["id"] if isinstance(entry, dict) else entry


async def _fetch_detailed(
    adapter: Any, key: str, headers: dict[str, str] | None = None
) -> list[dict[str, Any]] | None:
    """Fetch model entries as ``{"id", "contextWindow"}`` dicts.

    Prefers ``list_models_detailed``; adapters/stubs exposing only
    ``list_models`` get unknown context windows. ``headers`` are extra
    auth headers (e.g. ``ChatGPT-Account-Id`` for OAuth providers).
    """
    detailed: Callable[..., Awaitable[list[dict[str, Any]] | None]] | None = getattr(
        adapter, "list_models_detailed", None
    )
    if detailed is not None:
        return await detailed(key, headers) if headers is not None else await detailed(key)
    models = await adapter.list_models(key, headers) if headers else await adapter.list_models(key)
    if models is None:
        return None
    return [{"id": m, "contextWindow": None} for m in models]


async def validate_and_fetch(
    adapter: Any,
    api_key: str,
    provider: str,
    model_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[bool, str | None, list[dict[str, Any]] | None]:
    """Validate an API key against a provider and fetch its model list.

    Returns ``(ok, error, models)`` where each model entry is
    ``{"id": str, "contextWindow": int | None}``:

    - ``ok=False`` — the key is invalid (401/403) or validation failed; the
      caller must NOT store the key.
    - ``ok=True, error=None, models=[...]`` — authorized; models fetched.
    - ``ok=True, error=None, models=None`` — authorized; no models endpoint.
    - ``ok=True, error=<note>, models=[...]`` — authorized (e.g. a 402 credit
      limit or a probe model rejecting the request); the note is informational.

    The provider's ``GET /models`` endpoint is NOT sufficient to validate a
    key: several providers expose a PUBLIC list (openrouter and ollama-cloud
    verified live — a bad key returns 200 with the full list). So whenever a
    key is given, the key is proven with a minimal chat call (``max_tokens=1``)
    against ``model_id`` or the first fetched model. 401/403 → hard failure;
    402 → the key is valid but the account has no credit (soft pass, note);
    other chat errors → hard failure.

    With an empty ``api_key`` (NO_AUTH providers like ``llama.cpp``/``ollama``)
    no validation happens; the model list is still fetched when possible.

    ``headers`` are extra auth headers (e.g. ``ChatGPT-Account-Id`` for OAuth
    providers) forwarded to both the models endpoint fetch and the validation
    chat probe.
    """
    if not api_key:
        try:
            models = await _fetch_detailed(adapter, "", headers)
            return True, None, models or []
        except NotImplementedError:
            return True, None, None
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            return True, _redact_credentials(f"could not fetch models: {e}"), None

    # Key given: fetch the model list first (may be public; does not prove auth).
    try:
        models = await _fetch_detailed(adapter, api_key, headers)
    except NotImplementedError:
        models = None  # no list endpoint (e.g. Anthropic); chat validation below
    except Exception as e:  # noqa: BLE001 - surfaced to the user
        msg = _redact_credentials(str(e), api_key)
        if "401" in msg or "403" in msg:
            return False, f"Authorization failed: {msg}", None
        return True, f"could not fetch models: {msg}", None

    # Prove the key with a minimal chat call when a model is available.
    ids = _entry_ids(models) or []
    chat_model = model_id or (ids[0] if ids else None)
    if chat_model:
        try:
            await adapter.chat(api_key, chat_model, [{"role": "user", "content": "ping"}], "off", max_tokens=1, headers=headers)
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            msg = _redact_credentials(str(e), api_key)
            if "401" in msg or "403" in msg:
                return False, f"Authorization failed: {msg}", None
            if "402" in msg:
                return True, "key is valid, but the provider account has insufficient credit", models or []
            if "400" in msg:
                # The probe model rejected the request (e.g. no reasoning
                # support on a free/public model) — the key itself is fine.
                return True, f"key is valid, but the probe model rejected the request: {msg}", models or []
            return False, f"Validation failed: {msg}", None
        return True, None, models

    return True, "no model available to validate against", models


async def run_oauth_login(
    provider: str,
    adapter: Any,
    model_registry: Any,
    open_url: Callable[[str], Any] | None = None,
    read_line: Callable[[], str] | None = None,
) -> tuple[bool, str | None, list[dict[str, Any]] | None]:
    """Run the provider's subscription-OAuth flow and store the record.

    Phase 18. Mirrors :func:`validate_and_fetch`'s return contract:
    ``(ok, error, models)`` — on ``ok`` the OAuth record is persisted in the
    auth storage and any fetched models are registered + persisted.

    ``open_url``/``read_line`` are injectable for tests.
    """
    spec = OAUTH_FLOWS.get(provider)
    if spec is None:
        return False, f"OAuth login not available for {provider}", None
    if open_url is None:
        open_url = webbrowser.open
    if read_line is None:
        read_line = input

    # run_login (paste/loopback flows) already returns a NORMALIZED record
    # via build_oauth_record — do NOT convert again (double conversion
    # raised "missing access_token" on the record-shaped dict).
    record = await run_login(spec, open_url=open_url, read_line=read_line)

    model_registry.set_oauth_record(provider, record)
    access = str(record.get("access", ""))

    # OAuth providers may need record-derived headers for their model
    # endpoint (HOTFIX-5: Codex requires ChatGPT-Account-Id).
    extra_headers: dict[str, str] = {}
    if record.get("accountId"):
        extra_headers["ChatGPT-Account-Id"] = str(record["accountId"])

    # Best-effort model fetch with the fresh access token.
    fetched: list[dict[str, Any]] | None = None
    try:
        fetched = await _fetch_detailed(adapter, access, extra_headers or None)
    except Exception:  # noqa: BLE001 - models are optional after login
        fetched = None
    if not fetched and hasattr(adapter, "list_models"):
        try:
            ids = await adapter.list_models(access, extra_headers or None)
            fetched = [{"id": i, "contextWindow": None} for i in (ids or [])]
        except Exception:  # noqa: BLE001
            fetched = None
    if fetched:
        model_registry.register_models(provider, fetched)
        model_registry.persist_models(provider, fetched)
    return True, None, fetched
