from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_model_registry_llama_cpp_available_without_auth(tmp_path: Path) -> None:
    auth = AuthStorage.in_memory()
    # Isolate from the real ~/.config/one/models.json (builtins only).
    registry = ModelRegistry.create(auth, str(tmp_path / "no" / "models.json"))
    model = registry.find("llama.cpp", "local")
    assert model is not None
    # Builtin model must not hardcode a base URL, otherwise it would override
    # LLAMA_CPP_BASE_URL / --llama-cpp-url in _invoke_provider.
    assert model.base_url is None
    available = registry.get_available()
    assert any(m.provider == "llama.cpp" and m.id == "local" for m in available)
    auth_data = registry.get_api_key_and_headers(model)
    assert auth_data["ok"] is True
    assert auth_data["apiKey"] == ""
    status = registry.get_provider_auth_status("llama.cpp")
    assert status["requiresApiKey"] is False


def test_models_json_url_overrides_builtin_llama_cpp_base_url(tmp_path: Path) -> None:
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
                            "url": "http://192.168.200.20:8089",
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
    # models.json url must override the builtin (previously hardcoded) URL.
    assert model.base_url == "http://192.168.200.20:8089"


def test_placeholder_api_key_not_configured(tmp_path: Path) -> None:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "YOUR_OPENAI_API_KEY_HERE")
    registry = ModelRegistry.create(auth, str(tmp_path / "no" / "models.json"))
    openai_model = registry.find("openai", "gpt-4.1")
    assert openai_model is not None
    # Placeholder keys must not count as configured auth.
    assert registry.has_configured_auth(openai_model) is False
    assert registry.get_api_key_and_headers(openai_model)["ok"] is False
    avail = registry.get_available()
    assert all(m.provider != "openai" for m in avail)
    assert any(m.provider == "llama.cpp" and m.id == "local" for m in avail)


def test_select_default_prefers_exact_default(tmp_path: Path) -> None:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "sk-real-key-123")
    registry = ModelRegistry.create(auth, str(tmp_path / "no" / "models.json"))
    model = registry.select_default("openai", "gpt-4o")
    assert model is not None and model.id == "gpt-4o"


def test_select_default_falls_back_to_default_provider(tmp_path: Path) -> None:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "sk-real-key-123")
    registry = ModelRegistry.create(auth, str(tmp_path / "no" / "models.json"))
    # Stale default model id -> falls back to the default provider's first model.
    model = registry.select_default("openai", "gpt-99-nonexistent")
    assert model is not None and model.provider == "openai"


def test_select_default_placeholder_provider_falls_to_llama(tmp_path: Path) -> None:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "YOUR_OPENAI_API_KEY_HERE")
    registry = ModelRegistry.create(auth, str(tmp_path / "no" / "models.json"))
    # openai has only a placeholder key -> not available, default resolves to llama.cpp.
    model = registry.select_default("openai", "gpt-4.1")
    assert model is not None
    assert model.provider == "llama.cpp"
    assert model.id == "local"


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

    # A local extension directory and a single-file extension.
    ext_dir = tmp_path / "my-ext"
    ext_dir.mkdir()
    (ext_dir / "register.py").write_text("def register(ctx):\n    return {}\n", encoding="utf-8")
    ext_file = tmp_path / "single_ext.py"
    ext_file.write_text("def register(ctx):\n    return {}\n", encoding="utf-8")

    run_cmd(["install", str(ext_dir)])
    out = run_cmd(["list"]).stdout
    assert "my-ext" in out
    # Files are actually copied into the agent extensions dir.
    agent_ext = tmp_path / ".one" / "agent" / "extensions" / "my-ext"
    assert (agent_ext / "register.py").exists()

    # idempotent install
    out_install_again = run_cmd(["install", str(ext_dir)]).stdout
    assert "already installed" in out_install_again.lower()

    # single-file package
    run_cmd(["install", str(ext_file)])
    assert (tmp_path / ".one" / "agent" / "extensions" / "single_ext.py").exists()

    # missing source -> not found
    missing = run_cmd_no_check(["install", str(tmp_path / "nope.py")])
    assert missing.returncode == 1
    assert "not found" in missing.stdout.lower()

    run_cmd(["config", "tools.maxSteps", "9"])
    cfg = run_cmd(["config", "tools.maxSteps"]).stdout
    assert "9" in cfg

    missing_cfg = run_cmd_no_check(["config", "not.exists"])
    assert missing_cfg.returncode == 1
    assert "not found" in missing_cfg.stdout.lower()

    # update with an explicit package: up-to-date check
    up_to_date = run_cmd(["update", "my-ext"]).stdout
    assert "up to date" in up_to_date.lower()

    run_cmd(["remove", "my-ext"])
    out2 = run_cmd(["list"]).stdout
    assert "my-ext" not in out2
    # Files on disk are removed too.
    assert not agent_ext.exists()

    remove_missing = run_cmd_no_check(["remove", "my-ext"])
    assert remove_missing.returncode == 1
    assert "not installed" in remove_missing.stdout.lower()

    update_missing = run_cmd_no_check(["update", "my-ext"])
    assert update_missing.returncode == 1
    assert "not installed" in update_missing.stdout.lower()

    # update without args syncs the manifest with the extensions dir.
    (tmp_path / ".one" / "agent" / "extensions" / "dropped.py").write_text("x = 1\n", encoding="utf-8")
    synced = run_cmd(["update"]).stdout
    assert "dropped.py" in synced
    out3 = run_cmd(["list"]).stdout
    assert "dropped.py" in out3


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


