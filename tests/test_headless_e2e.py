"""E2E subprocess tests: CLI → OpenAI-compatible HTTP → agent finish tool → exit code + report."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


class _E2EServer:
    """Minimal OpenAI-compatible chat completions server for e2e tests."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {}

    def start(self) -> int:
        """Start the server on a random available port. Returns the port."""

        # Shared mutable state captured by the handler closure.
        state: dict[str, Any] = {
            "requests": [],
            "tool_json": json.dumps(
                {"tool": "finish", "args": {"summary": "e2e done", "goal_success": True}}
            ),
        }

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode("utf-8"))
                state["requests"].append(
                    {
                        "path": self.path,
                        "method": "POST",
                        "headers": dict(self.headers),
                        "payload": payload,
                    }
                )
                is_stream = payload.get("stream", False)
                if is_stream:
                    self._respond_sse()
                else:
                    self._respond_json()

            def _respond_sse(self) -> None:
                choice_id = "chatcmpl-e2e"
                finish_reason = "stop"
                tool_json = state["tool_json"]
                chunk = json.dumps(
                    {
                        "id": choice_id,
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "gpt-4.1",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": tool_json},
                                "finish_reason": None,
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
                done = json.dumps(
                    {
                        "id": choice_id,
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "gpt-4.1",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": finish_reason,
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
                sse = f"data: {chunk}\n\ndata: {done}\n\ndata: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(sse.encode("utf-8"))

            def _respond_json(self) -> None:
                choice_id = "chatcmpl-e2e"
                tool_json = state["tool_json"]
                resp = json.dumps(
                    {
                        "id": choice_id,
                        "object": "chat.completion",
                        "created": 0,
                        "model": "gpt-4.1",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": tool_json,
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(resp.encode("utf-8"))

            def log_message(self, format: str, *args: Any) -> None:
                pass  # suppress logs

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd = httpd
        self._state = state  # share state with tests
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return httpd.server_address[1]

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._httpd = None
        self._state = {}

    @property
    def httpd(self) -> ThreadingHTTPServer:
        assert self._httpd is not None
        return self._httpd

    @property
    def thread(self) -> threading.Thread:
        assert self._thread is not None
        return self._thread

    @property
    def state(self) -> dict[str, Any]:
        assert self._state is not None
        return self._state


def _e2e_server_fixture(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> dict[str, Any]:
    """Fixture: create temp agent dir + start a local HTTP server.

    Returns {"server": _E2EServer, "agent_dir": str}.
    """
    agent_dir = tmp_path / ".one" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    # --- models.json: override openai/gpt-4.1 base_url to fake server ---
    models_path = agent_dir / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "openai": [
                        {
                            "id": "gpt-4.1",
                            "reasoning": True,
                            "contextWindow": 1_000_000,
                            "url": "http://127.0.0.1:0",  # placeholder, set by test
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    # --- settings.json: deterministic limits ---
    settings_path = agent_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "tools": {"maxSteps": 2, "timeoutSec": 10, "approval": False},
                "retry": {"enabled": True, "maxRetries": 1, "baseDelayMs": 100},
                "budget": {"maxTokens": 0, "maxTimeSec": 0},
                "defaultProvider": "openai",
                "defaultModel": "gpt-4.1",
                "defaultThinkingLevel": "medium",
            }
        ),
        encoding="utf-8",
    )

    # --- auth.json ---
    auth_path = agent_dir / "auth.json"
    auth_path.write_text(
        json.dumps({"apiKeys": {"openai": "test-key"}}),
        encoding="utf-8",
    )

    server = _E2EServer()
    port = server.start()

    # Rewrite models.json with the actual port.
    models_path.write_text(
        json.dumps(
            {
                "providers": {
                    "openai": [
                        {
                            "id": "gpt-4.1",
                            "reasoning": True,
                            "contextWindow": 1_000_000,
                            "url": f"http://127.0.0.1:{port}",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    env = {
        **dict(__import__("os").environ),
        "ONE_CODING_AGENT_DIR": str(agent_dir),
        "OPENAI_API_KEY": "test-key",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }

    def teardown() -> None:
        server.stop()

    request.addfinalizer(teardown)

    return {
        "server": server,
        "agent_dir": str(agent_dir),
        "env": env,
        "port": port,
    }


@pytest.fixture()
def e2e_server(request: pytest.FixtureRequest, tmp_path: Path) -> dict[str, Any]:
    return _e2e_server_fixture(request, tmp_path)


def test_run_finish_success(e2e_server: dict[str, Any]) -> None:
    """finish goal_success=true → returncode 0; stdout has summary; report written."""
    server = e2e_server["server"]
    env = e2e_server["env"]

    # Reset requests from any prior tests (each test gets a fresh fixture).
    server.state["requests"].clear()

    # Configure server to return success finish JSON.
    server.state["tool_json"] = json.dumps(
        {"tool": "finish", "args": {"summary": "e2e done", "goal_success": True}}
    )

    result = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "test e2e success"],
        cwd=str(e2e_server["agent_dir"]),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "e2e done" in result.stdout, f"Missing summary in: {result.stdout!r}"

    # Verify server received the request with auth header.
    requests = server.state["requests"]
    assert len(requests) == 1, f"Expected 1 request, got {len(requests)}: {requests}"
    req = requests[0]
    auth_header = req["headers"].get("Authorization", "")
    assert auth_header == "Bearer test-key", f"Wrong auth header: {auth_header!r}"
    payload = req["payload"]
    assert payload["stream"] is True
    assert payload["model"] == "gpt-4.1"
    messages = payload.get("messages", [])
    task_found = any(
        isinstance(m, dict) and m.get("role") == "user" and "test e2e success" in str(m.get("content", ""))
        for m in messages
    )
    assert task_found, f"Task not found in messages: {messages}"

    # Verify reports.jsonl exists and has correct entry.
    report_path = Path(e2e_server["agent_dir"]) / "reports.jsonl"
    assert report_path.exists(), "reports.jsonl not found"
    lines = report_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) >= 1, f"No report lines: {lines}"
    report = json.loads(lines[-1])
    assert report["task"] == "test e2e success"
    assert report["summary"] == "e2e done"
    assert report["goalSuccess"] is True
    assert report["finished"] is True
    assert report["exitCode"] == 0


def test_run_finish_failure(e2e_server: dict[str, Any]) -> None:
    """finish goal_success=false → returncode 1; report has goalSuccess=False."""
    server = e2e_server["server"]
    env = e2e_server["env"]

    server.state["requests"].clear()

    # Configure server to return failure finish JSON.
    server.state["tool_json"] = json.dumps(
        {"tool": "finish", "args": {"summary": "e2e failed", "goal_success": False}}
    )

    result = subprocess.run(
        [sys.executable, "-m", "one.cli.main", "run", "test e2e failure"],
        cwd=str(e2e_server["agent_dir"]),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, f"Expected exit 1, got {result.returncode}. stdout={result.stdout!r}"
    assert "e2e failed" in result.stdout, f"Missing summary in: {result.stdout!r}"

    # Verify server received the request with auth header and streaming.
    requests = server.state["requests"]
    assert len(requests) == 1
    req = requests[0]
    payload = req["payload"]
    assert payload["stream"] is True

    # Verify report.
    report_path = Path(e2e_server["agent_dir"]) / "reports.jsonl"
    assert report_path.exists(), "reports.jsonl not found"
    lines = report_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) >= 1
    report = json.loads(lines[-1])
    assert report["task"] == "test e2e failure"
    assert report["summary"] == "e2e failed"
    assert report["goalSuccess"] is False
    assert report["exitCode"] == 1
