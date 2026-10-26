# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from one.core.agent_runtime_facade import AgentRuntimeFacade
from one.core.agent_session import AgentSession
from one.core.auth_storage import AuthStorage
from one.core.model_registry import ModelRegistry
from one.core.session_manager import SessionManager
from one.core.settings_manager import SettingsManager
from one.core.threaded_agent_runtime import RuntimeBootstrapSpec
from one.core.tui_runtime_host import TUI_COMMANDS, TuiRuntimeHost
from one.core.types import ModelInfo
from one.modes.tui_mode import TuiSessionView, _OneTextualApp
from tests.support.tui import _mk_app_session


def _models(count: int = 20) -> list[ModelInfo]:
    return [
        ModelInfo(
            "provider-a" if index % 2 else "provider-b",
            f"model-{index:02d}",
            context_window=1000 + index,
            reasoning=index % 2 == 0,
            input_image=index % 3 == 0,
        )
        for index in range(count)
    ]


async def _open_picker(app: _OneTextualApp, pilot):
    app.action_show_model_picker()
    await pilot.pause()
    picker = app.screen_stack[-1]
    assert picker.__class__.__name__ == "_ModelPicker"
    return picker


@pytest.mark.asyncio
async def test_model_picker_search_provider_navigation_and_details(tmp_path: Path) -> None:
    session = _mk_app_session(tmp_path, runtime_key="dummy")
    app = _OneTextualApp(session)
    with patch.object(session.model_registry, "get_available", return_value=_models()):
        async with app.run_test(size=(80, 24)) as pilot:
            picker = await _open_picker(app, pilot)
            search = picker.query_one("#model-picker-search")
            listing = picker.query_one("#model-picker-list")
            await pilot.press("pagedown")
            await pilot.pause()
            assert listing.highlighted is not None and listing.highlighted > 0
            await pilot.press("pageup")
            await pilot.press("down")
            await pilot.pause()
            assert listing.highlighted == 1
            await pilot.click("#model-picker-provider")
            await pilot.press("p")
            await pilot.press("enter")
            await pilot.pause()
            assert picker._filtered and {model.provider for model in picker._filtered} == {"provider-a"}
            search.focus()
            await pilot.press("ctrl+a")
            await pilot.press("backspace")
            await pilot.press("m", "o", "d", "e", "l", "-", "1", "9")
            await pilot.pause()
            assert [model.id for model in picker._filtered] == ["model-19"]
            assert "provider-a/model-19" in str(picker.query_one("#model-picker-details").content)


@pytest.mark.asyncio
async def test_model_picker_small_layout_and_cancel(tmp_path: Path) -> None:
    session = _mk_app_session(tmp_path, runtime_key="dummy")
    app = _OneTextualApp(session)
    with patch.object(session.model_registry, "get_available", return_value=_models()):
        async with app.run_test(size=(60, 20)) as pilot:
            picker = await _open_picker(app, pilot)
            assert picker.size.width <= 60
            assert picker.size.height <= 20
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (60, 20)])
async def test_model_picker_action_buttons_are_visible_and_clickable(tmp_path: Path, size: tuple[int, int]) -> None:
    """Compact terminals retain real, labelled button regions instead of a one-line bar."""
    from textual.widgets import Button

    session = _mk_app_session(tmp_path, runtime_key="dummy")
    app = _OneTextualApp(session)
    with patch.object(session.model_registry, "get_available", return_value=[ModelInfo("provider-a", "selected")]):
        async with app.run_test(size=size) as pilot:
            picker = await _open_picker(app, pilot)
            button = picker.query_one("#model-picker-session", Button)
            # Textual's content size excludes the border; the actual clickable
            # screen region must include the three-line bordered button.
            assert button.region.height >= 3
            assert "Session" in str(button.label) and "Enter" in str(button.label)
            assert button.region.y + button.region.height <= size[1]
            await pilot.click("#model-picker-session")
            await pilot.pause()
            assert (session.model.provider, session.model.id) == ("provider-a", "selected")
            assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_model_picker_details_preserve_known_false_and_unknown_capabilities(tmp_path: Path) -> None:
    session = _mk_app_session(tmp_path, runtime_key="dummy")
    app = _OneTextualApp(session)
    models = [
        ModelInfo("openai", "known", reasoning=True, input_image=True, context_window=200_000),
        ModelInfo("openai", "false", reasoning=False, input_image=False),
        ModelInfo("chatgpt", "gpt-5.6-sol", reasoning=True, input_image=True),
    ]
    with patch.object(session.model_registry, "get_available", return_value=models):
        async with app.run_test() as pilot:
            picker = await _open_picker(app, pilot)
            details = picker.query_one("#model-picker-details")
            assert "Context: 200000" in str(details.content)
            assert "Reasoning: yes" in str(details.content)
            assert "Vision: yes" in str(details.content)
            picker.query_one("#model-picker-list").highlighted = 1
            await pilot.pause()
            assert "Reasoning: no" in str(details.content) and "Vision: no" in str(details.content)
            picker.query_one("#model-picker-list").highlighted = 2
            await pilot.pause()
            assert "Context: unknown (no registry limit)" in str(details.content)
            assert "Reasoning: yes" in str(details.content) and "Vision: yes" in str(details.content)


