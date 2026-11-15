"""Offline handoff isolation and transactional replacement tests."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from one.core.agent_session_runtime import AgentSessionRuntimeHost, parse_handoff
from one.core.tui_runtime_host import TuiRuntimeHost
from tests.support.tui import _mk_app_session


def make_host(tmp_path):
    session = _mk_app_session(tmp_path)
    bootstrap = {
        "agentDir": str(tmp_path / "agent"), "settingsManager": session.settings_manager,
        "modelRegistry": session.model_registry, "model": session.model,
        "resourceLoaderAlreadyLoaded": True, "tools": session._base_tools,
    }
    runtime = SimpleNamespace(session=session, session_manager=session.session_manager,
                              resource_loader=session.resource_loader)
    return AgentSessionRuntimeHost(bootstrap, runtime)


@pytest.mark.asyncio
async def test_handoff_isolated_user_brief_and_effective_preset(tmp_path):
    host = make_host(tmp_path)
    source = host.session
    source.activate_preset("readonly", str(tmp_path / "agent"))
    source.session_manager.append_message({"role": "user", "content": "PRIVATE HISTORY"})
    source.session_manager.append_custom("native", {"opaque": "PRIVATE NATIVE"})
    original = deepcopy(source.session_manager.get_entries())
    expected = source.preset_effective_settings
    result = await host.handoff("Finish tests", "Only work on parser")
    try:
        assert host.session is not source
        assert host.session.session_manager.get_header()["id"] != source.session_manager.get_header()["id"]
        assert source.session_manager.get_entries() == original
        assert host.session.preset_id == "readonly"
        assert host.session.preset_effective_settings == expected
        assert host.session.active_tools == source.active_tools
        assert len(host.session.messages) == 1
        text = str(host.session.messages)
        assert "Finish tests" in text and "Only work on parser" in text
        assert "PRIVATE" not in str(host.session.session_manager.get_entries())
        assert result["briefSource"] == "user"
        provenance = next(e for e in host.session.session_manager.get_entries() if e.get("customType") == "handoff")
        assert provenance["data"]["sourceSessionId"] == source.session_manager.get_header()["id"]
        assert provenance["data"]["nativeStateTransferred"] is False
    finally:
        await host.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_handoff_failed_construction_keeps_source(tmp_path, monkeypatch, cancel):
    from one.core import agent_session_runtime as module
    from one.core.session_manager import SessionManager

    host = make_host(tmp_path)
    # Also exercise cleanup of a persistent candidate.
    host._runtime.session_manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    source = host.session
    before = deepcopy(source.session_manager.get_entries())

    async def fail(*args, **kwargs):
        raise asyncio.CancelledError() if cancel else ValueError("construction failed")

    monkeypatch.setattr(module, "create_agent_session_runtime", fail)
    with pytest.raises(asyncio.CancelledError if cancel else ValueError):
        await host.handoff("goal", "brief")
    assert host.session is source
    assert source.session_manager.get_entries() == before
    assert not list((tmp_path / "sessions").glob("*.jsonl"))
    await host.dispose()


@pytest.mark.asyncio
async def test_handoff_cancel_after_commit_reports_success(tmp_path, monkeypatch):
    host = make_host(tmp_path)
    source = host.session

    async def cancelled_dispose():
        raise asyncio.CancelledError()

    monkeypatch.setattr(source, "dispose", cancelled_dispose)
    result = await host.handoff("goal", "brief")
    assert result["cancelled"] is False
    assert host.session is not source
    await host.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["_is_streaming", "_is_compacting"])
async def test_handoff_busy_rejected(tmp_path, field):
    host = make_host(tmp_path)
    source = host.session
    setattr(source, field, True)
    with pytest.raises(ValueError, match="idle"):
        await host.handoff("goal", "brief")
    assert host.session is source
    setattr(source, field, False)
    await host.dispose()


@pytest.mark.parametrize("text", ["", "goal", "goal | ", " | brief", "x" * 2001 + " | brief", "g | " + "x" * 12001])
def test_handoff_validation(text):
    with pytest.raises(ValueError):
        parse_handoff(text)


def test_handoff_parse_preserves_remaining_delimiters():
    assert parse_handoff(" goal | brief | more ") == ("goal", "brief | more")


@pytest.mark.asyncio
async def test_handoff_host_epoch_and_rebinding(tmp_path):
    host = make_host(tmp_path)
    tui = TuiRuntimeHost(host, None)
    source = host.session
    with pytest.raises(ValueError, match="Session changed"):
        await tui.dispatch("handoff", {"goal": "goal", "brief": "brief", "epoch": -1})
    assert host.session is source
    result = await tui.dispatch("handoff", {"goal": "goal", "brief": "brief", "epoch": 0})
    assert result["epoch"] == 1
    assert host.session is not source
    assert len(result["messages"]) == 1
    await host.dispose()


@pytest.mark.asyncio
async def test_handoff_default_preserves_manual_policy_and_model(tmp_path):
    host = make_host(tmp_path)
    source = host.session
    source.temperature = 0.4
    result = await host.handoff("goal", "brief")
    assert result["sessionId"]
    assert host.session.model == source.model
    assert host.session.temperature == 0.4
    assert host.session.active_tools == source.active_tools
    await host.dispose()


@pytest.mark.asyncio
async def test_handoff_became_busy_during_construction(tmp_path, monkeypatch):
    from one.core import agent_session_runtime as module

    host = make_host(tmp_path)
    source = host.session
    original = module.create_agent_session_runtime

    async def build(*args, **kwargs):
        runtime = await original(*args, **kwargs)
        source._is_streaming = True
        return runtime

    monkeypatch.setattr(module, "create_agent_session_runtime", build)
    with pytest.raises(ValueError, match="busy"):
        await host.handoff("goal", "brief")
    assert host.session is source
    source._is_streaming = False
    await host.dispose()
