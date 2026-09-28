from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.providers.base import ChatResult
from tests.support.agents import _Loader


class _UsageProvider:
    def __init__(self, usage: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.usage = usage if usage is not None else {}
        self.error = error

    async def chat(self, api_key: str, model: str, messages: list[dict[str, Any]], thinking_level: str, headers=None):
        if self.error is not None:
            raise self.error
        return ChatResult(text="answer", raw={}, usage=self.usage, stop_reason="stop")


def _agent(tmp_path: Path) -> AgentSession:
    auth = AuthStorage.in_memory()
    auth.set_runtime_api_key("openai", "test")
    registry = ModelRegistry.create(auth)
    model = registry.find("openai", "gpt-4.1")
    assert model is not None
    return AgentSession(
        SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry,
        _Loader(), model, "medium",
    )


def test_output_generation_measurement_survives_session_jsonl_round_trip(tmp_path: Path):
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    manager.append_message(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "answer"}],
            "usage": {"input": 0, "output": 25, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
            "_outputGeneration": {"durationSec": 2.5, "outputTokens": 25},
        }
    )
    assert manager.session_file is not None

    reopened = SessionManager.open(manager.session_file, str(tmp_path / "sessions"))
    restored = reopened.build_session_context()["messages"]
    assert restored[0]["_outputGeneration"] == {"durationSec": 2.5, "outputTokens": 25}
    reloaded_agent = AgentSession(
        reopened, SettingsManager.in_memory(), ModelRegistry.create(AuthStorage.in_memory()),
        _Loader(), None, "medium",
    )
    assert reloaded_agent.get_session_stats()["time"]["activeGenerationSec"] == 2.5


def test_time_stats_use_durable_session_age_and_tolerate_clock_anomalies(tmp_path: Path):
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    manager.get_header()["timestamp"] = "2026-09-28T00:00:00Z"
    manager.append_message({"role": "user", "content": "persist the header"})
    assert manager.session_file is not None
    agent = AgentSession(
        SessionManager.open(manager.session_file, str(tmp_path / "sessions")),
        SettingsManager.in_memory(), ModelRegistry.create(AuthStorage.in_memory()), _Loader(), None, "medium",
    )
    agent._wall_clock = lambda: 1_790_553_610.0
    assert agent.get_session_stats()["time"]["sessionAgeSec"] == 10.0

    agent._wall_clock = lambda: 1.0
    assert agent.get_session_stats()["time"]["sessionAgeSec"] == 0.0
    agent.session_manager.get_header()["timestamp"] = "not-a-timestamp"
    assert agent.get_session_stats()["time"]["sessionAgeSec"] is None


@pytest.mark.asyncio
async def test_output_generation_rate_uses_provider_usage_and_active_duration(tmp_path: Path):
    agent = _agent(tmp_path)
    agent.providers = {"openai": _UsageProvider({"completion_tokens": 25})}
    clock = iter((10.0, 12.5))
    agent._generation_clock = lambda: next(clock)

    agent.messages.append(await agent._invoke_provider([], allow_live_stream=False))
    generation = agent.get_session_stats()["outputGeneration"]
    assert generation == {
        "tokensPerSecond": 10.0, "outputTokens": 25, "durationSec": 2.5,
        "measurements": 1, "missingMeasurements": 0, "reliable": True,
    }


@pytest.mark.asyncio
async def test_time_stats_average_normal_responses_and_exclude_non_normal_calls(tmp_path: Path):
    agent = _agent(tmp_path)
    agent.providers = {"openai": _UsageProvider({"completion_tokens": 1})}
    clock = iter((10.0, 12.0, 20.0, 25.0))
    agent._generation_clock = lambda: next(clock)

    agent.messages.append(await agent._invoke_provider([], allow_live_stream=False))
    agent.messages.append(await agent._invoke_provider([], allow_live_stream=False))
    # Nudge, repair, compaction, and retry paths pass this flag as False.
    agent.messages.append(
        await agent._invoke_provider([], allow_live_stream=False, count_output_generation=False)
    )

    timing = agent.get_session_stats()["time"]
    assert timing["activeGenerationSec"] == 7.0
    assert timing["averageResponseSec"] == 3.5
    assert timing["lastResponseSec"] == 5.0
    assert timing["responseMeasurements"] == 2


@pytest.mark.asyncio
async def test_compaction_time_persists_and_old_entries_are_ignored(tmp_path: Path):
    session_dir = tmp_path / "sessions"
    manager = SessionManager.create(str(tmp_path), str(session_dir))
    agent = AgentSession(
        manager, SettingsManager.in_memory({"compaction": {"summarizeWithModel": False}}),
        ModelRegistry.create(AuthStorage.in_memory()), _Loader(), None, "medium",
    )
    for i in range(2):
        manager.append_message({"role": "user", "content": f"question {i}"})
        manager.append_message({"role": "assistant", "content": f"answer {i}"})
    agent.messages = manager.build_session_context()["messages"]
    clock = iter((30.0, 32.5))
    agent._compaction_clock = lambda: next(clock)

    assert (await agent.compact())["skipped"] is False
    persisted = manager.get_last_compaction()
    assert persisted is not None and persisted["durationMs"] == 2500.0
    assert manager.session_file is not None

    reopened = SessionManager.open(manager.session_file, str(session_dir))
    reopened.append_compaction("old", "root", tokens_before=0)
    restored = AgentSession(
        reopened, SettingsManager.in_memory(), ModelRegistry.create(AuthStorage.in_memory()),
        _Loader(), None, "medium",
    )
    timing = restored.get_session_stats()["time"]
    assert timing["compactionSec"] == 2.5
    assert timing["averageCompactionSec"] == 2.5
    assert timing["lastCompactionSec"] is None


def test_time_stats_clamp_negative_and_zero_durations(tmp_path: Path):
    agent = _agent(tmp_path)
    agent.messages = [
        {"role": "assistant", "_outputGeneration": {"durationSec": -1, "outputTokens": 1}},
        {"role": "assistant", "_outputGeneration": {"durationSec": 0, "outputTokens": 1}},
    ]
    timing = agent.get_session_stats()["time"]
    assert timing["activeGenerationSec"] == 0.0
    assert timing["averageResponseSec"] == 0.0
    assert timing["lastResponseSec"] == 0.0
    assert timing["responseMeasurements"] == 2


@pytest.mark.asyncio
async def test_output_generation_rate_is_null_for_missing_usage_failure_or_zero_duration(tmp_path: Path):
    agent = _agent(tmp_path)
    agent.providers = {"openai": _UsageProvider()}
    clock = iter((5.0, 6.0))
    agent._generation_clock = lambda: next(clock)
    agent.messages.append(await agent._invoke_provider([], allow_live_stream=False))
    generation = agent.get_session_stats()["outputGeneration"]
    assert generation["tokensPerSecond"] is None
    assert generation["missingMeasurements"] == 1
    assert generation["reliable"] is False

    agent = _agent(tmp_path)
    agent.providers = {"openai": _UsageProvider({"completion_tokens": 1})}
    clock = iter((5.0, 5.0))
    agent._generation_clock = lambda: next(clock)
    agent.messages.append(await agent._invoke_provider([], allow_live_stream=False))
    assert agent.get_session_stats()["outputGeneration"]["tokensPerSecond"] is None

    agent = _agent(tmp_path)
    agent.providers = {"openai": _UsageProvider(error=RuntimeError("failed"))}
    with pytest.raises(RuntimeError, match="failed"):
        await agent._invoke_provider([], allow_live_stream=False)
    assert agent.get_session_stats()["outputGeneration"]["tokensPerSecond"] is None
