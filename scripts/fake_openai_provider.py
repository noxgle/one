"""A tiny deterministic loopback-only OpenAI chat-completions fake."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeOpenAIProvider:
    """Serve fixed assistant texts and retain request bodies for diagnostics."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> FakeOpenAIProvider:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    request = json.loads(self.rfile.read(length))
                except json.JSONDecodeError:
                    request = {"invalid": True}
                fake.requests.append(request)
                index = min(len(fake.requests) - 1, len(fake.responses) - 1)
                content = fake.responses[index]
                body = (
                    f"data: {json.dumps({'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}]})}\n\n"
                    "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        assert self._thread is not None
        self._thread.join(timeout=2)
