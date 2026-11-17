"""Exercise the installed CLI offline, inside the Docker smoke-test container."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SUMMARY = "docker smoke complete"
TOOL_CALL = json.dumps({"tool": "finish", "args": {"summary": SUMMARY, "goal_success": True}})


class Handler(BaseHTTPRequestHandler):
    requests: list[str] = []

    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        assert self.path == "/v1/chat/completions", self.path
        self.requests.append(self.path)
        if not payload.get("stream"):
            body = json.dumps({"id": "smoke", "object": "chat.completion", "created": 0,
                               "model": "local", "choices": [{"index": 0, "finish_reason": "stop",
                               "message": {"role": "assistant", "content": TOOL_CALL}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        chunks = [
            {"delta": {"role": "assistant", "content": TOOL_CALL}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        ]
        body = b"".join(
            ("data: " + json.dumps({"id": "smoke", "object": "chat.completion.chunk", "created": 0,
                                    "model": "local", "choices": [{"index": 0, **chunk}]}) + "\n\n").encode()
            for chunk in chunks
        ) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def run(args: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["one", *args, "--no-extensions", "--no-mcp"], input=stdin,
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, (args, result.returncode, result.stdout[-4000:], result.stderr[-4000:])
    assert "Traceback (most recent call last)" not in result.stderr, result.stderr[-4000:]
    return result


def tui(common: list[str]) -> None:
    master, slave = pty.openpty()
    process = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": "100", "LINES": "30"}
        process = subprocess.Popen(
            ["one", "--no-extensions", "--no-mcp", *common, "--mode", "tui"],
            stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True,
        )
        os.close(slave)
        slave = -1
        deadline = time.monotonic() + 20
        output = bytearray()
        sent = False
        rendered_at = None
        while time.monotonic() < deadline and process.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                output.extend(chunk)
                if len(output) > 1_000_000:
                    raise AssertionError("TUI output exceeded 1 MB")
                if rendered_at is None and b"\x1b[?1049h" in output:
                    rendered_at = time.monotonic()
            # Give Textual time to mount after entering the alternate screen.
            if not sent and rendered_at is not None and time.monotonic() - rendered_at >= 1:
                os.write(master, b"\x11")
                sent = True
        assert sent, f"TUI did not enter alternate screen: {bytes(output[-2000:])!r}"
        assert process.wait(timeout=2) == 0, f"TUI exit {process.returncode}: {bytes(output[-2000:])!r}"
        assert b"Traceback (most recent call last)" not in output
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        if slave >= 0:
            os.close(slave)
        os.close(master)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        common = ["--provider", "ollama", "--model", "local", "--ollama-url",
                  f"http://127.0.0.1:{server.server_port}/v1", "--no-session"]
        result = run(["run", *common, "--json", "smoke task"])
        report = json.loads(result.stdout)
        assert report["summary"] == SUMMARY and report["finished"] and report["goalSuccess"], report
        result = run([*common, "--mode", "text", "--print", "smoke task"])
        assert SUMMARY in result.stdout, result.stdout
        result = run([*common, "--mode", "json", "--json", "--print", "smoke task"])
        report = json.loads(result.stdout)
        assert SUMMARY in json.dumps(report["messages"]), report
        assert len(Handler.requests) == 3, Handler.requests

        empty = run([*common, "--mode", "rpc"])
        assert not empty.stdout.strip(), empty.stdout
        state = run([*common, "--mode", "rpc"], '{"type":"get_state","id":"smoke"}\n')
        lines = state.stdout.splitlines()
        assert lines, "RPC returned no response"
        responses = [json.loads(line) for line in lines]
        assert all(isinstance(item, dict) for item in responses), responses
        assert any(item.get("type") == "response" and item.get("command") == "get_state"
                   and item.get("id") == "smoke" and item.get("success") is True for item in responses), responses
        tui(common)
        print("Docker offline runtime smoke tests passed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
