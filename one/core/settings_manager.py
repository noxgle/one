from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from one.config import get_agent_dir, get_settings_path

DEFAULT_SETTINGS: dict[str, Any] = {
    "defaultProvider": None,
    "defaultModel": None,
    "defaultThinkingLevel": "medium",
    "enabledModels": [],
    "compaction": {
        "enabled": True,
        "thresholdPercent": 85,
        "recentTokens": 8192,
        "minKeptMessages": 20,
        "summarizeWithModel": True,
        "maxSummaryInputTokens": 20000,
    },
    "retry": {"enabled": True, "maxRetries": 3, "baseDelayMs": 1500, "maxDelayMs": 20000},
    "image": {"autoResize": True, "blockImages": False},
    "sessionDir": None,
    "theme": "default",
    "quietStartup": False,
    "steeringMode": "interrupt",
    "followUpMode": "queue",
    "transport": "http",
    "thinkingBudgets": {},
    "shellCommandPrefix": None,
    "tools": {"maxSteps": 6, "timeoutSec": 30, "approval": False, "approvalTools": ["bash", "write", "edit"]},
    "bash": {"showOutput": True},
    "subagents": {"enabled": True, "maxConcurrent": 2, "maxDepth": 3},
    "askUser": {"timeoutSec": 0},
    "packages": [],
    "budget": {"maxTokens": 0, "maxTimeSec": 0},
}


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class SettingsManager:
    def __init__(self, cwd: str, agent_dir: str, in_memory: bool = False, initial: dict[str, Any] | None = None) -> None:
        self._cwd = cwd
        self._agent_dir = agent_dir
        self._global_path = Path(get_settings_path()) if not in_memory else None
        self._project_path = Path(cwd) / ".one" / "settings.json" if not in_memory else None
        self._errors: list[dict[str, Any]] = []
        self._in_memory = in_memory
        if in_memory:
            self._global = _deep_merge(DEFAULT_SETTINGS, initial or {})
            self._project = {}
        else:
            self._global = self._load(self._global_path, "global")
            self._project = self._load(self._project_path, "project")

    def _load(self, path: Path | None, scope: str) -> dict[str, Any]:
        if not path or not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            self._errors.append({"scope": scope, "error": e})
            return {}

    def _save_global(self) -> None:
        if self._in_memory or not self._global_path:
            return
        self._global_path.parent.mkdir(parents=True, exist_ok=True)
        self._global_path.write_text(json.dumps(self._global, indent=2), encoding="utf-8")

    def drain_errors(self) -> list[dict[str, Any]]:
        out = self._errors[:]
        self._errors = []
        return out

    def merged(self) -> dict[str, Any]:
        return _deep_merge(_deep_merge(DEFAULT_SETTINGS, self._global), self._project)

    def get_global_settings(self) -> dict[str, Any]:
        return _deep_merge(DEFAULT_SETTINGS, self._global)

    def get_project_settings(self) -> dict[str, Any]:
        return self._project

    def get_default_provider(self) -> str | None:
        return self.merged().get("defaultProvider")

    def get_default_model(self) -> str | None:
        return self.merged().get("defaultModel")

    def get_default_thinking_level(self) -> str:
        return self.merged().get("defaultThinkingLevel", "medium")

    def get_enabled_models(self) -> list[str]:
        return list(self.merged().get("enabledModels", []))

    def get_session_dir(self) -> str | None:
        return self.merged().get("sessionDir")

    def get_theme(self) -> str:
        return self.merged().get("theme", "default")

    def get_quiet_startup(self) -> bool:
        return bool(self.merged().get("quietStartup", False))

    def get_steering_mode(self) -> str:
        return self.merged().get("steeringMode", "interrupt")

    def get_follow_up_mode(self) -> str:
        return self.merged().get("followUpMode", "queue")

    def get_transport(self) -> str:
        return self.merged().get("transport", "http")

    def get_thinking_budgets(self) -> dict[str, Any]:
        return self.merged().get("thinkingBudgets", {})

    def get_shell_command_prefix(self) -> str | None:
        return self.merged().get("shellCommandPrefix")

    def get_image_auto_resize(self) -> bool:
        return bool(self.merged().get("image", {}).get("autoResize", True))

    def get_block_images(self) -> bool:
        return bool(self.merged().get("image", {}).get("blockImages", False))

    def get_retry_settings(self) -> dict[str, Any]:
        return self.merged().get("retry", {})

    def get_retry_enabled(self) -> bool:
        return bool(self.get_retry_settings().get("enabled", True))

    def get_compaction_settings(self) -> dict[str, Any]:
        return self.merged().get("compaction", {})

    def get_compaction_threshold_percent(self) -> float:
        return float(self.get_compaction_settings().get("thresholdPercent", 85))

    def get_compaction_recent_tokens(self) -> int:
        return int(self.get_compaction_settings().get("recentTokens", 8192))

    def get_compaction_min_kept_messages(self) -> int:
        return int(self.get_compaction_settings().get("minKeptMessages", 20))

    def get_compaction_summarize_with_model(self) -> bool:
        return bool(self.get_compaction_settings().get("summarizeWithModel", True))

    def get_compaction_max_summary_input_tokens(self) -> int:
        return int(self.get_compaction_settings().get("maxSummaryInputTokens", 20000))

    def get_tool_settings(self) -> dict[str, Any]:
        return self.merged().get("tools", {})

    def get_tool_max_steps(self) -> int:
        return int(self.get_tool_settings().get("maxSteps", 6))

    def get_tool_timeout_sec(self) -> int:
        return int(self.get_tool_settings().get("timeoutSec", 30))

    def get_tool_approval(self) -> bool:
        return bool(self.get_tool_settings().get("approval", False))

    def get_tool_approval_tools(self) -> list[str]:
        return list(self.get_tool_settings().get("approvalTools", ["bash", "write", "edit"]))

    def get_subagents_max_concurrent(self) -> int:
        return int(self.merged().get("subagents", {}).get("maxConcurrent", 2))

    def get_subagents_max_depth(self) -> int:
        return int(self.merged().get("subagents", {}).get("maxDepth", 3))

    def get_subagents_enabled(self) -> bool:
        return bool(self.merged().get("subagents", {}).get("enabled", True))

    def set_subagents_enabled(self, enabled: bool, persist: bool = True) -> None:
        subagents = dict(self._global.get("subagents", {}))
        subagents["enabled"] = bool(enabled)
        self._global["subagents"] = subagents
        if persist:
            self._save_global()

    def get_ask_user_timeout_sec(self) -> int:
        try:
            return max(0, int((self.merged().get("askUser") or {}).get("timeoutSec", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def get_bash_show_output(self) -> bool:
        return bool(self.merged().get("bash", {}).get("showOutput", True))

    def set_bash_show_output(self, enabled: bool, persist: bool = True) -> None:
        bash = dict(self._global.get("bash", {}))
        bash["showOutput"] = bool(enabled)
        self._global["bash"] = bash
        if persist:
            self._save_global()

    def get_budget_settings(self) -> dict[str, Any]:
        return self.merged().get("budget", {}) or {}

    def get_budget_max_tokens(self) -> int:
        return int(self.get_budget_settings().get("maxTokens") or 0)

    def get_budget_max_time_sec(self) -> int:
        return int(self.get_budget_settings().get("maxTimeSec") or 0)

    def get_packages(self) -> list[str]:
        return list(self.merged().get("packages", []))

    def get_mcp_servers(self) -> dict[str, Any]:
        """MCP server config: {name: {command, args, env}} from settings.json."""
        return self.merged().get("mcpServers", {}) or {}

    def set_mcp_server_enabled(self, name: str, enabled: bool) -> None:
        """Persist the enabled flag for an MCP server config entry."""
        mcp = dict(self._global.get("mcpServers", {}) or {})
        entry = dict(mcp.get(name, {}) or {})
        entry["enabled"] = bool(enabled)
        mcp[name] = entry
        self._global["mcpServers"] = mcp
        self._save_global()

    def set_retry_enabled(self, enabled: bool) -> None:
        retry = self._global.get("retry", {})
        retry["enabled"] = enabled
        self._global["retry"] = retry
        self._save_global()

    def set_steering_mode(self, mode: str) -> None:
        self._global["steeringMode"] = mode
        self._save_global()

    def set_follow_up_mode(self, mode: str) -> None:
        self._global["followUpMode"] = mode
        self._save_global()

    def set_theme(self, theme: str) -> None:
        self._global["theme"] = theme
        self._save_global()

    def set_default_provider(self, provider: str | None) -> None:
        self._global["defaultProvider"] = provider
        self._save_global()

    def set_default_model(self, model: str | None) -> None:
        self._global["defaultModel"] = model
        self._save_global()

    def set_default_thinking_level(self, level: str) -> None:
        self._global["defaultThinkingLevel"] = level
        self._save_global()

    def set_tool_settings(self, max_steps: int | None = None, timeout_sec: int | None = None) -> None:
        tools = dict(self._global.get("tools", {}))
        if max_steps is not None:
            tools["maxSteps"] = int(max_steps)
        if timeout_sec is not None:
            tools["timeoutSec"] = int(timeout_sec)
        self._global["tools"] = tools
        self._save_global()

    def set_packages(self, packages: list[str]) -> None:
        self._global["packages"] = sorted(set(packages))
        self._save_global()

    def set_config_value(self, key: str, value: Any) -> None:
        # Dotted-path setter for simple CLI config edits.
        parts = [p for p in key.split(".") if p]
        if not parts:
            raise ValueError("Invalid config key")
        for part in parts:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part):
                raise ValueError(f"Invalid config key segment: {part}")
        cur: dict[str, Any] = self._global
        for part in parts[:-1]:
            if part not in cur or not isinstance(cur[part], dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = value
        self._save_global()

    @classmethod
    def create(cls, cwd: str | None = None, agent_dir: str | None = None) -> "SettingsManager":
        from pathlib import Path

        return cls(str(Path(cwd or Path.cwd()).resolve()), agent_dir or get_agent_dir())

    @classmethod
    def in_memory(cls, initial: dict[str, Any] | None = None) -> "SettingsManager":
        from pathlib import Path

        return cls(str(Path.cwd()), get_agent_dir(), in_memory=True, initial=initial)
