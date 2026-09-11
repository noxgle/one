from __future__ import annotations

from typing import Any


async def run_print_mode(runtime_host: Any, options: dict[str, Any]) -> int:
    session = runtime_host.session
    images = options.get("images")
    initial = options.get("initialMessage")
    first = True
    if initial:
        await session.prompt(initial, images=images)
        first = False
        images = None  # only on first prompt

    for msg in options.get("messages", []):
        if first:
            await session.prompt(msg, images=images)
            first = False
            images = None
        else:
            await session.prompt(msg)

    # If no text was provided and only images were given, fail rather than
    # silently returning success with empty output.
    if not initial and not options.get("messages"):
        if images:
            print("Error: print mode requires at least one text message", flush=True)
            return 1

    if options.get("mode") == "json":
        import json

        print(json.dumps({"messages": session.messages}, ensure_ascii=False))
    else:
        last = session.get_last_assistant_text()
        if last:
            print(last)
    return 0
