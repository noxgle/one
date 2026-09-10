"""Phase 18: subscription OAuth login (Claude Pro/Max, ChatGPT/Codex).

All HTTP is faked (no network); the loopback test uses a real local server.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

import pytest

import one.core.oauth as oauth
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.oauth import (
    ANTHROPIC_OAUTH,
    CHATGPT_OAUTH,
    OAuthError,
    build_authorize_url,
    build_oauth_record,
    ensure_fresh_token,
    exchange_authorization_code,
    extract_chatgpt_account_id,
    generate_pkce,
    is_oauth_access_token,
    is_oauth_provider,
    jwt_payload,
    refresh_access_token,
    run_loopback_flow,
    run_paste_flow,
)
from one.core.provider_login import run_oauth_login
from one.core.types import ModelInfo
from one.providers.anthropic import AnthropicAdapter
from one.providers.codex_responses import CodexResponsesAdapter
from one.providers.registry import build_provider_registry


def _fake_client_factory(monkeypatch, module, handler):
    """Patch ``module.httpx.AsyncClient`` with a fake driven by ``handler``.

    ``handler(method, url, kwargs)`` returns a response-like object.
    """

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def post(self, url: str, **kwargs: Any):
            return handler("POST", url, kwargs)

        async def get(self, url: str, **kwargs: Any):
            return handler("GET", url, kwargs)

        def stream(self, method: str, url: str, **kwargs: Any):
            return handler(method, url, kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


# --- PKCE / authorize URL -------------------------------------------------------


def test_generate_pkce_s256_relation():
    verifier, challenge = generate_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_build_authorize_url_anthropic():
    url = build_authorize_url(ANTHROPIC_OAUTH, "challenge-x", state="state-x")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith("https://claude.ai/oauth/authorize?")
    assert q["client_id"] == ["9d1c250a-e61b-44d9-88ed-5944d1962f5e"]
    assert q["response_type"] == ["code"]
    assert q["code_challenge"] == ["challenge-x"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] == ["state-x"]
    assert q["code"] == ["true"]
    assert "console.anthropic.com" in q["redirect_uri"][0]


def test_build_authorize_url_chatgpt_extra_params():
    url = build_authorize_url(CHATGPT_OAUTH, "ch", state="st")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["originator"] == ["codex_cli_rs"]
    assert q["codex_cli_simplified_flow"] == ["true"]
    assert q["scope"] == ["openid profile email offline_access"]
    assert "localhost:1455" in q["redirect_uri"][0]


def test_is_oauth_provider_and_token_prefixes():
    assert is_oauth_provider("anthropic") and is_oauth_provider("chatgpt")
    assert not is_oauth_provider("openai")
    assert is_oauth_access_token("sk-ant-oat01-abc")
    assert is_oauth_access_token("sk-ant-oat-xyz")
    assert not is_oauth_access_token("sk-ant-api03-realkey")
    assert not is_oauth_access_token(None)


# --- token endpoint -------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_token_request_uses_json_body(monkeypatch):
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        text = ""

        @property
        def is_error(self) -> bool:
            return False

        def json(self) -> dict[str, Any]:
            return {"access_token": "sk-ant-oat01-t", "refresh_token": "r", "expires_in": 3600}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Resp()

    _fake_client_factory(monkeypatch, oauth, handler)
    resp = await exchange_authorization_code(ANTHROPIC_OAUTH, code="C", verifier="V")
    assert resp["access_token"] == "sk-ant-oat01-t"
    assert captured["url"] == "https://console.anthropic.com/v1/oauth/token"
    body = captured["kwargs"]["json"]
    assert captured["kwargs"].get("data") is None
    assert body["grant_type"] == "authorization_code"
    assert body["code_verifier"] == "V"
    assert set(body) == {"grant_type", "code", "client_id", "redirect_uri", "code_verifier"}


@pytest.mark.asyncio
async def test_chatgpt_token_request_uses_form_body(monkeypatch):
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        text = ""

        @property
        def is_error(self) -> bool:
            return False

        def json(self) -> dict[str, Any]:
            return {"access_token": "at", "refresh_token": "rt"}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Resp()

    _fake_client_factory(monkeypatch, oauth, handler)
    resp = await refresh_access_token(CHATGPT_OAUTH, "rt-old")
    assert resp["access_token"] == "at"
    assert captured["url"] == "https://auth.openai.com/oauth/token"
    body = captured["kwargs"]["data"]
    assert captured["kwargs"].get("json") is None
    assert body == {"grant_type": "refresh_token", "refresh_token": "rt-old", "client_id": CHATGPT_OAUTH.client_id}


@pytest.mark.asyncio
async def test_token_request_error_raises_oauth_error(monkeypatch):
    class _Resp:
        status_code = 400
        text = "bad"

        @property
        def is_error(self) -> bool:
            return True

        def json(self) -> dict[str, Any]:
            return {"error": "invalid_grant"}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        return _Resp()

    _fake_client_factory(monkeypatch, oauth, handler)
    with pytest.raises(OAuthError, match="token endpoint error HTTP 400"):
        await refresh_access_token(CHATGPT_OAUTH, "rt")


@pytest.mark.asyncio
async def test_token_request_missing_access_token_includes_diagnostics(monkeypatch):
    """200-without-access_token must surface status, body and error fields."""

    class _Resp:
        status_code = 200
        text = '{"error":"invalid_grant","error_description":"code expired"}'
        is_error = False

        def json(self) -> dict[str, Any]:
            return {"error": "invalid_grant", "error_description": "code expired"}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        return _Resp()

    _fake_client_factory(monkeypatch, oauth, handler)
    with pytest.raises(OAuthError) as exc_info:
        await exchange_authorization_code(CHATGPT_OAUTH, code="C", verifier="V")
    msg = str(exc_info.value)
    assert "missing access_token" in msg
    assert "invalid_grant" in msg
    assert "code expired" in msg
    assert "HTTP 200" in msg


@pytest.mark.asyncio
async def test_token_request_non_json_body_is_handled(monkeypatch):
    """Non-JSON 2xx body → clean OAuthError with the raw snippet, no crash."""

    class _Resp:
        status_code = 200
        text = "<html>blocked</html>"
        is_error = False

        def json(self) -> dict[str, Any]:
            raise ValueError("not json")

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        return _Resp()

    _fake_client_factory(monkeypatch, oauth, handler)
    with pytest.raises(OAuthError, match="blocked"):
        await refresh_access_token(CHATGPT_OAUTH, "rt")


@pytest.mark.asyncio
async def test_chatgpt_exchange_body_exact_shape(monkeypatch):
    """Token body matches opencode exactly — no extra params (e.g. state)."""
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        text = ""
        is_error = False

        def json(self) -> dict[str, Any]:
            return {"access_token": "at", "refresh_token": "rt"}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        captured["kwargs"] = kwargs
        return _Resp()

    _fake_client_factory(monkeypatch, oauth, handler)
    await exchange_authorization_code(CHATGPT_OAUTH, code="CB", verifier="VF")
    assert captured["kwargs"]["data"] == {
        "grant_type": "authorization_code",
        "code": "CB",
        "client_id": CHATGPT_OAUTH.client_id,
        "redirect_uri": "http://localhost:1455/auth/callback",
        "code_verifier": "VF",
    }


# --- records / JWT --------------------------------------------------------------


def _jwt(claims: dict[str, Any]) -> str:
    head = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{head}.{payload}.sig"


def test_jwt_payload_decodes_without_verification():
    tok = _jwt({"sub": "u1", "exp": 123})
    assert jwt_payload(tok) == {"sub": "u1", "exp": 123}
    assert jwt_payload("not-a-jwt") == {}


def test_extract_chatgpt_account_id_chain():
    direct = _jwt({"chatgpt_account_id": "acc-direct"})
    nested = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc-nested"}})
    orgs = _jwt({"organizations": [{"id": "org-1"}]})
    none_ = _jwt({"sub": "x"})
    # id_token wins over access_token regardless of claim level.
    assert extract_chatgpt_account_id(nested, direct) == "acc-nested"
    assert extract_chatgpt_account_id(direct, nested) == "acc-direct"
    assert extract_chatgpt_account_id(None, nested) == "acc-nested"
    assert extract_chatgpt_account_id(None, orgs) == "org-1"
    assert extract_chatgpt_account_id(None, none_) is None
    assert extract_chatgpt_account_id(None, "") is None


def test_build_oauth_record_shape_and_expiry_fallback():
    now_ms = int(time.time() * 1000)
    rec = build_oauth_record(
        CHATGPT_OAUTH,
        {"access_token": _jwt({"exp": now_ms // 1000 + 7200}), "refresh_token": "rt"},
    )
    assert rec["type"] == "oauth"
    assert rec["refresh"] == "rt"
    # No expires_in → falls back to the JWT exp claim (~2h ahead).
    assert abs(rec["expires"] - (now_ms + 7200_000)) < 10_000
    # chatgpt record extracts accountId when claims allow it.
    rec2 = build_oauth_record(
        CHATGPT_OAUTH,
        {"access_token": _jwt({"chatgpt_account_id": "acc-9"}), "expires_in": 60},
    )
    assert rec2["accountId"] == "acc-9"
    # anthropic never gets an accountId field.
    rec3 = build_oauth_record(
        ANTHROPIC_OAUTH,
        {"access_token": _jwt({"chatgpt_account_id": "acc-9"}), "expires_in": 60},
    )
    assert "accountId" not in rec3
    with pytest.raises(OAuthError, match="received keys"):
        build_oauth_record(CHATGPT_OAUTH, {"refresh_token": "rt"})


# --- ensure_fresh_token ---------------------------------------------------------


def _auth_with_record(expires_in_ms: int, refresh: str = "rt-1") -> AuthStorage:
    auth = AuthStorage.in_memory()
    auth.set_oauth_record(
        "chatgpt",
        {
            "type": "oauth",
            "access": "tok-old",
            "refresh": refresh,
            "expires": int(time.time() * 1000) + expires_in_ms,
        },
    )
    return auth


@pytest.mark.asyncio
async def test_ensure_fresh_token_noop_when_fresh_or_missing():
    auth = _auth_with_record(expires_in_ms=3600_000)

    async def _boom(spec, refresh):  # must not be called
        raise AssertionError("refresh should not run for a fresh token")

    monkey_refresh = _boom
    saved = oauth.refresh_access_token
    try:
        oauth.refresh_access_token = monkey_refresh  # type: ignore[assignment]
        token = await ensure_fresh_token(auth, "chatgpt")
        assert token == "tok-old"
    finally:
        oauth.refresh_access_token = saved  # type: ignore[assignment]
    # Unknown provider / missing record → None.
    assert await ensure_fresh_token(auth, "openai") is None
    empty = AuthStorage.in_memory()
    assert await ensure_fresh_token(empty, "chatgpt") is None


@pytest.mark.asyncio
async def test_ensure_fresh_token_refreshes_near_expiry(monkeypatch):
    auth = _auth_with_record(expires_in_ms=60_000)  # inside the 5 min margin
    calls: list[str] = []

    async def fake_refresh(spec, refresh_token):
        calls.append(refresh_token)
        return {"access_token": "tok-new", "refresh_token": "rt-2", "expires_in": 3600}

    monkeypatch.setattr(oauth, "refresh_access_token", fake_refresh)
    token = await ensure_fresh_token(auth, "chatgpt")
    assert token == "tok-new"
    assert calls == ["rt-1"]
    stored = auth.get_oauth_record("chatgpt")
    assert stored["access"] == "tok-new"
    assert stored["refresh"] == "rt-2"


@pytest.mark.asyncio
async def test_ensure_fresh_token_keeps_old_refresh_when_response_lacks_one(monkeypatch):
    auth = _auth_with_record(expires_in_ms=0)

    async def fake_refresh(spec, refresh_token):
        return {"access_token": "tok-new", "expires_in": 3600}

    monkeypatch.setattr(oauth, "refresh_access_token", fake_refresh)
    await ensure_fresh_token(auth, "chatgpt")
    assert auth.get_oauth_record("chatgpt")["refresh"] == "rt-1"


@pytest.mark.asyncio
async def test_ensure_fresh_token_raises_on_expired_without_refresh():
    auth = _auth_with_record(expires_in_ms=-1000, refresh="")
    with pytest.raises(OAuthError, match="log in again"):
        await ensure_fresh_token(auth, "chatgpt")


# --- paste flow (Anthropic) -----------------------------------------------------


@pytest.mark.asyncio
async def test_run_paste_flow_code_state(monkeypatch):
    opened: list[str] = []
    exchanged: dict[str, Any] = {}

    async def fake_exchange(spec, *, code, verifier):
        exchanged.update({"code": code, "verifier": verifier})
        return {"access_token": "sk-ant-oat01-a", "refresh_token": "r", "expires_in": 3600}

    monkeypatch.setattr(oauth, "exchange_authorization_code", fake_exchange)
    record = await run_paste_flow(
        ANTHROPIC_OAUTH,
        open_url=opened.append,
        read_line=lambda: "ABCD#STATE123",
    )
    assert len(opened) == 1
    q = urllib.parse.parse_qs(urllib.parse.urlparse(opened[0]).query)
    # Paste mode uses the verifier as the state fallback in the AUTHORIZE url.
    assert q["state"] == [exchanged["verifier"]]
    assert exchanged["code"] == "ABCD"
    # Token request carries no state (matches opencode/codex-rs).
    assert "state" not in exchanged
    assert record["access"] == "sk-ant-oat01-a"


@pytest.mark.asyncio
async def test_run_paste_flow_bare_code_falls_back_to_verifier(monkeypatch):
    opened: list[str] = []
    exchanged: dict[str, Any] = {}

    async def fake_exchange(spec, *, code, verifier):
        exchanged.update({"code": code, "verifier": verifier})
        return {"access_token": "t", "expires_in": 60}

    monkeypatch.setattr(oauth, "exchange_authorization_code", fake_exchange)
    await run_paste_flow(ANTHROPIC_OAUTH, open_url=opened.append, read_line=lambda: "JUSTCODE")
    assert exchanged["code"] == "JUSTCODE"
    # Authorize URL used the verifier as state so a bare CODE still validates.
    q = urllib.parse.parse_qs(urllib.parse.urlparse(opened[0]).query)
    assert q["state"] == [exchanged["verifier"]]


@pytest.mark.asyncio
async def test_run_paste_flow_empty_input_raises(monkeypatch):
    async def fail_exchange(spec, **kw):
        raise AssertionError("should not exchange")

    monkeypatch.setattr(oauth, "exchange_authorization_code", fail_exchange)
    with pytest.raises(OAuthError, match="No authorization code"):
        await run_paste_flow(ANTHROPIC_OAUTH, open_url=lambda u: None, read_line=lambda: "  ")


# --- loopback flow (ChatGPT/Codex) ----------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_run_loopback_flow_receives_callback(monkeypatch):
    port = _free_port()

    async def fake_exchange(spec, *, code, verifier, state=None):
        return {"access_token": _jwt({"chatgpt_account_id": "acc-lb"}), "refresh_token": "rt", "expires_in": 3600}

    monkeypatch.setattr(oauth, "exchange_authorization_code", fake_exchange)

    def open_url(url: str) -> None:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        state = q["state"][0]

        def hit() -> None:
            last_err: Exception | None = None
            for _ in range(50):
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/auth/callback?code=CB&state={state}", timeout=2
                    ).read()
                    return
                except Exception as e:  # server may not be listening yet
                    last_err = e
                    time.sleep(0.02)
            raise AssertionError(f"callback failed: {last_err}")

        threading.Thread(target=hit, daemon=True).start()

    record = await run_loopback_flow(CHATGPT_OAUTH, open_url=open_url, port=port, timeout_sec=15)
    assert record["access"]
    assert record["accountId"] == "acc-lb"


@pytest.mark.asyncio
async def test_run_loopback_flow_rejects_bad_state(monkeypatch):
    port = _free_port()

    async def fail_exchange(spec, **kw):
        raise AssertionError("should not exchange on state mismatch")

    monkeypatch.setattr(oauth, "exchange_authorization_code", fail_exchange)

    def open_url(url: str) -> None:
        def hit() -> None:
            for _ in range(50):
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/auth/callback?code=CB&state=WRONG", timeout=2
                    ).read()
                    return
                except Exception:
                    time.sleep(0.02)

        threading.Thread(target=hit, daemon=True).start()

    with pytest.raises(OAuthError, match="state mismatch"):
        await run_loopback_flow(CHATGPT_OAUTH, open_url=open_url, port=port, timeout_sec=15)


@pytest.mark.asyncio
async def test_run_loopback_flow_reports_provider_error(monkeypatch):
    port = _free_port()

    async def fail_exchange(spec, **kw):
        raise AssertionError("should not exchange on provider error")

    monkeypatch.setattr(oauth, "exchange_authorization_code", fail_exchange)

    def open_url(url: str) -> None:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        state = q["state"][0]

        def hit() -> None:
            for _ in range(50):
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/auth/callback?error=access_denied&state={state}",
                        timeout=2,
                    ).read()
                    return
                except Exception:
                    time.sleep(0.02)

        threading.Thread(target=hit, daemon=True).start()

    with pytest.raises(OAuthError, match="access_denied"):
        await run_loopback_flow(CHATGPT_OAUTH, open_url=open_url, port=port, timeout_sec=15)


@pytest.mark.asyncio
async def test_run_loopback_flow_timeout(monkeypatch):
    """No browser navigation → queue.get times out → OAuthError with timeout message."""

    async def fail_exchange(spec, **kw):
        raise AssertionError("should not exchange on timeout")

    monkeypatch.setattr(oauth, "exchange_authorization_code", fail_exchange)
    # open_url is a no-op — nothing calls the callback server.

    with pytest.raises(OAuthError, match="No OAuth callback received"):
        await run_loopback_flow(
            CHATGPT_OAUTH,
            open_url=lambda u: None,
            port=0,
            timeout_sec=1,
        )


@pytest.mark.asyncio
async def test_run_loopback_flow_missing_code(monkeypatch):
    """Callback fires but carries neither code nor error → OAuthError."""
    port = _free_port()

    async def fail_exchange(spec, **kw):
        raise AssertionError("should not exchange on missing code")

    monkeypatch.setattr(oauth, "exchange_authorization_code", fail_exchange)

    def open_url(url: str) -> None:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        state = q["state"][0]

        def hit() -> None:
            for _ in range(50):
                try:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/auth/callback?state={state}&foo=1",
                        timeout=2,
                    ).read()
                    return
                except Exception:
                    time.sleep(0.02)

        threading.Thread(target=hit, daemon=True).start()

    with pytest.raises(OAuthError, match="did not contain an authorization code"):
        await run_loopback_flow(CHATGPT_OAUTH, open_url=open_url, port=port, timeout_sec=15)


# --- storage roundtrip + registry integration -----------------------------------


def test_auth_storage_oauth_record_roundtrip(tmp_path):
    path = tmp_path / "auth.json"
    auth = AuthStorage(str(path))
    assert auth.get_oauth_record("chatgpt") is None
    rec = {"type": "oauth", "access": "a", "refresh": "r", "expires": 123}
    auth.set_oauth_record("chatgpt", rec)
    auth2 = AuthStorage(str(path))
    assert auth2.get_oauth_record("chatgpt") == rec
    auth2.remove_oauth_record("chatgpt")
    assert AuthStorage(str(path)).get_oauth_record("chatgpt") is None


def test_model_registry_oauth_counts_as_configured_auth():
    reg = ModelRegistry(AuthStorage.in_memory())
    model = ModelInfo(provider="chatgpt", id="gpt-5.3-codex")
    assert not reg.has_configured_auth(model)
    reg._auth.set_oauth_record("chatgpt", {"type": "oauth", "access": "at", "refresh": "r", "expires": 1, "accountId": "ACC"})
    assert reg.has_configured_auth(model)


def test_get_api_key_and_headers_oauth_fallback_and_precedence():
    reg = ModelRegistry(AuthStorage.in_memory())
    reg._auth.set_oauth_record(
        "chatgpt",
        {"type": "oauth", "access": "at-1", "refresh": "r", "expires": 1, "accountId": "ACC-7"},
    )
    info = ModelInfo(provider="chatgpt", id="m")
    res = reg.get_api_key_and_headers(info)
    assert res["ok"] and res["apiKey"] == "at-1"
    assert res["headers"]["ChatGPT-Account-Id"] == "ACC-7"
    # Explicit API key takes precedence over the OAuth record.
    reg._auth.set_stored_api_key("chatgpt", "sk-explicit")
    res2 = reg.get_api_key_and_headers(info)
    assert res2["apiKey"] == "sk-explicit"
    assert "ChatGPT-Account-Id" not in res2["headers"]


@pytest.mark.asyncio
async def test_model_registry_ensure_oauth_fresh(monkeypatch):
    reg = ModelRegistry(AuthStorage.in_memory())
    reg._auth.set_oauth_record(
        "chatgpt",
        {"type": "oauth", "access": "old", "refresh": "rt", "expires": int(time.time() * 1000)},
    )

    async def fake_refresh(spec, refresh_token):
        return {"access_token": "new", "refresh_token": "rt2", "expires_in": 3600}

    monkeypatch.setattr(oauth, "refresh_access_token", fake_refresh)
    await reg.ensure_oauth_fresh("chatgpt")
    assert reg._auth.get_oauth_record("chatgpt")["access"] == "new"
    # Non-oauth provider without a record is a no-op (even a bogus name).
    await reg.ensure_oauth_fresh("does-not-exist")


# --- Anthropic adapter OAuth mode -----------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_adapter_headers_oauth_vs_api_key(monkeypatch):
    adapter = AnthropicAdapter()
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        text = ""
        is_error = False

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"output": [{"content": [{"type": "text", "text": "ok"}]}], "usage": {}, "stop_reason": "end_turn"}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:

        captured["headers"] = kwargs["headers"]
        captured["url"] = url
        return _Resp()

    from one.providers import anthropic as anth_mod

    _fake_client_factory(monkeypatch, anth_mod, handler)
    await adapter.chat("sk-ant-oat01-t", "claude-x", [{"role": "user", "content": "hi"}], "off")
    h = captured["headers"]
    assert h["Authorization"] == "Bearer sk-ant-oat01-t"
    assert "oauth-2025-04-20" in h.get("anthropic-beta", "")
    assert "x-api-key" not in h
    await adapter.chat("sk-ant-api03-k", "claude-x", [{"role": "user", "content": "hi"}], "off")
    h2 = captured["headers"]
    assert h2["x-api-key"] == "sk-ant-api03-k"
    assert "Authorization" not in h2
    assert "oauth-2025-04-20" not in h2.get("anthropic-beta", "")


@pytest.mark.asyncio
async def test_anthropic_list_models_only_for_oauth_tokens(monkeypatch):
    adapter = AnthropicAdapter()
    # Plain API key: no endpoint → None, no HTTP call at all.
    assert await adapter.list_models_detailed("sk-ant-api03-k") is None
    assert await adapter.list_models("sk-ant-api03-k") is None

    class _Resp:
        status_code = 200
        text = ""
        is_error = False

        def json(self) -> dict[str, Any]:
            return {"data": [{"id": "claude-opus-4-6"}, {"id": "claude-sonnet-4-6"}, {"id": ""}]}

    called = {}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        called["url"] = url
        called["headers"] = kwargs["headers"]
        return _Resp()

    from one.providers import anthropic as anth_mod

    _fake_client_factory(monkeypatch, anth_mod, handler)
    detailed = await adapter.list_models_detailed("sk-ant-oat01-t")
    assert detailed == [
        {"id": "claude-opus-4-6", "contextWindow": None},
        {"id": "claude-sonnet-4-6", "contextWindow": None},
    ]
    assert called["url"] == "https://api.anthropic.com/v1/models"
    assert called["headers"]["Authorization"] == "Bearer sk-ant-oat01-t"
    ids = await adapter.list_models("sk-ant-oat01-t")
    assert ids == ["claude-opus-4-6", "claude-sonnet-4-6"]


# --- Codex Responses adapter ----------------------------------------------------


def test_codex_payload_building():
    ad = CodexResponsesAdapter()
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    p = ad._build_payload("gpt-5.3-codex", messages, "high", stream=False, max_tokens=77)
    assert p["model"] == "gpt-5.3-codex"
    assert p["instructions"] == "be terse"
    assert p["store"] is False and p["stream"] is False
    assert p["max_output_tokens"] == 77
    assert p["reasoning"] == {"effort": "high"}
    # HOTFIX-5: backend requires Responses message items with explicit type.
    assert all(i["type"] == "message" for i in p["input"])
    assert [i["content"][0]["type"] for i in p["input"]] == ["input_text", "output_text"]
    assert [i["role"] for i in p["input"]] == ["user", "assistant"]
    # xhigh clamps to high; off omits reasoning entirely.
    p2 = ad._build_payload("m", messages, "xhigh", stream=True)
    assert p2["reasoning"] == {"effort": "high"}
    assert p2["stream"] is True and "max_output_tokens" not in p2
    p3 = ad._build_payload("m", messages, "off", stream=False)
    assert "reasoning" not in p3


def test_codex_payload_instructions_fallback():
    """HOTFIX-5: empty instructions are rejected by the backend — the adapter
    must fall back to a non-empty base prompt when no system message exists."""
    ad = CodexResponsesAdapter()
    p = ad._build_payload("gpt-5.3-codex", [{"role": "user", "content": "hi"}], "medium", stream=False)
    assert p["instructions"].startswith("You are Codex")


@pytest.mark.asyncio
async def test_codex_nonstream_chat_parses_output(monkeypatch):
    ad = CodexResponsesAdapter()
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        is_error = False

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {
                "output": [
                    {"type": "reasoning", "summary": []},
                    {"type": "message", "content": [{"type": "output_text", "text": "hello "}, {"type": "output_text", "text": "world"}]},
                ],
                "usage": {"input_tokens": 5},
                "status": "completed",
            }

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["payload"] = kwargs["json"]
        return _Resp()

    from one.providers import codex_responses as cod_mod

    _fake_client_factory(monkeypatch, cod_mod, handler)
    result = await ad.chat("tok", "gpt-5.3-codex", [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}], "medium")
    assert result.text == "hello world"
    assert result.usage == {"input_tokens": 5}
    assert result.stop_reason == "completed"
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    h = captured["headers"]
    assert h["Authorization"] == "Bearer tok"
    assert h["originator"] == "codex_cli_rs"
    assert h.get("session_id")
    assert captured["payload"]["store"] is False


class _StreamResp:
    status_code = 200
    is_error = False

    def raise_for_status(self) -> None:
        pass

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


@pytest.mark.asyncio
async def test_codex_stream_chat_deltas_and_completed(monkeypatch):
    ad = CodexResponsesAdapter()
    sse = [
        "data: " + '{"type":"response.output_text.delta","delta":"Hel"}',
        "",
        "data: " + '{"type":"response.output_text.delta","delta":"lo"}',
        "data: " + '{"type":"response.completed","response":{"usage":{"input_tokens":9},"status":"completed"}}',
        "data: [DONE]",
    ]

    class _Ctx:
        def __init__(self, resp: _StreamResp) -> None:
            self.resp = resp

        async def __aenter__(self):
            return self.resp

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    from one.providers import codex_responses as cod_mod

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Ctx:
        assert kwargs["json"]["stream"] is True
        return _Ctx(_StreamResp(sse))

    _fake_client_factory(monkeypatch, cod_mod, handler)
    deltas: list[str] = []
    result = await ad.chat(
        "tok",
        "m",
        [{"role": "user", "content": "q"}],
        "low",
        on_delta=deltas.append,
    )
    assert deltas == ["Hel", "lo"]
    assert result.text == "Hello"
    assert result.usage == {"input_tokens": 9}


@pytest.mark.asyncio
async def test_codex_list_models_slugs(monkeypatch):
    ad = CodexResponsesAdapter()
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200
        text = ""
        is_error = False

        def json(self) -> dict[str, Any]:
            return {"models": [{"slug": "gpt-5.3-codex"}, {"slug": "gpt-5.1-codex-mini"}, {}]}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        captured["url"] = url
        return _Resp()

    from one.providers import codex_responses as cod_mod

    _fake_client_factory(monkeypatch, cod_mod, handler)
    detailed = await ad.list_models_detailed("tok")
    assert detailed == [
        {"id": "gpt-5.3-codex", "contextWindow": None},
        {"id": "gpt-5.1-codex-mini", "contextWindow": None},
    ]
    assert "client_version=" in captured["url"]


def test_provider_registry_has_chatgpt_codex_adapter():
    registry = build_provider_registry()
    assert isinstance(registry["chatgpt"], CodexResponsesAdapter)


# --- run_oauth_login orchestration ----------------------------------------------


class _RecordingRegistry:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.registered: list[tuple[str, list[dict[str, Any]]]] = []

    def set_oauth_record(self, provider: str, record: dict[str, Any]) -> None:
        self.records[provider] = record

    def register_models(self, provider: str, models: list[dict[str, Any]]) -> int:
        self.registered.append((provider, models))
        return len(models)

    def persist_models(self, provider: str, models: list[dict[str, Any]]) -> None:
        self.registered.append(("persist:" + provider, models))


class _FakeAdapter:
    async def list_models_detailed(self, key: str) -> list[dict[str, Any]]:
        if not key:
            raise RuntimeError("no token")
        return [{"id": "m1", "contextWindow": None}]


@pytest.mark.asyncio
async def test_run_oauth_login_anthropic_paste(monkeypatch):
    reg = _RecordingRegistry()

    from one.core import provider_login as pl_mod

    async def fake_login(spec, **kwargs):
        # run_login returns a NORMALIZED record (flows convert internally).
        return {
            "type": "oauth",
            "access": "sk-ant-oat01-z",
            "refresh": "r",
            "expires": 4102444800000,
            "accountId": None,
        }

    monkeypatch.setattr(pl_mod, "run_login", fake_login)
    ok, error, fetched = await run_oauth_login(
        "anthropic", _FakeAdapter(), reg, open_url=lambda u: None, read_line=lambda: ""
    )
    assert ok and error is None
    assert fetched == [{"id": "m1", "contextWindow": None}]
    assert reg.records["anthropic"]["access"] == "sk-ant-oat01-z"
    assert ("anthropic", fetched) in reg.registered


@pytest.mark.asyncio
async def test_run_oauth_login_does_not_double_convert(monkeypatch):
    """Regression: HOTFIX-3 — record from run_login must be stored verbatim.

    The flows already normalize via build_oauth_record; a second conversion
    raised "missing access_token (received keys: ['access', ...])".
    """
    reg = _RecordingRegistry()

    from one.core import provider_login as pl_mod

    record = {"type": "oauth", "access": "tok", "refresh": "r", "expires": 1, "accountId": "acc"}

    async def fake_login(spec, **kwargs):
        return dict(record)

    monkeypatch.setattr(pl_mod, "run_login", fake_login)
    ok, error, fetched = await run_oauth_login(
        "chatgpt", _FakeAdapter(), reg, open_url=lambda u: None, read_line=lambda: ""
    )
    assert ok and error is None
    assert reg.records["chatgpt"] == record


@pytest.mark.asyncio
async def test_run_oauth_login_unknown_provider():
    ok, error, fetched = await run_oauth_login("openai", _FakeAdapter(), _RecordingRegistry())
    assert not ok and "OAuth login not available" in (error or "")
    assert fetched is None


@pytest.mark.asyncio
async def test_run_oauth_login_passes_account_id_headers(monkeypatch):
    """HOTFIX-5: Codex models endpoint needs ChatGPT-Account-Id — the record's
    accountId must reach list_models_detailed as extra headers."""
    reg = _RecordingRegistry()

    from one.core import provider_login as pl_mod

    seen_headers: list[dict[str, str] | None] = []

    class _HdrAdapter:
        async def list_models_detailed(self, key, headers=None):
            seen_headers.append(headers)
            if not key:
                raise RuntimeError("no token")
            return [{"id": "gpt-5.3-codex", "contextWindow": None}]

    async def fake_login(spec, **kwargs):
        return {"type": "oauth", "access": "tok", "refresh": "r", "expires": 1, "accountId": "acc-1"}

    monkeypatch.setattr(pl_mod, "run_login", fake_login)
    ok, error, fetched = await run_oauth_login(
        "chatgpt", _HdrAdapter(), reg, open_url=lambda u: None, read_line=lambda: ""
    )
    assert ok and error is None
    assert fetched == [{"id": "gpt-5.3-codex", "contextWindow": None}]
    assert seen_headers == [{"ChatGPT-Account-Id": "acc-1"}]


def test_model_registry_set_oauth_record_delegates():
    """Regression: HOTFIX-4 — registry must delegate OAuth record writes.

    run_oauth_login only receives the registry; without the delegation the
    persist step crashed with AttributeError after a SUCCESSFUL token exchange.
    """
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelInfo, ModelRegistry

    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    record = {
        "type": "oauth",
        "access": "tok",
        "refresh": "r",
        "expires": 4102444800000,
        "accountId": "acc",
    }
    registry.set_oauth_record("chatgpt", record)
    assert auth.get_oauth_record("chatgpt") == record

    model = ModelInfo(
        id="gpt-5.3-codex",
        provider="chatgpt",
        context_window=400_000,
        reasoning=True,
    )
    assert registry.has_configured_auth(model) is True
    resolved = registry.get_api_key_and_headers(model)
    assert resolved["ok"] is True
    assert resolved["apiKey"] == "tok"
    assert resolved["headers"]["ChatGPT-Account-Id"] == "acc"


@pytest.mark.asyncio
async def test_validate_and_fetch_forwards_headers(monkeypatch):
    """HOTFIX-6: validate_and_fetch forwards extra headers to both the models
    endpoint fetch and the validation chat probe."""
    from one.core import provider_login as pl_mod

    seen_headers: list[dict[str, str] | None] = []

    class _HdrAdapter:
        async def list_models_detailed(self, key, headers=None):
            seen_headers.append(headers)
            return [{"id": "gpt-5.1-codex-max", "contextWindow": None}]

        async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
            seen_headers.append(headers)
            # Pretend validation succeeded
            raise RuntimeError("400 probe model rejected (key is valid)")

    headers = {"ChatGPT-Account-Id": "acc-99"}
    ok, error, fetched = await pl_mod.validate_and_fetch(
        _HdrAdapter(), "sk-test", "chatgpt", headers=headers
    )
    # The 400 is a known probe-reject case → soft pass with models.
    assert ok is True
    assert "probe model rejected" in (error or "")
    assert fetched == [{"id": "gpt-5.1-codex-max", "contextWindow": None}]
    # First call = models endpoint; second call = chat probe.
    assert seen_headers == [headers, headers]


@pytest.mark.asyncio
async def test_validate_and_fetch_headers_none_by_default(monkeypatch):
    """HOTFIX-6: omitting headers defaults to None (backward compatible)."""
    from one.core import provider_login as pl_mod

    seen_headers: list[dict[str, str] | None] = []

    class _HdrAdapter:
        async def list_models_detailed(self, key, headers=None):
            seen_headers.append(headers)
            return [{"id": "m1", "contextWindow": None}]

        async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
            seen_headers.append(headers)
            raise RuntimeError("400 probe model rejected (key is valid)")

    ok, error, fetched = await pl_mod.validate_and_fetch(
        _HdrAdapter(), "sk-test", "chatgpt"
    )
    assert ok is True
    assert seen_headers == [None, None]


@pytest.mark.asyncio
async def test_validate_and_fetch_codex_validation_error_with_headers(monkeypatch):
    """HOTFIX-6: 400 validation error surfaces the real server reason when
    headers are forwarded (the old bug — headers dropped → wrong shape → empty
    models → silent "no models endpoint")."""
    from one.core import provider_login as pl_mod

    class _HdrAdapter:
        async def list_models_detailed(self, key, headers=None):
            return [{"id": "gpt-5.1-codex", "contextWindow": None}]

        async def chat(self, api_key, model, messages, thinking_level, headers=None, on_delta=None, on_thinking_delta=None, max_tokens=None, images=None, storage_dir=""):
            raise RuntimeError("chatgpt API error 400: The 'gpt-5.1-codex' model is not supported")

    headers = {"ChatGPT-Account-Id": "acc-99"}
    ok, error, fetched = await pl_mod.validate_and_fetch(
        _HdrAdapter(), "sk-test", "chatgpt", headers=headers
    )
    # 400 from the probe → soft pass; the real reason is surfaced to the user.
    assert ok is True
    assert error is not None
    assert "gpt-5.1-codex" in error
    assert "not supported" in error


@pytest.mark.asyncio
async def test_codex_list_models_detailed_raises_on_shape_mismatch(monkeypatch):
    """HOTFIX-6: list_models_detailed raises RuntimeError when the JSON
    response lacks a 'models' key (was silently returning [])."""
    from unittest.mock import patch

    import httpx

    adapter = CodexResponsesAdapter()

    # Simulate a response that has no "models" key — happens when the request
    # is missing ChatGPT-Account-Id (the endpoint returns 200 with empty data).
    class _Resp:
        is_error = False

        def json(self) -> dict:
            return {"status": "ok", "count": 0}

    mock_resp = _Resp()

    async def fake_get(*args, **kwargs):
        return mock_resp

    with patch.object(httpx.AsyncClient, "get", fake_get):
        with pytest.raises(RuntimeError, match="unexpected shape"):
            # list_models_detailed expects headers=None for API-key path,
            # but the shape validation fires regardless.
            await adapter.list_models_detailed("fake-key", None)


@pytest.mark.asyncio
async def test_codex_list_models_detailed_raises_on_empty_models(monkeypatch):
    """When /models returns {"models": []} — the client_version gating
    response — list_models_detailed must raise a clear RuntimeError."""
    from one.providers.codex_responses import CodexResponsesAdapter

    class _Resp:
        status_code = 200
        text = ""
        is_error = False

        def json(self) -> dict[str, Any]:
            return {"models": []}

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _Resp:
        return _Resp()

    import one.providers.codex_responses as cod_mod

    _fake_client_factory(monkeypatch, cod_mod, handler)
    adapter = CodexResponsesAdapter()
    with pytest.raises(RuntimeError, match="returned 0 models"):
        await adapter.list_models_detailed("tok")


def test_model_registry_codex_seed_slugs():
    """GPT-5.6 seeds are the current builtin codex model IDs."""
    from one.core.model_registry import BUILTIN_MODELS

    chatgpt_slugs = [m.id for m in BUILTIN_MODELS if m.provider == "chatgpt"]
    # Old codex slugs must NOT be present.
    assert "gpt-5.3-codex" not in chatgpt_slugs
    assert "gpt-5.1-codex-max" not in chatgpt_slugs
    assert "gpt-5.1-codex" not in chatgpt_slugs
    # Current GPT-5.6 seeds must be present.
    assert "gpt-5.6-sol" in chatgpt_slugs
    assert "gpt-5.6-terra" in chatgpt_slugs
    assert "gpt-5.6-luna" in chatgpt_slugs
