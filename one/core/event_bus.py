from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

Listener = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = defaultdict(list)

    def on(self, event: str, listener: Listener) -> Callable[[], None]:
        self._listeners[event].append(listener)

        def off() -> None:
            if listener in self._listeners[event]:
                self._listeners[event].remove(listener)

        return off

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        for listener in list(self._listeners.get(event, [])):
            listener(payload)
