"""Private, opt-in Responses replay bookkeeping; does not invoke providers."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from one.core.session_manager import SessionManager
from one.providers.base import native_tool_replay_pairs
from one.providers.codex_responses import BASE_URL, CodexResponsesAdapter
from one.providers.native_compaction import (
    NativeCompactionHTTPError,
    NativeCompactionProtocolError,
    validate_native_items,
)

_ENDPOINT = f"{BASE_URL}/responses"


def native_eligible(provider: Any, model: str, settings: dict[str, Any], *, endpoint: str = _ENDPOINT) -> bool:
    """Require explicit V2 settings and the adapter's exact-target capability gate."""
    native = settings.get("native")
    return (isinstance(provider, CodexResponsesAdapter) and provider.name == "chatgpt"
            and endpoint == _ENDPOINT and bool(model)
            and settings.get("enabled") is True and settings.get("experimentalV2") is True
            and settings.get("strategy") in ("auto", "native_openai")
            and isinstance(native, dict) and native.get("chatgpt") == "auto"
            and provider.supports_native_compaction_for(model, endpoint) is True)


def native_retryable(error: Exception) -> bool:
    """Only protocol failures and structured HTTP 400/422 permit local retry."""
    return (isinstance(error, NativeCompactionProtocolError)
            or isinstance(error, NativeCompactionHTTPError) and error.status_code in (400, 422)
            or isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (400, 422))


@dataclass
class NativeCompactionView:
    """One run's private replay view. Never put this object in public DTOs.

    `fingerprint` must identify all effective context, settings and preset inputs,
    using a stable, non-secret string across resume. A changed fingerprint cannot
    resume a window. The storage binding hashes it without writing it in clear.
    """
    storage: SessionManager = field(repr=False)
    adapter: CodexResponsesAdapter = field(repr=False)
    model: str
    fingerprint: str = field(repr=False)
    endpoint: str = _ENDPOINT
    disabled: bool = field(default=False, init=False)
    _pending: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _retried: bool = field(default=False, init=False, repr=False)

    def _binding(self) -> str:
        digest = hashlib.sha256(self.fingerprint.encode("utf-8")).hexdigest()
        return f"{self.adapter.name}:native-v1:{digest}"

    def disable(self, error: Exception) -> bool:
        """Disable this view and return whether one canonical retry is permitted.

        Caller must additionally ensure no tool side effects occurred and apply
        ordinary run admission/budget checks. Never log the exception content.
        """
        retry = not self.disabled and not self._retried and native_retryable(error)
        self.disabled = True
        self._retried = self._retried or retry
        self._pending = None
        return retry

    def prepare(
        self, messages: list[dict[str, Any]], entry_ids: list[str], settings: dict[str, Any],
        *, thinking_level: str = "off", images: list[dict[str, Any]] | None = None,
        storage_dir: str = "",
    ) -> list[dict[str, Any]] | None:
        """Before invoke, return native_input_items or None for ordinary input.

        Messages and IDs must be the same full canonical branch, excluding the
        system prefix. Pass the same images/storage_dir as the impending call.
        Call commit only after exactly one assistant message is persisted.
        """
        self._pending = None
        if self.disabled or not native_eligible(self.adapter, self.model, settings, endpoint=self.endpoint):
            self.disabled = True
            return None
        if (not self.fingerprint or entry_ids != self.storage.get_message_entry_ids()
                or messages != self.storage.build_session_context(raw_messages=True)["messages"]
                or len(messages) != len(entry_ids)):
            self.disabled = True
            return None
        view = self.storage.get_native_compaction(self._binding(), self.model, self.endpoint)
        replay = None
        first = entry_ids[0] if entry_ids else None
        if view is not None:
            covered = view["coveredEntryIds"]
            # A window must cover the prefix of this exact canonical branch.
            if entry_ids[:len(covered)] != covered:
                self.disabled = True
                return None
            tail = messages[len(covered):]
            # Seed the adapter's pairing algorithm with calls already in the
            # private window, then remove those synthetic calls from its output.
            calls = []
            remaining = set()
            for item in view["nativeInputItems"]:
                if item["type"] == "function_call" and isinstance(item.get("call_id"), str):
                    try:
                        arguments = json.loads(item.get("arguments", "{}"))
                    except (TypeError, ValueError):
                        arguments = None
                    if isinstance(arguments, dict) and isinstance(item.get("name"), str):
                        calls.append({"id": item["call_id"], "name": item["name"], "arguments": arguments})
                        remaining.add(item["call_id"])
                elif item["type"] == "function_call_output":
                    remaining.discard(item.get("call_id"))
            calls = [call for call in calls if call["id"] in remaining]
            source = ([{"role": "assistant", "content": "", "_nativeToolCalls": calls}] if calls else []) + tail
            try:
                payload = self.adapter._build_payload(self.model, source, thinking_level, True,
                                                      images=images, storage_dir=storage_dir)
            except Exception:
                self.disabled = True
                raise
            added = payload["input"]
            if calls:
                paired, _ = native_tool_replay_pairs(source)
                synthetic_count = sum(1 for index, _ in paired if index == 0)
                added = added[synthetic_count:]
            try:
                replay = validate_native_items(view["nativeInputItems"] + added)
            except NativeCompactionProtocolError:
                self.disabled = True
                raise
        self._pending = {"ids": list(entry_ids), "first": first, "input": replay, "binding": self._binding(),
                         "model": self.model, "endpoint": self.endpoint}
        return validate_native_items(replay) if replay is not None else None

    def commit(self, output_items: Any, assistant_entry_id: str) -> bool:
        """After persistence, atomically save a replay window; return whether saved.

        The branch must be unchanged since prepare except for the one assistant
        message. Output is the complete ordered ChatResult.native_output_items.
        Errors leave the old durable view intact and disable this run's view.
        """
        pending = self._pending
        self._pending = None
        if self.disabled or pending is None:
            return False
        try:
            ids = self.storage.get_message_entry_ids()
            if (not pending["first"] or ids != pending["ids"] + [assistant_entry_id]
                    or (self._binding(), self.model, self.endpoint)
                    != (pending["binding"], pending["model"], pending["endpoint"])):
                raise NativeCompactionProtocolError("Native compaction branch changed")
            entry = self.storage.get_entry(assistant_entry_id)
            if not entry or entry.get("message", {}).get("role") != "assistant":
                raise NativeCompactionProtocolError("Native compaction requires persisted assistant")
            output = validate_native_items(output_items)
            indexes = [i for i, item in enumerate(output) if item["type"] == "compaction"]
            if indexes:
                window = output[indexes[-1]:]
            elif pending["input"] is not None:
                window = pending["input"] + output
            else:
                return False
            window = validate_native_items(window)
            self.storage.append_native_compaction(self._binding(), self.model, self.endpoint, window,
                                                  pending["first"], assistant_entry_id)
            return True
        except Exception:
            self.disabled = True
            raise
