from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from one.config import get_agent_dir
from one.core.persistence import atomic_write_text, ensure_private_dir, ensure_private_file, load_json_text_safe

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
    "retry": {"mode": "on", "maxRetries": 3, "baseDelayMs": 1500, "maxDelayMs": 20000},
    "image": {"autoResize": True, "blockImages": False},
    "sessionDir": None,
    "theme": "default",
    "defaultMode": "tui",
    "quietStartup": False,
    "steeringMode": "interrupt",
    "followUpMode": "queue",
    "transport": "http",
    "thinkingBudgets": {},
    "shellCommandPrefix": None,
    "tools": {"maxSteps": 6, "timeoutSec": 30, "approval": False, "approvalTools": ["bash", "write", "edit", "plan", "apply_patch"]},
    "bash": {"showOutput": True},
    "subagents": {"enabled": True, "maxConcurrent": 2, "maxDepth": 3},
    "askUser": {"timeoutSec": 0},
    "providers": {"timeoutSec": 300},
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
        # Intentionally uses the *injected* agent_dir (may be custom/TEST)
        # rather than consulting ``get_agent_dir()`` — preserves test isolation
        # and allows CLI --agent-dir to control global settings path.
        self._global_path = Path(agent_dir) / "settings.json" if not in_memory else None
        self._project_path = Path(cwd) / ".one" / "settings.json" if not in_memory else None
        self._errors: list[dict[str, Any]] = []
        self._in_memory = in_memory
        # Guard: refuse to overwrite a known-malformed global settings file.
        self._global_is_locked: bool = False
        if in_memory:
            self._global = _deep_merge(DEFAULT_SETTINGS, initial or {})
            self._project = {}
        else:
            self._global, self._global_is_locked = self._load_global(self._global_path)
            self._project = self._load(self._project_path, "project")

    def _load(self, path: Path | None, scope: str) -> dict[str, Any]:
        if not path or not path.exists():
            return {}
        data, err = load_json_text_safe(path)
        if err is not None:
            self._errors.append({"scope": scope, "error": err})
            return {}
        if isinstance(data, dict):
            return data
        # Valid JSON but not a dict — record as error, return defaults.
        self._errors.append({"scope": scope, "error": ValueError(f"{scope} settings is not a JSON object")})
        return {}

    def _load_global(self, path: Path | None) -> tuple[dict[str, Any], bool]:
        """Load global settings and return (parsed, is_locked).

        ``is_locked`` is True when the file is malformed or non-dict, so
        that subsequent saves are refused.  Tightens file/dir to 0600/0700
        on POSIX for ALL branches (valid, malformed, non-dict).
        """
        if not path or not path.exists():
            return {}, False
        data, err = load_json_text_safe(path)
        # Always tighten existing global file/dir on POSIX.
        try:
            ensure_private_dir(path.parent)
            ensure_private_file(path, 0o600)
        except Exception:
            pass
        if err is not None:
            self._errors.append({"scope": "global", "error": err})
            return {}, True
        if isinstance(data, dict):
            return data, False
        # Valid JSON but not a dict — record and lock.
        self._errors.append(
            {"scope": "global", "error": ValueError("global settings is not a JSON object")}
        )
        return {}, True

    def _save_global(self) -> None:
        if self._in_memory or not self._global_path:
            return
        # Refuse to overwrite a known-malformed global file.
        if self._global_is_locked:
            raise RuntimeError(
                "global settings file is malformed or not a JSON object; "
                "repair it before the agent can persist config"
            )
        atomic_write_text(self._global_path, json.dumps(self._global, indent=2) + "\n")

    # --- Writability guard ---

    def _require_writable_global(self) -> None:
        """Raise before mutation if the global file is malformed."""
        if self._global_is_locked:
            raise RuntimeError(
                "global settings file is malformed or not a JSON object; "
                "repair it before the agent can persist config"
            )

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

    def get_default_mode(self) -> str:
        mode = str(self.merged().get("defaultMode") or "tui").lower()
        return mode if mode in {"tui", "cli"} else "tui"

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
        """Backward-compatible: True when retry mode is 'on' or 'unlimited'."""
        mode = self.get_retry_mode()
        return mode in ("on", "unlimited")

    def get_retry_mode(self) -> str:
        """Return the retry mode: 'off', 'on', or 'unlimited'."""
        return str(self.get_retry_settings().get("mode", "on"))

    def set_retry_mode(self, mode: str) -> None:
        """Persist a retry mode ('off', 'on', or 'unlimited')."""
        self._require_writable_global()
        retry = self._global.get("retry", {})
        if mode not in ("off", "on", "unlimited"):
            raise ValueError(f"Invalid retry mode: {mode}")
        retry["mode"] = mode
        self._global["retry"] = retry
        self._save_global()

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
        return list(self.get_tool_settings().get("approvalTools", ["bash", "write", "edit", "plan", "apply_patch"]))

    def get_subagents_max_concurrent(self) -> int:
        return int(self.merged().get("subagents", {}).get("maxConcurrent", 2))

    def get_subagents_max_depth(self) -> int:
        return int(self.merged().get("subagents", {}).get("maxDepth", 3))

    def get_subagents_enabled(self) -> bool:
        return bool(self.merged().get("subagents", {}).get("enabled", True))

    def set_subagents_enabled(self, enabled: bool, persist: bool = True) -> None:
        if persist:
            self._require_writable_global()
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

    def get_provider_timeout_sec(self) -> int:
        """Timeout for a single provider call (outer asyncio.wait_for backstop).

        Default 300 s. The SSE idle watchdog remains shorter and catches
        streams that stop producing data. Overridable via
        /config providers.timeoutSec.
        """
        try:
            return max(10, int((self.merged().get("providers") or {}).get("timeoutSec", 300) or 300))
        except (TypeError, ValueError):
            return 300

    def get_bash_show_output(self) -> bool:
        return bool(self.merged().get("bash", {}).get("showOutput", True))

    def set_bash_show_output(self, enabled: bool, persist: bool = True) -> None:
        if persist:
            self._require_writable_global()
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
        self._require_writable_global()
        mcp = dict(self._global.get("mcpServers", {}) or {})
        entry = dict(mcp.get(name, {}) or {})
        entry["enabled"] = bool(enabled)
        mcp[name] = entry
        self._global["mcpServers"] = mcp
        self._save_global()

    def set_retry_enabled(self, enabled: bool) -> None:
        """Backward-compatible: preserve 'unlimited' when enabling."""
        if enabled:
            current = self.get_retry_mode()
            if current != "unlimited":
                self.set_retry_mode("on")
        else:
            self.set_retry_mode("off")

    def set_steering_mode(self, mode: str) -> None:
        self._require_writable_global()
        self._global["steeringMode"] = mode
        self._save_global()

    def set_follow_up_mode(self, mode: str) -> None:
        self._require_writable_global()
        self._global["followUpMode"] = mode
        self._save_global()

    def set_theme(self, theme: str) -> None:
        self._require_writable_global()
        self._global["theme"] = theme
        self._save_global()

    def set_default_provider(self, provider: str | None) -> None:
        self._require_writable_global()
        self._global["defaultProvider"] = provider
        self._save_global()

    def set_default_model(self, model: str | None) -> None:
        self._require_writable_global()
        self._global["defaultModel"] = model
        self._save_global()

    def set_default_thinking_level(self, level: str) -> None:
        self._require_writable_global()
        self._global["defaultThinkingLevel"] = level
        self._save_global()

    def set_tool_settings(self, max_steps: int | None = None, timeout_sec: int | None = None) -> None:
        self._require_writable_global()
        tools = dict(self._global.get("tools", {}))
        if max_steps is not None:
            max_steps = int(max_steps)
            if max_steps < 0:
                raise ValueError(f"maxSteps must be >= 0, got {max_steps}")
            tools["maxSteps"] = max_steps
        if timeout_sec is not None:
            tools["timeoutSec"] = int(timeout_sec)
        self._global["tools"] = tools
        self._save_global()

    def set_packages(self, packages: list[str]) -> None:
        self._require_writable_global()
        self._global["packages"] = sorted(set(packages))
        self._save_global()

    def set_config_value(self, key: str, value: Any) -> None:
        # Dotted-path setter for simple CLI config edits.
        self._require_writable_global()
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
    def create(cls, cwd: str | None = None, agent_dir: str | None = None) -> SettingsManager:
        from pathlib import Path

        return cls(str(Path(cwd or Path.cwd()).resolve()), agent_dir or get_agent_dir())

    @classmethod
    def in_memory(cls, initial: dict[str, Any] | None = None) -> SettingsManager:
        from pathlib import Path

        return cls(str(Path.cwd()), get_agent_dir(), in_memory=True, initial=initial)
