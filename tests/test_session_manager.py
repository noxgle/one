from __future__ import annotations

import json
from pathlib import Path

from one.core.session_manager import SessionManager


def test_session_append_and_context(tmp_path: Path):
    sm = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    sm.append_model_change("openai", "gpt-4.1")
    sm.append_thinking_level_change("medium")
    sm.append_message({"role": "user", "content": "hello"})
    sm.append_message({"role": "assistant", "content": [{"type": "text", "text": "hi"}], "provider": "openai", "model": "gpt-4.1"})

    ctx = sm.build_session_context()
    assert ctx["thinkingLevel"] == "medium"
    assert ctx["model"]["provider"] == "openai"
    assert len(ctx["messages"]) == 2


def test_migrate_v1_to_v3(tmp_path: Path):
    session_file = tmp_path / "legacy.jsonl"
    lines = [
        json.dumps({"type": "session", "id": "s1", "timestamp": "2020-01-01T00:00:00Z", "cwd": str(tmp_path)}),
        json.dumps({"type": "message", "timestamp": "2020-01-01T00:00:01Z", "message": {"role": "hookMessage", "content": "x"}}),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    sm = SessionManager.open(str(session_file), str(tmp_path))
    entries = sm.get_entries()
    assert entries[0]["message"]["role"] == "custom"
    assert sm.get_header()["version"] == 3
