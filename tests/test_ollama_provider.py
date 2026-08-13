from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.providers.openai_compatible import OpenAICompatibleAdapter
from one.providers.registry import build_provider_registry


def test_provider_registry_includes_ollama() -> None:
    registry = build_provider_registry()
    assert "ollama" in registry


def test_provider_registry_ollama_default_base_url() -> None:
    registry = build_provider_registry()
    provider = registry["ollama"]
    assert isinstance(provider, OpenAICompatibleAdapter)
    assert provider.base_url == "http://localhost:11434/v1"


def test_provider_registry_ollama_env_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    registry = build_provider_registry()
    provider = registry["ollama"]
    assert isinstance(provider, OpenAICompatibleAdapter)
    assert provider.base_url == "http://127.0.0.1:11434/v1"


def test_model_registry_ollama_available_without_auth(tmp_path: Path) -> None:
    auth = AuthStorage.in_memory()
    # Isolate from the real ~/.config/one/models.json (builtins only).
    registry = ModelRegistry.create(auth, str(tmp_path / "no" / "models.json"))
    model = registry.find("ollama", "llama3.1")
    assert model is not None
    assert model.base_url is None
    available = registry.get_available()
    assert any(m.provider == "ollama" and m.id == "llama3.1" for m in available)
    auth_data = registry.get_api_key_and_headers(model)
    assert auth_data["ok"] is True
    assert auth_data["apiKey"] == ""
    status = registry.get_provider_auth_status("ollama")
    assert status["requiresApiKey"] is False


def test_cli_accepts_ollama_url_flag(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "one.cli.main",
            "--ollama-url",
            "http://127.0.0.1:11434",
            "--list-models",
            "ollama",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 0
    assert "ollama/llama3.1" in res.stdout
