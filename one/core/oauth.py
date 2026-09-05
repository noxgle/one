"""Subscription OAuth login for providers.

Implements the flows used by Claude Code (Claude Pro/Max) and Codex CLI
(ChatGPT Plus/Pro).  These endpoints are not part of public provider APIs and
are externally controlled by the respective providers — they may change without
notice.  Third-party subscription use is at the user's own risk and may violate
the provider's Terms of Service.

Two flow styles:

- **paste** (Anthropic): the browser displays ``CODE#STATE`` and the user
  pastes it back — no local server needed.
- **loopback** (ChatGPT/Codex): a temporary HTTP server on ``127.0.0.1:1455``
  receives the browser redirect.

Token records are persisted per provider via :class:`~one.core.auth_storage.AuthStorage`
as ``{"type": "oauth", "access", "refresh", "expires" (ms epoch), "accountId"?}``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import queue
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from one.core.auth_storage import AuthStorage

# Refresh proactively when less than this much validity remains.
REFRESH_MARGIN_MS = 5 * 60 * 1000

# Default wait for the browser redirect in the loopback flow.
LOOPBACK_TIMEOUT_SEC = 300.0


class OAuthError(RuntimeError):
    """Raised when an OAuth login or refresh step fails."""


@dataclass(frozen=True)
class OAuthFlowSpec:
    """Provider-specific OAuth endpoints and conventions."""

    provider: str
    client_id: str
    authorize_url: str
    token_url: str
    redirect_uri: str
    scope: str
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    token_content_type: str = "form"  # "form" | "json"
    paste_flow: bool = False  # True → user pastes CODE[#STATE] back


ANTHROPIC_OAUTH = OAuthFlowSpec(
    provider="anthropic",
    client_id="9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    authorize_url="https://claude.ai/oauth/authorize",
    token_url="https://console.anthropic.com/v1/oauth/token",
    redirect_uri="https://console.anthropic.com/oauth/code/callback",
    scope="org:create_api_key user:profile user:inference",
    extra_authorize_params={"code": "true"},  # copy/paste-friendly mode
    token_content_type="json",
    paste_flow=True,
)

CHATGPT_OAUTH = OAuthFlowSpec(
    provider="chatgpt",
    client_id="app_EMoamEEZ73f0CkXaXp7hrann",
    authorize_url="https://auth.openai.com/oauth/authorize",
    token_url="https://auth.openai.com/oauth/token",
    redirect_uri="http://localhost:1455/auth/callback",
    scope="openid profile email offline_access",
    extra_authorize_params={
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "codex_cli_rs",
    },
    token_content_type="form",
    paste_flow=False,
)

OAUTH_FLOWS: dict[str, OAuthFlowSpec] = {
    "anthropic": ANTHROPIC_OAUTH,
    "chatgpt": CHATGPT_OAUTH,
}


def is_oauth_provider(provider: str) -> bool:
    return provider in OAUTH_FLOWS


def is_oauth_access_token(api_key: str | None) -> bool:
    """True when the credential looks like a subscription OAuth access token."""
    return bool(api_key) and any(api_key.startswith(prefix) for prefix in ("sk-ant-oat01", "sk-ant-oat"))


def oauth_login_hint(provider: str) -> str | None:
    """Human-readable description of the subscription login, when supported."""
    if provider == "anthropic":
        return "log in with your Claude Pro/Max account"
    if provider == "chatgpt":
        return "log in with your ChatGPT Plus/Pro account"
    return None


# --- PKCE ---------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_pkce() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` using S256."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def build_authorize_url(spec: OAuthFlowSpec, code_challenge: str, state: str) -> str:
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": spec.client_id,
        "redirect_uri": spec.redirect_uri,
        "scope": spec.scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        **spec.extra_authorize_params,
    }
    return f"{spec.authorize_url}?{urlencode(params)}"


# --- token endpoint -----------------------------------------------------------


def _user_agent() -> str:
    try:
        from importlib.metadata import version

        return f"one/{version('one')}"
    except Exception:
        return "one/0.0.0"


def _describe_response(resp: Any) -> str:
    """Compact status+body description for OAuth error messages."""
    snippet = ""
    try:
        snippet = (resp.text or "").strip()[:400]
    except Exception:
        pass
    # Redact secret-looking values so error messages don't leak tokens.
    for _key in ("code", "access_token", "refresh_token", "id_token", "code_verifier"):
        snippet = re.sub(
            r'("(?:' + _key + r')"\s*:\s*")[^"]*(")',
            r"\1[REDACTED]\2",
            snippet,
            flags=re.IGNORECASE,
        )
    return f"HTTP {resp.status_code}: {snippet or '<empty body>'}"


async def _token_request(spec: OAuthFlowSpec, body: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": _user_agent()}) as client:
        if spec.token_content_type == "json":
            resp = await client.post(spec.token_url, json=body)
        else:
            resp = await client.post(spec.token_url, data=body)
    if resp.is_error:
        raise OAuthError(f"{spec.provider} token endpoint error {_describe_response(resp)}")
    try:
        data = resp.json()
    except Exception:
        data = None
    if not isinstance(data, dict) or not data.get("access_token"):
        # Surface the server's error payload and raw body so failures are
        # diagnosable (mirrors how opencode/codex report exchange errors).
        err = ""
        if isinstance(data, dict):
            err = str(data.get("error_description") or data.get("error") or "")
        suffix = f": {err}" if err else ""
        raise OAuthError(f"{spec.provider} token response missing access_token{suffix} ({_describe_response(resp)})")
    return data


async def exchange_authorization_code(spec: OAuthFlowSpec, *, code: str, verifier: str) -> dict[str, Any]:
    # NOTE: no `state` here — matches opencode/codex-rs exactly; extra params
    # in the token body made auth.openai.com answer without access_token.
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": spec.client_id,
        "redirect_uri": spec.redirect_uri,
        "code_verifier": verifier,
    }
    return await _token_request(spec, body)


async def refresh_access_token(spec: OAuthFlowSpec, refresh_token: str) -> dict[str, Any]:
    return await _token_request(
        spec,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": spec.client_id},
    )


# --- token records ------------------------------------------------------------


def jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload WITHOUT verifying the signature.

    This is intentionally unverified: the tokens we decode are **issued by the
    OAuth provider** during our own flow, and the token itself (present in the
    stored record) is the proof of identity.  Signature verification would
    require fetching and caching the provider's JWKS keys and adds nothing
    here.

    **Warning:** do not reuse this helper for untrusted tokens.
    """
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode())
    except Exception:
        return {}


