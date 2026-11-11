# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import asyncio
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from one.core.local_provider_setup import local_provider_operation


class LocalProviderSetup(ModalScreen[bool]):
    """Explicit local-server setup; opening this screen never probes a server."""

    CONFIG_TIMEOUT = 10.0

    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    LocalProviderSetup { align: center middle; background: $background 75%; }
    #local-provider-setup {
        width: 95%; max-width: 100; height: 90%; max-height: 100%;
        padding: 0 1; border: round $primary; background: $panel; color: $foreground;
    }
    #local-provider-form { height: 1fr; min-height: 3; scrollbar-size: 1 1; }
    #local-provider-form > Static { height: auto; }
    #local-provider-setup Input, #local-provider-setup Select { width: 1fr; height: 3; min-height: 3; }
    #local-provider-paths { height: auto; margin: 1 0; }
    #local-provider-message { height: auto; max-height: 7; overflow-y: auto; color: $foreground; }
    #local-provider-message.error { color: $error; }
    #local-provider-actions { height: 3; min-height: 3; }
    #local-provider-setup Button {
        height: 3; min-height: 3; min-width: 0; border: round $border;
        background: $panel; color: $foreground; background-tint: transparent;
        tint: transparent; text-opacity: 100%;
    }
    #local-provider-actions Button { width: 1fr; }
    #local-provider-setup Button.-primary { border: round $primary; color: $primary; background: $panel; }
    #local-provider-setup Button:hover, #local-provider-setup Button:focus {
        border: round $primary; background: $primary 15%; color: $foreground;
    }
    #local-provider-setup Button:disabled {
        background: $panel; color: $text-muted; border: round $border; opacity: 100%;
    }
    """

    def __init__(self, provider: str) -> None:
        super().__init__()
        self.provider = provider
        self._session: Any = None
        self._epoch: int | None = None
        self._busy = False
        self._ready = False
        self._initialized = False
        # Textual owns _task (message pump) and _closing (queue lifecycle).
        self._setup_closing = False
        self._operation_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="local-provider-setup"):
            yield Static(f"Configure {self.provider}", markup=False)
            with VerticalScroll(id="local-provider-form"):
                yield Static("Server base URL (HTTP/HTTPS)")
                yield Input(id="local-provider-url")
                yield Button("Fetch server models", id="local-provider-fetch")
                yield Static("Fetch lists models already on the server; it never downloads models.")
                yield Select([], prompt="Select a fetched model (optional)", id="local-provider-models")
                yield Static("Manual model ID (no fetch required)")
                yield Input(placeholder="Type the exact model ID, then Enter to save", id="local-provider-model")
                yield Static("", id="local-provider-paths", markup=False)
            yield Static("", id="local-provider-message", markup=False)
            with Horizontal(id="local-provider-actions"):
                yield Button("Save & use", id="local-provider-save", variant="primary")
                yield Button("Cancel", id="local-provider-cancel")

    def on_mount(self) -> None:
        self._session = self.app.session
        self._epoch = getattr(self._session, "epoch", None)
        self._message("Loading configuration…")
        for ident in ("fetch", "save"):
            self.query_one(f"#local-provider-{ident}", Button).disabled = True
        # Textual sets is_mounted only after Mount handlers have returned.
        self.call_after_refresh(self._initialize)

    def on_screen_resume(self) -> None:
        self.call_after_refresh(self._initialize)

    def _initialize(self) -> None:
        if not self._active():
            return  # ScreenResume schedules initialization again when uncovered.
        if not self._valid():
            self._message("Session changed. Cancel and reopen setup.", error=True)
            return
        if not self._initialized:
            self._initialized = True
            self._start("local_provider_config")

    def _active(self) -> bool:
        return (not self._setup_closing and self.is_attached and self.is_mounted
                and self.app.screen is self)

    def _valid(self) -> bool:
        return (self._active() and self.app.session is self._session
                and getattr(self._session, "epoch", None) == self._epoch)

    def on_unmount(self) -> None:
        self._setup_closing = True
        if self._operation_task is not None and not self._operation_task.done():
            self._operation_task.cancel()

    def _message(self, text: str, *, error: bool = False) -> None:
        message = self.query_one("#local-provider-message", Static)
        message.set_class(error, "error")
        message.update(text)
        message.scroll_home(animate=False)

    def _start(self, operation: str) -> None:
        if self._busy:
            return
        self._busy = True
        self._message({
            "local_provider_config": "Loading configuration…",
            "local_provider_models": "Fetching existing server models… (no downloads)",
            "local_provider_save": "Saving configuration and selecting model…",
        }[operation])
        for ident in ("fetch", "save"):
            self.query_one(f"#local-provider-{ident}", Button).disabled = True
        payload = {"provider": self.provider}
        if operation != "local_provider_config":
            payload["url"] = self.query_one("#local-provider-url", Input).value
        if operation == "local_provider_save":
            payload["model"] = self.query_one("#local-provider-model", Input).value
            for ident in ("url", "model", "models"):
                self.query_one(f"#local-provider-{ident}").disabled = True
        self._operation_task = asyncio.create_task(self._run(operation, payload))

    async def _run(self, operation: str, payload: dict[str, Any]) -> None:
        try:
            if not self._valid():
                if not self._setup_closing and self.is_attached and self.is_mounted:
                    self._message("Setup interrupted or session changed. Retry setup or cancel and reopen.", error=True)
                return
            if self._session.is_streaming:
                result = {"ok": False, "error": "Wait for the current response to finish, then reopen setup."}
            else:
                async def execute() -> dict[str, Any]:
                    if hasattr(self._session, "epoch"):
                        return await self._session.command(
                            "ui", operation=operation, payload={**payload, "epoch": self._epoch})
                    return await local_provider_operation(self._session, operation, payload, is_current=self._valid)

                # Only bound the read-only load, never a potentially partial save.
                if operation == "local_provider_config":
                    async with asyncio.timeout(self.CONFIG_TIMEOUT):
                        result = await execute()
                else:
                    result = await execute()
            if not self._valid():
                if not self._setup_closing and self.is_attached and self.is_mounted:
                    self._message("Setup interrupted or session changed. Retry setup or cancel and reopen.", error=True)
                return
            if not result.get("ok"):
                self._message(result.get("error", "Setup failed. Retry setup."), error=True)
            elif operation == "local_provider_config":
                self._ready = True
                self._message("Enter a model ID manually or fetch existing server models. Save writes global defaults.")
                self.query_one("#local-provider-url", Input).value = result["url"]
                self.query_one("#local-provider-model", Input).value = result["model"]
                self.query_one("#local-provider-paths", Static).update(
                    f"Save preview (global):\n{result['settingsPath']}\n{result['modelsPath']}"
                )
                self.query_one("#local-provider-url", Input).focus()
            elif operation == "local_provider_models":
                # Do not apply discovery from a URL the user edited while fetching.
                if payload["url"] == self.query_one("#local-provider-url", Input).value:
                    models = result["models"]
                    self.query_one("#local-provider-models", Select).set_options([(m, m) for m in models])
                    self._message(
                        f"Found {len(models)} models. Select one or enter an ID manually. Nothing saved."
                        if models else "No server models found. Enter a model ID manually or check the server URL."
                    )
                    if models:
                        self.query_one("#local-provider-models", Select).focus()
                else:
                    self._message("URL changed during fetch; fetch again for the new URL. Nothing saved.")
            else:
                if hasattr(self._session, "epoch"):
                    await self._session.refresh()
                if not self._valid():
                    return
                self.app._refresh_sidebar()
                self.app._write(f"Local provider {self.provider} configured and saved as default.", "info")
                self._setup_closing = True
                self.dismiss(True)
        except TimeoutError:
            if self._valid():
                self._message(
                    "Configuration load timed out. Retry setup or cancel and reopen."
                    if operation == "local_provider_config" else "Operation timed out. Check configuration and retry.",
                    error=True,
                )
        except Exception:
            if self._valid():
                self._message(
                    "Unable to complete setup. Check configuration permissions and server availability, then retry.",
                    error=True,
                )
        finally:
            self._busy = False
            if not self._setup_closing and self.is_attached and self.is_mounted:
                # Descendants may already be pruned during Textual shutdown,
                # before this screen receives Unmount or loses is_mounted.
                for ident in ("url", "model", "models"):
                    for widget in self.query(f"#local-provider-{ident}"):
                        widget.disabled = False
                for ident in ("fetch", "save"):
                    for button in self.query(f"Button#local-provider-{ident}"):
                        button.disabled = ident == "save" and not self._ready
                for button in self.query("Button#local-provider-fetch"):
                    button.label = "Fetch server models" if self._ready else "Retry setup"

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._valid() and event.select.id == "local-provider-models" and isinstance(event.value, str):
            self.query_one("#local-provider-model", Input).value = event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "local-provider-cancel":
            self.action_cancel()
        elif self._active() and not self._valid():
            self._message("Session changed. Cancel and reopen setup.", error=True)
        elif not self._ready and not self._busy and self._valid():
            self._start("local_provider_config")
        elif self._ready and self._valid():
            self._start("local_provider_models" if event.button.id == "local-provider-fetch" else "local_provider_save")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if not self._ready or self._busy or not self._valid():
            return
        if event.input.id == "local-provider-url":
            self.query_one("#local-provider-model", Input).focus()
        elif event.input.id == "local-provider-model":
            self._start("local_provider_save")

    def action_cancel(self) -> None:
        if not self._active():
            return
        # A save may already have replaced a file; don't imply it can be cancelled.
        if self._busy and self._ready and self._operation_task is not None:
            self._message("Operation in progress; wait for it to finish.")
            return
        self._setup_closing = True
        self.dismiss(False)
