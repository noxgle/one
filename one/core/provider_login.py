from __future__ import annotations

from typing import Any


async def validate_and_fetch(
    adapter: Any,
    api_key: str,
    provider: str,
    model_id: str | None = None,
) -> tuple[bool, str | None, list[str] | None]:
    """Validate an API key against a provider and fetch its model list.

    Returns ``(ok, error, models)``:

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
    """
    if not api_key:
        try:
            models = await adapter.list_models("")
            return True, None, models or []
        except NotImplementedError:
            return True, None, None
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            return True, f"could not fetch models: {e}", None

    # Key given: fetch the model list first (may be public; does not prove auth).
    try:
        models = await adapter.list_models(api_key)
    except NotImplementedError:
        models = None  # no list endpoint (e.g. Anthropic); chat validation below
    except Exception as e:  # noqa: BLE001 - surfaced to the user
        msg = str(e)
        if "401" in msg or "403" in msg:
            return False, f"Authorization failed: {msg}", None
        return True, f"could not fetch models: {msg}", None

    # Prove the key with a minimal chat call when a model is available.
    chat_model = model_id or (models[0] if models else None)
    if chat_model:
        try:
            await adapter.chat(api_key, chat_model, [{"role": "user", "content": "ping"}], "off", max_tokens=1)
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            msg = str(e)
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