def extract_chatgpt_account_id(id_token: str | None, access_token: str) -> str | None:
    """Codex accountId from JWT claims (id_token first, then access_token)."""
    for tok in (id_token, access_token):
        if not tok:
            continue
        payload = jwt_payload(tok)
        acc = payload.get("chatgpt_account_id")
        if isinstance(acc, str) and acc:
            return acc
        claim = payload.get("https://api.openai.com/auth")
        if isinstance(claim, dict):
            acc = claim.get("chatgpt_account_id")
            if isinstance(acc, str) and acc:
                return acc
        orgs = payload.get("organizations")
        if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict):
            oid = orgs[0].get("id")
            if isinstance(oid, str) and oid:
                return oid
    return None


def build_oauth_record(spec: OAuthFlowSpec, token_resp: dict[str, Any]) -> dict[str, Any]:
    """Normalize a token response into the stored record shape."""
    access = token_resp.get("access_token")
    if not access:
        raise OAuthError(
            f"{spec.provider} token response missing access_token (received keys: {sorted(token_resp) or '[]'})"
        )
    now_ms = int(time.time() * 1000)
    expires_in = token_resp.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires = now_ms + int(expires_in * 1000)
    else:
        exp = jwt_payload(str(access)).get("exp")
        expires = int(exp * 1000) if isinstance(exp, (int, float)) else now_ms + 3600_000
    record: dict[str, Any] = {
        "type": "oauth",
        "access": str(access),
        "refresh": str(token_resp.get("refresh_token") or ""),
        "expires": expires,
    }
    if spec.provider == "chatgpt":
        acc = extract_chatgpt_account_id(token_resp.get("id_token"), record["access"])
        if acc:
            record["accountId"] = acc
    return record


