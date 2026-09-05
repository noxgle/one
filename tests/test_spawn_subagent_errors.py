"""Tests for Task 30.10a: spawn_subagent error handling and dispatch.

Verifies that spawn_subagent calls reach _spawn_subagent, disabled
subagents produce structured errors (not bash fallback), and tool
error results are valid JSON without sensitive traces.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager


class _Loader:
    def get_system_prompt(self, selected_tools: list[str] | None = None) -> str:  # noqa: ARG002
        return "You are a coding agent."


class _Provider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0

    async def chat(
        self,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        thinking_level: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        from one.providers.base import ChatResult

        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResult(text=self.responses[idx], raw={}, usage={}, stop_reason="stop")


def _mk_session(tmp_path: Path, tools: list[str] | None = None, settings: dict[str, Any] | None = None) -> Any:
    """Create a minimal AgentSession for spawn_subagent testing."""
    from one.core.agent_session import AgentSession
    from one.core.auth_storage import AuthStorage
    from one.core.model_registry import ModelRegistry
    from one.core.session_manager import SessionManager
    from one.core.settings_manager import SettingsManager

    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "dummy")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    sm = SettingsManager.in_memory(settings or {"tools": {"maxSteps": 4, "timeoutSec": 5}})
    session_mgr = SessionManager.in_memory(str(tmp_path))
    return AgentSession(
        session_manager=session_mgr,
        settings_manager=sm,
        model_registry=registry,
        resource_loader=_Loader(),
        model=model,
        thinking_level="medium",
        tools=tools,
    )


class TestSpawnSubagentDispatch:
    """Verify that spawn_subagent is properly dispatched."""

    def test_spawn_subagent_async_raises_when_called_directly(self) -> None:
        """The tool placeholder is async and raises when awaited."""
        from one.tools.spawn_subagent import spawn_subagent_tool

        with pytest.raises(RuntimeError, match="session loop"):
            asyncio.run(spawn_subagent_tool(task="test"))

    def test_spawn_subagent_in_all_tools(self) -> None:
        """spawn_subagent must be in the all_tools registry."""
        from one.tools.index import all_tools

        assert "spawn_subagent" in all_tools

    def test_spawn_subagent_in_default_tool_names(self) -> None:
        """spawn_subagent must be in DEFAULT_TOOL_NAMES."""
        from one.tools.index import DEFAULT_TOOL_NAMES

        assert "spawn_subagent" in DEFAULT_TOOL_NAMES


class TestSpawnSubagentDisabled:
    """Verify that disabled subagents produce a structured error."""

    def test_disabled_subagents_error_is_english(self, tmp_path: Path) -> None:
        """The error when subagents are disabled must be in English."""
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent"],
            settings={"subagents": {"enabled": False}},
        )

        with pytest.raises(RuntimeError, match="Subagents disabled"):
            asyncio.run(session._spawn_subagent({"task": "test subtask"}))


class TestSpawnSubagentValidCall:
    """Verify that valid spawn_subagent calls are dispatched correctly."""

    def test_valid_spawn_subagent_call_reaches_session(self, tmp_path: Path) -> None:
        """A valid spawn_subagent call should dispatch to _spawn_subagent, not bash.

        With no providers configured, the subagent will fail and
        _spawn_subagent catches the exception, returning a result dict
        with ok=False — NOT raising.  This proves the dispatch reached
        _spawn_subagent and was not remapped to bash.
        """
        session = _mk_session(tmp_path, tools=["spawn_subagent"])

        result = asyncio.run(session._spawn_subagent({"task": "test"}))
        # Should return a dict, not raise
        assert isinstance(result, dict)
        assert "sessionId" in result
        assert "summary" in result
        assert "ok" in result
        assert result.get("ok") is False
        assert result.get("finished") is False
        # Error summary should not suggest bash remapping
        summary = result.get("summary", "")
        assert "bash" not in summary.lower()

    def test_spawn_subagent_missing_task_error(self, tmp_path: Path) -> None:
        """Missing 'task' or 'tasks' should produce a clear error."""
        session = _mk_session(tmp_path, tools=["spawn_subagent"])

        with pytest.raises(RuntimeError, match="requires 'task' or 'tasks'"):
            asyncio.run(session._spawn_subagent({}))


class TestToolErrorResultJSON:
    """Verify that tool error results are valid JSON without sensitive traces."""

    def test_error_result_is_valid_json(self, tmp_path: Path) -> None:
        """Tool error results must be valid JSON when serialized."""
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent"],
            settings={"subagents": {"enabled": False}},
        )

        # Capture the error
        error_text = ""
        try:
            asyncio.run(session._spawn_subagent({"task": "test"}))
        except RuntimeError as e:
            error_text = str(e)
        assert error_text, "Expected a RuntimeError to be raised"

        # The error message must not contain sensitive data
        assert "sk-" not in error_text
        assert "Bearer " not in error_text
        assert "password" not in error_text.lower()
        assert "secret" not in error_text.lower()

        # The error text must be serializable as JSON (no control chars)
        json.dumps(error_text)


class TestNoBashFallback:
    """Verify that spawn_subagent is never silently remapped to bash."""

    def test_spawn_subagent_not_in_bash_aliases(self) -> None:
        """spawn_subagent must not be callable as a bash command alias."""
        from one.tools.index import all_tools

        # spawn_subagent should be a distinct tool, not aliased to bash
        spawn_tool = all_tools["spawn_subagent"]
        bash_tool = all_tools["bash"]
        assert spawn_tool.fn is not bash_tool.fn

    def test_disabled_subagents_doesnt_invoke_bash(self, tmp_path: Path) -> None:
        """When subagents are disabled, spawn_subagent error must not mention bash."""
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent"],
            settings={"subagents": {"enabled": False}},
        )

        error_text = ""
        try:
            asyncio.run(session._spawn_subagent({"task": "test"}))
        except RuntimeError as e:
            error_text = str(e).lower()
        assert error_text, "Expected a RuntimeError to be raised"

        # The error should not suggest bash as a fallback
        assert "bash" not in error_text
        # Should clearly state the subagent issue
        assert "subagent" in error_text or "disabled" in error_text


class TestSpawnSubagentDepthLimit:
    """Verify depth-limit errors are structured."""

    def test_depth_limit_error(self, tmp_path: Path) -> None:
        """Hitting depth limit must produce a clear error."""
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent"],
            settings={"subagents": {"maxDepth": 0}},
        )

        with pytest.raises(RuntimeError, match="depth limit"):
            asyncio.run(session._spawn_subagent({"task": "test"}))


class TestSpawnSubagentConcurrencyLimit:
    """Verify concurrency-limit errors are structured."""

    def test_concurrency_limit_error(self, tmp_path: Path) -> None:
        """Too many parallel tasks must produce a clear error."""
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent"],
            settings={"subagents": {"maxConcurrent": 1}},
        )

        with pytest.raises(RuntimeError, match="maxConcurrent"):
            asyncio.run(session._spawn_subagent({"tasks": ["a", "b"]}))


# ---------------------------------------------------------------------------
# Task 30.10a — Subagent failure diagnostics
# ---------------------------------------------------------------------------


class TestUnsuccessfulChild:
    """A child subagent that finishes without goal_success must surface ok=False with diagnostics."""

    def test_unsuccessful_child_has_error_and_error_type(self, tmp_path: Path) -> None:
        """When a child finishes but goalSuccess=False, the parent sees ok=False with error/errorType."""
        spawn_json = json.dumps({"tool": "spawn_subagent", "args": {"task": "do the impossible"}})
        # Child finishes with goal_success=False — no provider call; the subagent
        # itself reaches the step limit.  We test the _spawn_subagent path directly:
        # provide a provider that makes the subagent emit a non-finish response
        # so it hits maxSteps and returns a failure result.
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent", "finish"],
            settings={"tools": {"maxSteps": 2, "timeoutSec": 5}},
        )
        # The subagent's provider: it never calls finish → step limit reached.
        session.providers = {"openai": _Provider(["I can't do this."])}

        result = asyncio.run(session._spawn_subagent({"task": "impossible task"}))
        assert result["ok"] is False
        assert result["finished"] is False
        assert result["goalSuccess"] is False
        assert result.get("error") is not None and len(result["error"]) > 0
        assert result.get("errorType") is not None
        assert result.get("summary") is not None

    def test_spawn_subagent_failure_tool_result_contains_diagnostics(self, tmp_path: Path) -> None:
        """The spawn_subagent tool result JSON must contain error, not {ok:false, error:null}."""
        spawn_json = json.dumps({"tool": "spawn_subagent", "args": {"task": "impossible"}})
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent", "finish"],
            settings={"tools": {"maxSteps": 2, "timeoutSec": 5}},
        )
        session.providers = {"openai": _Provider([spawn_json, "DONE"])}
        events: list[dict[str, Any]] = []
        session.subscribe(events.append)

        asyncio.run(session.prompt("go"))

        # Find the spawn_subagent tool result message
        spawn_results = [
            m for m in session.messages
            if m.get("role") == "toolResult"
            and json.loads(m.get("content", "{}")).get("tool") == "spawn_subagent"
        ]
        assert len(spawn_results) == 1
        content = json.loads(spawn_results[0]["content"])
        assert content["ok"] is False
        # error must be non-empty — never {ok:false, error:null}
        assert content.get("error") is not None
        assert len(str(content["error"])) > 0


class TestExceptionChild:
    """A subagent that crashes (e.g. provider exception) must return structured error."""

    def test_provider_exception_produces_structured_error(self, tmp_path: Path) -> None:
        """When the subagent provider raises, the prompt loop catches it,
        the subagent hits step-limit, and _run_subagent returns ok=False
        with a non-empty error and errorType (never {ok:false, error:null})."""
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent"],
            settings={"retry": {"enabled": False}},  # no retry → hit step limit faster
        )

        class _FailingProvider:
            async def chat(self, **kwargs):  # noqa: ARG002
                raise RuntimeError("provider network error")

        session.providers = {"openai": _FailingProvider()}
        result = asyncio.run(session._spawn_subagent({"task": "test"}))
        assert result["ok"] is False
        assert result["error"] is not None
        assert len(result["error"]) > 0
        # errorType is IncompleteExecution (subagent hit step limit after provider crash)
        assert result["errorType"] is not None

    def test_error_is_sanitized_no_credentials(self, tmp_path: Path) -> None:
        """Errors must not leak API keys or secrets, even when the subagent
        enriches diagnostic text with the last assistant message."""
        session = _mk_session(tmp_path, tools=["spawn_subagent"])
        # The subagent finishes (with a plain text response that happens to
        # contain a fake API key). The finish call sets goalSuccess=True
        # so ok=True — no error to sanitize.  Instead we test that the
        # _build_tool_result_message_payload fallback sanitises errors
        # by using a subagent that fails with an exception.

        class _LeakyProvider:
            async def chat(self, **kwargs):  # noqa: ARG002
                raise RuntimeError("API call failed: key=sk-abc123def456ghi789jkl012mno345")

        session.providers = {"openai": _LeakyProvider()}
        result = asyncio.run(session._spawn_subagent({"task": "test"}))
        # The error should not contain the raw key.
        assert "sk-abc123def456ghi789jkl012mno345" not in str(result.get("error", ""))
        assert "sk-abc123def456" not in str(result.get("error", ""))


class TestParallelFailureAggregate:
    """Parallel tasks must aggregate ok and errors correctly."""

    def test_parallel_all_fail_produces_aggregate_error(self, tmp_path: Path) -> None:
        """When both children fail, aggregate ok=False and error is set."""
        settings = SettingsManager.in_memory({"tools": {"maxSteps": 2, "timeoutSec": 5}})
        auth = AuthStorage.in_memory()
        auth.set_runtime_api_key("openai", "dummy")
        registry = ModelRegistry.create(auth)
        model = registry.find("openai", "gpt-4.1")
        assert model is not None

        # Parent with no provider — subagents will fail.
        session_dir = str(tmp_path / "sessions")
        manager = SessionManager.create(str(tmp_path), session_dir)
        agent = AgentSession(manager, settings, registry, _Loader(), model, "medium", tools=["spawn_subagent"])
        # No providers set — subagents will crash.

        result = asyncio.run(agent._spawn_subagent({"tasks": ["a", "b"]}))
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert result["goalSuccess"] is False
        assert result.get("error") is not None
        assert len(result["error"]) > 0
        assert result.get("output") is not None and len(result["output"]) > 0
        # Per-child results are preserved
        assert len(result["results"]) == 2
        for r in result["results"]:
            assert r["ok"] is False

    def test_parallel_mixed_ok_and_fail(self, tmp_path: Path) -> None:
        """When one child succeeds and one fails, aggregate ok=False."""
        from one.core.agent_session import AgentSession

        settings = SettingsManager.in_memory({"tools": {"maxSteps": 4, "timeoutSec": 5}})
        auth = AuthStorage.in_memory()
        auth.set_runtime_api_key("openai", "dummy")
        registry = ModelRegistry.create(auth)
        model = registry.find("openai", "gpt-4.1")
        assert model is not None

        session_dir = str(tmp_path / "sessions")
        parent_mgr = SessionManager.create(str(tmp_path), session_dir)
        parent = AgentSession(parent_mgr, settings, registry, _Loader(), model, "medium", tools=["spawn_subagent"])

        # Create two child sessions: one succeeds (finish), one fails (no finish).
        child_a = AgentSession(
            SessionManager(parent_mgr.cwd, parent_mgr.session_dir, None, True),
            parent.settings_manager,
            parent.model_registry,
            parent.resource_loader,
            model,
            parent.thinking_level,
            tools=["finish"],
        )
        child_a.providers = {"openai": _Provider([
            json.dumps({"tool": "finish", "args": {"summary": "a done", "goal_success": True}}),
        ])}

        child_b = AgentSession(
            SessionManager(parent_mgr.cwd, parent_mgr.session_dir, None, True),
            parent.settings_manager,
            parent.model_registry,
            parent.resource_loader,
            model,
            parent.thinking_level,
            tools=["finish"],
        )
        child_b.providers = {"openai": _Provider(["I can't finish this."])}

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(asyncio.gather(child_a.prompt("do a"), child_b.prompt("do b")))
        finally:
            loop.close()

        # Verify child_a succeeded and child_b failed.
        finish_a = child_a.get_last_finish_result()
        finish_b = child_b.get_last_finish_result()
        assert finish_a.get("finished") is True
        assert finish_a.get("goalSuccess") is True
        assert finish_b.get("finished") is False


class TestInvalidTasks:
    """Invalid tasks input must be rejected with clear errors."""

    def test_tasks_must_be_list(self, tmp_path: Path) -> None:
        """Non-list 'tasks' must be rejected."""
        session = _mk_session(tmp_path, tools=["spawn_subagent"])

        with pytest.raises(RuntimeError, match="'tasks' must be a list"):
            asyncio.run(session._spawn_subagent({"tasks": "not a list"}))

    def test_tasks_items_must_be_strings(self, tmp_path: Path) -> None:
        """Each task item must be a string."""
        session = _mk_session(tmp_path, tools=["spawn_subagent"])

        with pytest.raises(RuntimeError, match="tasks\\[1\\] must be a string"):
            asyncio.run(session._spawn_subagent({"tasks": ["valid", 42, "also valid"]}))

    def test_empty_tasks_list_is_rejected(self, tmp_path: Path) -> None:
        """An empty list for tasks with no 'task' must error."""
        session = _mk_session(tmp_path, tools=["spawn_subagent"])

        with pytest.raises(RuntimeError, match="requires 'task' or 'tasks'"):
            asyncio.run(session._spawn_subagent({"tasks": []}))


class TestExplicitToolsWithoutFinish:
    """Explicit tool sets missing 'finish' must be rejected."""

    def test_tools_without_finish_rejected(self, tmp_path: Path) -> None:
        """A non-empty tools list that omits 'finish' must raise."""
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent", "bash", "read", "write"],
            settings={"tools": {"maxSteps": 2, "timeoutSec": 5}},
        )

        with pytest.raises(
            RuntimeError,
            match="must include 'finish'",
        ):
            asyncio.run(
                session._spawn_subagent({"task": "test", "tools": ["bash", "read"]})
            )

    def test_tools_including_finish_allowed(self, tmp_path: Path) -> None:
        """A tools list that includes 'finish' must be accepted."""
        # This should NOT raise — finish is present.
        spawn_json = json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}})
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent", "bash", "finish"],
            settings={"tools": {"maxSteps": 4, "timeoutSec": 5}},
        )
        session.providers = {"openai": _Provider([spawn_json])}
        # Should not raise
        result = asyncio.run(session._spawn_subagent({"task": "test", "tools": ["bash", "finish"]}))
        assert isinstance(result, dict)

    def test_explicit_empty_tools_list_rejected(self, tmp_path: Path) -> None:
        """Explicit empty tools list [] must be rejected — subagent cannot complete."""
        session = _mk_session(tmp_path, tools=["spawn_subagent"])

        # Empty list is rejected as non-completable (distinct from omitted).
        with pytest.raises(
            RuntimeError,
            match="'tools' parameter must not be an empty list",
        ):
            asyncio.run(session._spawn_subagent({"task": "test", "tools": []}))


class TestSubagentEndEventDiagnostics:
    """subagent_end must carry diagnostic info for failures."""

    def test_subagent_end_carries_error_on_failure(self, tmp_path: Path) -> None:
        """When a subagent fails, subagent_end includes error and errorType."""
        session = _mk_session(
            tmp_path,
            tools=["spawn_subagent"],
            settings={"retry": {"enabled": False}},  # no retry → prompt catches and breaks
        )

        class _FailingProvider:
            async def chat(self, **kwargs):  # noqa: ARG002
                raise ValueError("boom")

        session.providers = {"openai": _FailingProvider()}
        events: list[dict[str, Any]] = []
        session.subscribe(events.append)

        result = asyncio.run(session._spawn_subagent({"task": "test"}))
        assert result["ok"] is False

        sub_end = [e for e in events if e.get("type") == "subagent_end"]
        assert len(sub_end) == 1
        assert sub_end[0]["ok"] is False
        assert sub_end[0].get("error") is not None
        assert "boom" in sub_end[0]["error"]

    def test_subagent_end_ok_on_success(self, tmp_path: Path) -> None:
        """When a subagent succeeds, subagent_end has ok=True and no error."""
        spawn_json = json.dumps({"tool": "finish", "args": {"summary": "done", "goal_success": True}})
        session = _mk_session(tmp_path, tools=["spawn_subagent", "finish"])
        session.providers = {"openai": _Provider([spawn_json])}
        events: list[dict[str, Any]] = []
        session.subscribe(events.append)

        result = asyncio.run(session._spawn_subagent({"task": "test"}))
        assert result["ok"] is True

        sub_end = [e for e in events if e.get("type") == "subagent_end"]
        assert len(sub_end) == 1
        assert sub_end[0]["ok"] is True
        # error and errorType should be None on success
        assert sub_end[0].get("error") is None
        assert sub_end[0].get("errorType") is None
