from __future__ import annotations

from typing import Any


class ExtensionRunner:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Any]] = {}

    def has_handlers(self, event: str) -> bool:
        return bool(self._handlers.get(event))

    async def emit(self, event: dict[str, Any]) -> Any:
        handlers = self._handlers.get(event.get("type", ""), [])
        result = None
        for h in handlers:
            result = await h(event)
        return result

    def get_registered_commands(self) -> list[Any]:
        return []
