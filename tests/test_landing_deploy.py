# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("landing_deploy", Path(__file__).parents[1] / "landing_page/deploy.py")
assert SPEC and SPEC.loader
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    for key in deploy.KEYS:
        monkeypatch.delenv(key, raising=False)
    landing = tmp_path / "landing_page"
    landing.mkdir()
    (landing / "index.html").write_text("landing")
    (landing / "deploy.py").write_text("not static")
    (landing / "image with spaces.svg").write_text("svg")
    (landing / ".env").write_text(
        'FTP_HOST=ftp.example.org\nFTP_DIR=/public_html/\nFTP_USER=test\n'
        'FTP_PASSWORD=secret-test\nPUBLIC_SITE_URL=https://example.org\n'
    )
    dist = tmp_path / "website/dist"
    (dist / "pagefind").mkdir(parents=True)
    (dist / "nested").mkdir()
    (dist / "index.html").write_text("docs")
    (dist / "nested/index.html").write_text("nested")
    (dist / "pagefind/index.js").write_text("search")
    return tmp_path


def fake_commands(monkeypatch, fail=None):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if fail and command[:len(fail)] == fail:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(deploy.subprocess, "run", run)
    return calls


def test_docs_dry_run_builds_without_curl(tree, monkeypatch, capsys):
    calls = fake_commands(monkeypatch)
    deploy.deploy(tree, ["--docs", "--dry-run"])
    assert [c[0] for c in calls] == [["npm", "ci"], ["npm", "run", "build"], ["npm", "run", "check:links"]]
    assert all(c[1]["env"]["DOCS_BASE"] == "/docs/" for c in calls)
    text = capsys.readouterr().out
    assert "/public_html/docs/pagefind/index.js" in text
    assert "secret-test" not in text
    assert "landing_page/index.html" not in text


def test_default_landing_upload_secrets_only_in_stdin(tree, monkeypatch):
    calls = fake_commands(monkeypatch)
    deploy.deploy(tree, [])
    assert len(calls) == 2
    for command, kwargs in calls:
        assert command[0] == "curl" and "--ssl-reqd" in command
        assert "secret-test" not in " ".join(command)
        assert 'user = "test:secret-test"' in kwargs["input"]
        assert "deploy.py" not in kwargs["input"]
    assert "image%20with%20spaces.svg" in calls[0][1]["input"]
    assert 'public_html/index.html"' in calls[-1][1]["input"]


def test_all_prepares_before_upload_and_recurses(tree, monkeypatch):
    calls = fake_commands(monkeypatch)
    deploy.deploy(tree, ["--all"])
    assert [c[0][0] for c in calls[:3]] == ["npm"] * 3
    configs = [kwargs["input"] for command, kwargs in calls if command[0] == "curl"]
    assert len(configs) == 5
    assert "docs/pagefind/index.js" in configs[0]
    assert "docs/nested/index.html" in configs[1]
    assert "docs/index.html" in configs[2]


def test_build_failure_never_uploads(tree, monkeypatch):
    calls = fake_commands(monkeypatch, ["npm", "run", "build"])
    with pytest.raises(subprocess.CalledProcessError):
        deploy.deploy(tree, ["--all"])
    assert not any(c[0][0] == "curl" for c in calls)


@pytest.mark.parametrize("value", ["/", "/../docs/", "/docs/../", "docs", "//docs/", "/%2e%2e/"])
def test_unsafe_docs_base_rejected(tree, monkeypatch, value):
    calls = fake_commands(monkeypatch)
    with (tree / "landing_page/.env").open("a") as f:
        f.write(f"DOCS_BASE={value}\n")
    with pytest.raises(ValueError):
        deploy.deploy(tree, ["--docs"])
    assert not calls


def test_no_execution_of_env_and_unknown_keys_rejected(tmp_path):
    config = tmp_path / "env"
    config.write_text("FTP_PASSWORD=$(touch /never-run)\n")
    assert deploy.load_config(config)["FTP_PASSWORD"] == "$(touch /never-run)"
    config.write_text("BASH_ENV=bad\n")
    with pytest.raises(ValueError):
        deploy.load_config(config)


def test_symlink_rejected(tree):
    (tree / "website/dist/leak").symlink_to(tree / "landing_page/.env")
    with pytest.raises(ValueError):
        deploy.files_in(tree / "website/dist", recursive=True)


def test_upload_failure_stops_without_exposing_diagnostics(tree, monkeypatch):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 67, stderr="secret-test")

    monkeypatch.setattr(deploy.subprocess, "run", run)
    with pytest.raises(ValueError, match="curl exit 67") as exc:
        deploy.deploy(tree, [])
    assert "secret-test" not in str(exc.value)


@pytest.mark.parametrize("setting", ["cli", "env"])
def test_self_signed_opt_in_keeps_tls(tree, monkeypatch, capsys, setting):
    calls = fake_commands(monkeypatch)
    args = []
    if setting == "cli":
        args.append("--allow-self-signed")
    else:
        with (tree / "landing_page/.env").open("a") as f:
            f.write("FTP_ALLOW_SELF_SIGNED=true\n")
    deploy.deploy(tree, args)
    assert all("--insecure" in command and "--ssl-reqd" in command for command, _ in calls)
    assert "server identity is not verified" in capsys.readouterr().err


def test_default_verifies_certificate(tree, monkeypatch):
    calls = fake_commands(monkeypatch)
    deploy.deploy(tree, [])
    assert all("--insecure" not in command for command, _ in calls)


@pytest.mark.parametrize("config", ["FTP_TLS=false\n", "FTP_ALLOW_SELF_SIGNED=invalid\n"])
def test_invalid_self_signed_configuration_never_connects(tree, monkeypatch, config):
    calls = fake_commands(monkeypatch)
    with (tree / "landing_page/.env").open("a") as f:
        f.write(config)
    with pytest.raises(ValueError):
        deploy.deploy(tree, ["--allow-self-signed"])
    assert not calls


def test_self_signed_dry_run_never_connects(tree, monkeypatch):
    calls = fake_commands(monkeypatch)
    deploy.deploy(tree, ["--landing", "--dry-run", "--allow-self-signed"])
    assert not calls