async def ensure_fresh_token(auth: AuthStorage, provider: str, *, margin_ms: int = REFRESH_MARGIN_MS) -> str | None:
    """Return a live OAuth access token for the provider.

    Returns ``None`` when the provider has no OAuth record (API-key auth is
    being used instead). Refreshes the token proactively when it expires
    within ``margin_ms``; raises :class:`OAuthError` when refreshing fails.
    """
    spec = OAUTH_FLOWS.get(provider)
    if spec is None:
        return None
    record = auth.get_oauth_record(provider)
    if not record:
        return None
    now_ms = int(time.time() * 1000)
    expires = int(record.get("expires") or 0)
    access = str(record.get("access") or "")
    if access and expires - now_ms > margin_ms:
        return access
    refresh_token = str(record.get("refresh") or "")
    if not refresh_token:
        raise OAuthError(
            f"{provider}: OAuth token expired and no refresh token stored; log in again (/login {provider})."
        )
    resp = await refresh_access_token(spec, refresh_token)
    new_record = build_oauth_record(spec, resp)
    if not new_record.get("refresh"):
        new_record["refresh"] = refresh_token
    auth.set_oauth_record(provider, new_record)
    return str(new_record["access"])


# --- login flows --------------------------------------------------------------


async def run_paste_flow(
    spec: OAuthFlowSpec,
    *,
    open_url: Callable[[str], None],
    read_line: Callable[[], str],
) -> dict[str, Any]:
    """Copy/paste login: print the URL, read back ``CODE`` or ``CODE#STATE``."""
    verifier, challenge = generate_pkce()
    # Anthropic's paste mode echoes CODE#STATE; the verifier doubles as the
    # state so a bare CODE still completes the exchange.
    url = build_authorize_url(spec, challenge, state=verifier)
    open_url(url)
    raw = (await asyncio.to_thread(read_line)).strip()
    if "#" in raw:
        code, state = raw.split("#", 1)
    else:
        code = raw
    if not code:
        raise OAuthError("No authorization code entered.")
    token_resp = await exchange_authorization_code(spec, code=code, verifier=verifier)
    return build_oauth_record(spec, token_resp)


async def run_loopback_flow(
    spec: OAuthFlowSpec,
    *,
    open_url: Callable[[str], None],
    port: int = 1455,
    timeout_sec: float = LOOPBACK_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Browser login via a temporary loopback HTTP server receiving the redirect."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h3>Login complete.</h3>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )
            results.put(params)

        def log_message(self, format: str, *args: Any) -> None:  # silence stderr noise
            pass

    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(spec, challenge, state=state)

    results: queue.Queue[dict[str, str]] = queue.Queue()
    server = HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        open_url(url)
        try:
            callback = await asyncio.to_thread(results.get, True, timeout_sec)
        except queue.Empty:
            raise OAuthError(f"No OAuth callback received within {int(timeout_sec)} seconds.") from None
    finally:
        server.shutdown()
        server.server_close()

    if callback.get("error"):
        raise OAuthError(f"Authorization failed: {callback.get('error_description') or callback['error']}")
    code = callback.get("code")
    if not code:
        raise OAuthError("OAuth callback did not contain an authorization code.")
    if callback.get("state") != state:
        raise OAuthError("OAuth state mismatch — restart the login.")
    token_resp = await exchange_authorization_code(spec, code=code, verifier=verifier)
    return build_oauth_record(spec, token_resp)


async def run_login(spec: OAuthFlowSpec, **kwargs: Any) -> dict[str, Any]:
    """Dispatch to the flow style configured for this provider."""
    if spec.paste_flow:
        return await run_paste_flow(
            spec,
            open_url=kwargs["open_url"],
            read_line=kwargs["read_line"],
        )
    return await run_loopback_flow(
        spec,
        open_url=kwargs["open_url"],
        port=int(kwargs.get("port", 1455)),
        timeout_sec=float(kwargs.get("timeout_sec", LOOPBACK_TIMEOUT_SEC)),
    )
