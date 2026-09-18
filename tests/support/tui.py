from __future__ import annotations

from pathlib import Path
from typing import Any


def _visible_text_area_text(widget: Any) -> str:
    """Read TextArea compositor lines, rather than its backing ``.text``."""
    height = max(1, int(widget.size.height))
    return "\n".join(widget.render_line(y).text for y in range(height))

class _FakeLoader:
    cwd = "/tmp/project"

    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."

def _mk_app_session(tmp_path: Path, runtime_key: str | None = None, settings_override: dict | None = None):
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager

    auth = AuthStorage.in_memory()
    if runtime_key:
        auth.set_runtime_api_key("openai", runtime_key)
    registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    base_settings: dict[str, Any] = {"tools": {"maxSteps": 4, "timeoutSec": 5}, "bash": {"showOutput": True}}
    if settings_override:
        from copy import deepcopy
        merged = deepcopy(base_settings)
        for k, v in settings_override.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        settings_to_use = merged
    else:
        settings_to_use = base_settings
    settings = SettingsManager.in_memory(settings_to_use)
    session_manager = SessionManager.in_memory(str(tmp_path))
    return AgentSession(session_manager, settings, registry, _FakeLoader(), model, "medium")

async def _submit(app, pilot, text: str) -> None:
    from textual.widgets import TextArea

    input_widget = app.query_one("#input", TextArea)
    input_widget.text = text
    await input_widget.action_submit()
    await pilot.pause()
