from __future__ import annotations

import json
from typing import Callable


def serialize_json_line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def attach_jsonl_line_reader(stream, callback: Callable[[str], None]):
    buffer = ""

    def on_data(data):
        nonlocal buffer
        buffer += data.decode("utf-8", errors="ignore") if isinstance(data, (bytes, bytearray)) else data
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line.strip():
                callback(line)

    stream.reconfigure(encoding="utf-8") if hasattr(stream, "reconfigure") else None
    orig = stream.readline

    async def _noop():
        return

    return lambda: _noop
