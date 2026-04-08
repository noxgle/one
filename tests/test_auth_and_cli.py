from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry


def test_auth_precedence_runtime_file_env(tmp_path: Path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"apiKeys": {"openai": "from-file"}}), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")

    auth = AuthStorage.create(str(auth_path))
    assert auth.get_api_key("openai") == "from-file"

    auth.set_runtime_api_key("openai", "from-runtime")
    assert auth.get_api_key("openai") == "from-runtime"


def test_auth_error_message_has_provider_hint(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    res = registry.get_api_key_and_headers(model)
    assert res["ok"] is False
    assert "OPENAI_API_KEY" in res["error"]


def test_cli_package_and_config_commands(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    def run_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "one.cli.main", *args],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )

    def run_cmd_no_check(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "one.cli.main", *args],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    run_cmd(["install", "pkg-a"])
    # idempotent install
    out_install_again = run_cmd(["install", "pkg-a"]).stdout
    assert "already installed" in out_install_again.lower()
    out = run_cmd(["list"]).stdout
    assert "pkg-a" in out

    run_cmd(["config", "tools.maxSteps", "9"])
    cfg = run_cmd(["config", "tools.maxSteps"]).stdout
    assert "9" in cfg

    missing = run_cmd_no_check(["config", "not.exists"])
    assert missing.returncode == 1
    assert "not found" in missing.stdout.lower()

    run_cmd(["remove", "pkg-a"])
    out2 = run_cmd(["list"]).stdout
    assert "pkg-a" not in out2

    remove_missing = run_cmd_no_check(["remove", "pkg-a"])
    assert remove_missing.returncode == 1
    assert "not installed" in remove_missing.stdout.lower()

    update_missing = run_cmd_no_check(["update", "pkg-a"])
    assert update_missing.returncode == 1
    assert "not installed" in update_missing.stdout.lower()
