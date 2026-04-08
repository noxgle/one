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


def test_auth_storage_remove_stored_api_key(tmp_path: Path):
    auth_path = tmp_path / "auth.json"
    auth = AuthStorage.create(str(auth_path))
    auth.set_stored_api_key("openai", "secret")
    assert auth.get_api_key("openai") == "secret"
    auth.remove_stored_api_key("openai")
    assert auth.get_api_key("openai") is None


def test_auth_error_message_has_provider_hint(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    res = registry.get_api_key_and_headers(model)
    assert res["ok"] is False
    assert "OPENAI_API_KEY" in res["error"]


def test_auth_storage_uses_generic_env_var_for_unknown_provider(monkeypatch):
    monkeypatch.setenv("CUSTOM_PROVIDER_API_KEY", "custom-key")
    auth = AuthStorage.in_memory()
    assert auth.get_api_key("custom-provider") == "custom-key"


def test_model_registry_resolve_dynamic_known_provider():
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    m = registry.resolve("custom-provider", "my-model-v1", allow_dynamic=True)
    assert m is not None
    assert m.provider == "custom-provider"
    assert m.id == "my-model-v1"


def test_model_registry_llama_cpp_available_without_auth() -> None:
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth)
    model = registry.find("llama.cpp", "local")
    assert model is not None
    available = registry.get_available()
    assert any(m.provider == "llama.cpp" and m.id == "local" for m in available)
    auth_data = registry.get_api_key_and_headers(model)
    assert auth_data["ok"] is True
    assert auth_data["apiKey"] == ""
    status = registry.get_provider_auth_status("llama.cpp")
    assert status["requiresApiKey"] is False


def test_model_registry_reads_llama_cpp_url_from_models_json(tmp_path: Path) -> None:
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "llama.cpp": [
                        {
                            "id": "local",
                            "reasoning": False,
                            "contextWindow": 32768,
                            "url": "http://192.168.200.38:8089",
                            "toolParser": [{"type": "raw-function-call"}, {"type": "json"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    auth = AuthStorage.in_memory()
    registry = ModelRegistry.create(auth, str(models_path))
    model = registry.find("llama.cpp", "local")
    assert model is not None
    assert model.base_url == "http://192.168.200.38:8089"
    assert model.tool_parser == [{"type": "raw-function-call"}, {"type": "json"}]


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


def test_cli_flag_validation_errors(tmp_path: Path):
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
            check=False,
        )

    bad_mode = run_cmd(["--mode", "bad"])
    assert bad_mode.returncode == 2
    assert "invalid --mode value" in bad_mode.stdout.lower()

    bad_thinking = run_cmd(["--thinking", "bad"])
    assert bad_thinking.returncode == 2
    assert "invalid --thinking value" in bad_thinking.stdout.lower()


def test_cli_flag_semantics_mode_print_session_fork(tmp_path: Path):
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
            check=False,
        )

    cases = [
        (["--mode", "rpc", "--print"], "--print cannot be combined with --mode rpc"),
        (["--no-session", "--session", "x"], "--no-session cannot be combined"),
        (["--session", "x", "--continue"], "--session cannot be combined with --continue"),
        (["--session", "x", "--resume"], "--session cannot be combined with --resume"),
        (["--continue", "--resume"], "--continue cannot be combined with --resume"),
        (["--fork", "x", "--session", "y"], "--fork cannot be combined"),
    ]
    for args, expected in cases:
        res = run_cmd(args)
        assert res.returncode == 2
        assert expected in res.stdout


def test_cli_unknown_tools_is_usage_error(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "--tools", "read,not-a-tool", "--print", "hello"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 2
    assert "Unknown tools: not-a-tool" in res.stdout


def test_cli_accepts_llama_cpp_url_flag(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "one.cli.main",
            "--llama-cpp-url",
            "http://192.168.200.38:8089",
            "--list-models",
            "llama.cpp",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 0
    assert "llama.cpp/local" in res.stdout