def test_cli_run_requires_task(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 2
    assert "Usage: one run" in res.stdout


def test_cli_run_accepts_flags(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    # --json flag BEFORE task must be accepted by argparse (reaches run dispatch,
    # output is JSON with goalSuccess/finished fields).
    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "--json", "task"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 1
    data = json.loads(res.stdout)
    assert "goalSuccess" in data
    assert "finished" in data
    assert "summary" in data

    # --badflag must produce argparse error (returncode 2)
    res_bad = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "--badflag", "task"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res_bad.returncode == 2


def test_cli_run_resume_flag_parses(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    # --resume flag is accepted by argparse; run dispatches (returns 1 on
    # connection failure since no model server is running).
    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "--resume", "task"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 1
    # Must not be a usage/argparse error.
    assert "Usage:" not in res.stdout


def test_parse_args_run_value_flags_after_task():
    """Phase 12: value-taking flags keep their values with the run subcommand."""
    from one.cli.args import parse_args

    p = parse_args(["run", "task", "--provider", "openrouter", "--model", "openai/gpt-4.1"])
    assert p.command == "run"
    assert p.provider == "openrouter"
    assert p.model == "openai/gpt-4.1"
    assert p.run_task == "task"


def test_parse_args_run_value_flags_before_task():
    from one.cli.args import parse_args

    p = parse_args(["run", "--provider", "openrouter", "task", "--api-key", "sk-x", "--thinking", "high"])
    assert p.provider == "openrouter"
    assert p.api_key == "sk-x"
    assert p.thinking == "high"
    assert p.run_task == "task"


def test_parse_args_run_list_models_optional_value():
    from one.cli.args import parse_args

    # --list-models without a value (nargs="?") keeps run_task intact.
    p = parse_args(["run", "task", "--list-models"])
    assert p.list_models is True
    assert p.run_task == "task"

    # --list-models with a value pairs it.
    p2 = parse_args(["run", "task", "--list-models", "gpt"])
    assert p2.list_models == "gpt"
    assert p2.run_task == "task"


def test_parse_args_run_repeatable_and_short_flags():
    from one.cli.args import parse_args

    p = parse_args(["run", "task", "--param", "a=1", "--param", "b=2", "--theme", "dark", "-P", "c=3"])
    assert p.params == ["a=1", "b=2", "c=3"]
    assert p.themes == ["dark"]
    assert p.run_task == "task"


def test_cli_run_value_flags_not_argparse_error(tmp_path: Path):
    """Phase 12: `one run task --provider X --model Y` must not be an argparse error."""
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "task", "--provider", "openrouter", "--model", "openai/gpt-4.1", "--json"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    # Not a usage/argparse error (returncode 2); dispatch reached the run mode.
    assert res.returncode != 2
    assert "expected one argument" not in res.stdout + res.stderr
    assert "Usage:" not in res.stdout + res.stderr


def test_cli_run_resume_without_task_is_valid(tmp_path: Path):
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    # `one run --resume` without a task must not be a usage error: it resumes
    # the last user message. With no prior session it reports that cleanly.
    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "--resume"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 1
    assert "No previous user message to resume." in res.stdout


def test_cli_help_shows_plan_in_tools_help(tmp_path: Path):
    """Phase 7 regression: --tools help text must include 'plan'."""
    env = os.environ.copy()
    env["ONE_CODING_AGENT_DIR"] = str(tmp_path / ".one" / "agent")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    res = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "--help"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 0
    # The --tools line must include "plan" among the default tools.
    for line in res.stdout.splitlines():
        if "--tools" in line:
            assert "plan" in line, f"--tools line missing 'plan': {line}"
            break
    else:
        pytest.fail("--tools help line not found in --help output")
