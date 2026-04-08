from __future__ import annotations

from pathlib import Path

from one import config


def test_get_agent_dir_prefers_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(config.ENV_AGENT_DIR, str(tmp_path / "custom-agent-dir"))
    out = config.get_agent_dir()
    assert out == str(tmp_path / "custom-agent-dir")


def test_get_agent_dir_defaults_to_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(config.ENV_AGENT_DIR, raising=False)
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    out = config.get_agent_dir()
    assert out == str(tmp_path / "xdg" / "one")


def test_get_agent_dir_migrates_legacy_to_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(config.ENV_AGENT_DIR, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    legacy = tmp_path / ".one" / "agent"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "settings.json").write_text('{"a":1}', encoding="utf-8")
    (legacy / "models.json").write_text('{"providers":{}}', encoding="utf-8")
    out = config.get_agent_dir()
    xdg = tmp_path / "xdg" / "one"
    assert out == str(xdg)
    assert (xdg / "settings.json").exists()
    assert (xdg / "models.json").exists()
