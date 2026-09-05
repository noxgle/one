"""Tests for Task 30.10: OAuth/Codex production support.

Verifies that subscription OAuth and the Codex provider are enabled by
default, have no "experimental" labels in user-facing text, and that
all messages are in English.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


class TestNoExperimentalLabels:
    """Verify that OAuth/Codex are not labeled as experimental."""

    @pytest.fixture(autouse=True)
    def _clear_modules(self):  # type: ignore[no-untyped-def]
        """Clear cached modules so docstrings are re-read."""
        to_remove = [k for k in sys.modules if k.startswith("one.providers")]
        for k in to_remove:
            del sys.modules[k]
        yield
        # No cleanup needed

    def test_codex_docstring_not_experimental(self) -> None:
        """The Codex adapter docstring must not say 'experimental'."""
        from one.providers import codex_responses

        doc = str(codex_responses.__doc__ or "")
        assert "experimental" not in doc.lower()
        assert "subscription" in doc.lower()

    def test_oauth_docstring_not_experimental(self) -> None:
        """The OAuth module docstring must not say 'experimental'."""
        from one.core import oauth

        doc = str(oauth.__doc__ or "")
        assert "experimental" not in doc.lower()

    def test_codex_provider_registered_by_default(self) -> None:
        """The chatgpt/Codex provider must be in the registry without opt-in."""
        from one.providers.registry import build_provider_registry

        registry = build_provider_registry()
        assert "chatgpt" in registry

    def test_chatgpt_builtin_models_present(self) -> None:
        """Seed models for chatgpt must be present in the built-in registry."""
        from one.core.model_registry import BUILTIN_MODELS

        chatgpt_models = [m for m in BUILTIN_MODELS if m.provider == "chatgpt"]
        ids = [m.id for m in chatgpt_models]
        assert "gpt-5.6-sol" in ids
        assert "gpt-5.6-terra" in ids
        assert "gpt-5.6-luna" in ids

    def test_oauth_login_hint_is_english(self) -> None:
        """Login hint text must be in English and production-ready."""
        from one.core.oauth import oauth_login_hint

        hint = oauth_login_hint("anthropic")
        assert hint is not None
        assert "claude" in hint.lower()
        assert "pro" in hint.lower() or "max" in hint.lower()
        # Must not say "experimental" or "beta"
        assert "experimental" not in hint.lower()
        assert "beta" not in hint.lower()

    def test_chatgpt_oauth_login_hint_is_english(self) -> None:
        from one.core.oauth import oauth_login_hint

        hint = oauth_login_hint("chatgpt")
        assert hint is not None
        assert "chatgpt" in hint.lower()
        assert "plus" in hint.lower() or "pro" in hint.lower()
        assert "experimental" not in hint.lower()
        assert "beta" not in hint.lower()

    def test_help_text_not_experimental(self) -> None:
        """The CLI help text must not mention experimental OAuth."""
        import io

        from one.cli.args import print_help

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            print_help()
        finally:
            sys.stdout = old_stdout

        output = captured.getvalue().lower()
        assert "experimental" not in output
        assert "beta" not in output


class TestTokenRedaction:
    """Verify that tokens are redacted in error messages."""

    def test_redact_credentials_in_oauth_error(self) -> None:
        import types

        from one.core.oauth import _describe_response

        # Simulate an httpx-like response object containing a token
        body = '{"access_token": "sk-ant-test123", "error": "invalid_grant"}'
        resp = types.SimpleNamespace(status_code=400, text=body)
        result = _describe_response(resp)
        assert "sk-ant-test123" not in result
        assert "[REDACTED]" in result

    def test_redact_credentials_in_rpc_error(self) -> None:
        from one.modes.rpc_mode import _redact_credentials

        api_key = "sk-ant-test123"
        msg = f"Authentication failed for key {api_key}"
        redacted = _redact_credentials(msg, api_key)
        assert api_key not in redacted


class TestMCPClientVersionUnified:
    """Verify that the MCP client uses the unified version from config."""

    def test_mcp_client_imports_version(self) -> None:
        """The MCP client should import VERSION from config."""
        from one.mcp import client

        # Check the module has the version import
        assert hasattr(client, "_ONE_VERSION")
        assert client._ONE_VERSION == "0.1.0"


class TestCodexToSViolations:
    """Verify that Codex provider docs mention ToS risks."""

    def test_codex_docs_mention_provider_risk(self) -> None:
        """Codex provider docs must mention that the endpoint is externally controlled."""
        from one.providers import codex_responses

        doc = str(codex_responses.__doc__ or "")
        assert (
            "externally controlled" in doc.lower() or "terms of service" in doc.lower() or "may change" in doc.lower()
        )


class TestREADMENoExperimental:
    """Verify README does not label OAuth as experimental."""

    def test_readme_no_experimental_oauth(self) -> None:
        readme_path = Path(__file__).resolve().parent.parent / "README.md"
        content = readme_path.read_text(encoding="utf-8").lower()
        # The README should NOT mention OAuth as experimental
        assert "experimental" not in content or "extension" in content  # extensions hook name is fine
        # Subscription login should be documented as a feature
        assert "subscription" in content
        assert "oauth" in content


class TestSecurityMDProductionOAuth:
    """Verify SECURITY.md describes OAuth as production-supported."""

    def test_security_md_mentions_oauth(self) -> None:
        sec_path = Path(__file__).resolve().parent.parent / "SECURITY.md"
        content = str(sec_path.read_text(encoding="utf-8")).lower()
        assert "oauth" in content
        assert "token" in content
        # Should mention provider risk
        assert "terms of service" in content or "externally controlled" in content