@pytest.mark.asyncio
async def test_model_picker_session_only_and_default_selection(tmp_path: Path) -> None:
    session = _mk_app_session(tmp_path, runtime_key="dummy")
    session.settings_manager.set_default_provider("openai")
    session.settings_manager.set_default_model("gpt-4.1")
    models = [ModelInfo("provider-a", "session"), ModelInfo("provider-b", "default")]
    app = _OneTextualApp(session)
    with patch.object(session.model_registry, "get_available", return_value=models):
        async with app.run_test() as pilot:
            await _open_picker(app, pilot)
            await pilot.press("down", "enter")
            await pilot.pause()
            assert (session.model.provider, session.model.id) == ("provider-b", "default")
            assert session.settings_manager.get_default_model() == "gpt-4.1"
            await _open_picker(app, pilot)
            picker = app.screen_stack[-1]
            picker.query_one("#model-picker-list").highlighted = 0
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert (session.model.provider, session.model.id) == ("provider-a", "session")
            assert session.settings_manager.get_default_provider() == "provider-a"
            assert session.settings_manager.get_default_model() == "session"


@pytest.mark.asyncio
async def test_model_picker_application_error_stays_open(tmp_path: Path) -> None:
    session = _mk_app_session(tmp_path, runtime_key="dummy")
    app = _OneTextualApp(session)
    with patch.object(session.model_registry, "get_available", return_value=[ModelInfo("provider-a", "broken")]), patch.object(
        session, "set_model", side_effect=RuntimeError("no access")
    ):
        async with app.run_test() as pilot:
            picker = await _open_picker(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen_stack[-1] is picker
            assert "Unable to change model" in str(picker.query_one("#model-picker-error").content)


@pytest.mark.asyncio
async def test_detached_ctrl_k_loads_large_catalog_through_real_facade(tmp_path: Path) -> None:
    """Full catalog provenance must not overflow the facade's 64 KiB reply DTO."""
    from tests.support.tui import _FakeLoader

    models = [
        ModelInfo(
            "catalog-provider", f"catalog-model-{index:03d}", context_window=128_000,
            reasoning=index % 2 == 0, input_image=index % 3 == 0,
            metadata_source="remote-catalog", metadata_updated_at=1_700_000_000,
            manual_fields=frozenset({"context_window", "input_limit"}),
            catalog_fields=frozenset({"context_window", "input_limit", "output_limit"}),
            catalog_baseline={f"baseline-{item}": item for item in range(200)},
            catalog_values={f"catalog-{item}": item for item in range(200)},
            endpoint_fields=frozenset({"server_context_window"}),
        )
        for index in range(101)
    ]

    async def factory(_spec: RuntimeBootstrapSpec) -> TuiRuntimeHost:
        auth = AuthStorage.in_memory()
        auth.set_runtime_api_key("openai", "test-key")
        registry = ModelRegistry.create(auth, str(tmp_path / "models.json"))
        registry.get_available = lambda: models  # type: ignore[method-assign]
        session = AgentSession(
            SessionManager.in_memory(str(tmp_path)), SettingsManager.in_memory(), registry, _FakeLoader(),
            registry.find("openai", "gpt-4.1"), "medium",
        )
        return TuiRuntimeHost(type("Owner", (), {"session": session, "dispose": session.dispose})(), None)

    facade = AgentRuntimeFacade(
        RuntimeBootstrapSpec(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), factory,
        command_allowlist=TUI_COMMANDS,
    )
    await facade.start()
    view = TuiSessionView(facade)
    await view.refresh()
    app = _OneTextualApp(view)
    try:
        # A smaller host page budget exercises the complete multi-page route;
        # the production facade still enforces its normal 64 KiB DTO ceiling.
        with patch("one.core.tui_runtime_host._MODELS_PAGE_MAX_BYTES", 4 * 1024):
            async with app.run_test() as pilot:
                await pilot.press("ctrl+k")
                for _ in range(20):
                    await pilot.pause(0.02)
                    if app.screen_stack[-1].__class__.__name__ == "_ModelPicker":
                        break
                picker = app.screen_stack[-1]
                assert picker.__class__.__name__ == "_ModelPicker"
                assert len(picker._models) == len(models)
                assert picker._models[-1].id == "catalog-model-100"
                assert not hasattr(picker._models[0], "catalog_baseline")
    finally:
        await facade.close()
        assert facade.finalize()


def test_models_page_byte_pagination_advances_by_returned_rows() -> None:
    """A page boundary follows serialized bytes, not only the requested row count."""
    models = [ModelInfo("provider", f"model-{index}-" + "x" * 1_000) for index in range(100)]
    host = object.__new__(TuiRuntimeHost)
    host._epoch = 0
    host._session = lambda: type("Session", (), {"model_registry": type("Registry", (), {"get_available": lambda _self: models})()})()

    first = host.models_page(0, 100)
    assert 0 < len(first["models"]) < 100
    assert first["nextOffset"] == len(first["models"])
    assert len(str(first).encode("utf-8")) < 48 * 1024
    second = host.models_page(first["nextOffset"], 100)
    assert second["offset"] == first["nextOffset"]
    assert second["models"][0]["id"] == models[first["nextOffset"]].id


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (80, 24), (60, 20)])
async def test_model_picker_visual_hierarchy_and_inline_filters(tmp_path: Path, size: tuple[int, int]) -> None:
    """The search/filter row and real action outlines fit without hiding details."""
    session = _mk_app_session(tmp_path, runtime_key="dummy")
    app = _OneTextualApp(session)
    with patch.object(session.model_registry, "get_available", return_value=_models()):
        async with app.run_test(size=size) as pilot:
            picker = await _open_picker(app, pilot)
            search = picker.query_one("#model-picker-search")
            provider = picker.query_one("#model-picker-provider")
            assert search.region.y == provider.region.y
            assert search.region.right <= provider.region.x
            assert "models" in str(picker.query_one("#model-picker-title").content)
            for button_id in ("session", "default", "cancel"):
                button = picker.query_one(f"#model-picker-{button_id}")
                assert button.styles.border_top[0] == "round"
                assert button.region.bottom <= size[1]
            panel = picker.query_one("#model_picker")
            assert panel.border_title == "Choose model"
            if size[1] >= 27:
                assert panel.region.height < size[1]
            await pilot.press("m", "o", "d", "e", "l", "-", "1", "9")
            await pilot.pause()
            assert "1 /" in str(picker.query_one("#model-picker-title").content)


@pytest.mark.asyncio
async def test_session_button_label_survives_hover_focus_and_press(tmp_path: Path) -> None:
    """Button interaction must not restore the default opaque/tinted fill."""
    app = _OneTextualApp(_mk_app_session(tmp_path, runtime_key="dummy"))
    async with app.run_test(size=(80, 24)) as pilot:
        picker = await _open_picker(app, pilot)
        button = picker.query_one("#model-picker-session")
        await pilot.hover("#model-picker-session")
        button.focus()
        for active in (False, True):
            button.set_class(active, "-active")
            await pilot.pause()
            assert button.styles.tint.a == 0
            assert button.styles.background_tint.a == 0
            assert button.styles.border_top[0] == "round"
            frame = "\n".join(strip.text for strip in picker._compositor.render_strips())
            assert "Session (Enter)" in frame
