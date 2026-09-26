#!/usr/bin/env python3
"""Run special-character write cases through the installed ``one`` CLI."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:  # Supports direct execution in the image and importing from unit tests.
    from fake_openai_provider import FakeOpenAIProvider
except ModuleNotFoundError:
    from scripts.fake_openai_provider import FakeOpenAIProvider

REPORT_NAME = "write-tool-report.json"


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def call(content: str, *, framing: bool = False, malformed: str | None = None) -> str:
    raw = malformed or json.dumps({"tool": "write", "args": {"path": "target.txt", "content": content}}, ensure_ascii=False)
    return f"<|tool_call|>{raw}<tool_call|>" if framing else raw


CASES: list[dict[str, str]] = [
    {"id": "markers", "content": "<system-reminder><tool_response><|tool_response|><tool_call|><|tool_call|><untrusted-tool-output>"},
    {"id": "html", "content": "<main a=\"x&y\"><!-- comment --><script>if (a < b && c > d) {}</script></main>"},
    {"id": "entities", "content": "&lt; &gt; &amp; &quot; &#34; &#x3c;"},
    {"id": "json-escapes", "content": "< slash\\ quote\"\n tab\t CRLF\r\n {<tool_call|>}"},
    {"id": "unicode", "content": "\ufeffZażółć gęślą jaźń e\u0301 ‘smart’ 😀"},
    {"id": "framed", "content": "inside <|tool_call|> and <tool_response> survives", "framing": "yes"},
    {"id": "empty", "content": ""},
    {"id": "one-char", "content": "x"},
    {"id": "moderate", "content": "<tag>&amp;😀\n" * 2048},
]


def malformed_cases() -> list[dict[str, str]]:
    base = {"tool": "write", "args": {"path": "target.txt", "content": "raw\ncontrol\tkept"}}
    raw_control = json.dumps(base).replace("\\n", "\n").replace("\\t", "\t")
    missing_brace = '{"tool":"write","args":{"path":"target.txt","content":"missing brace"}'
    unescaped_quote = '{"tool":"write","args":{"path":"target.txt","content":"quote: " kept"}}'
    return [
        {"id": "raw-controls", "content": "raw\ncontrol\tkept", "malformed": raw_control},
        {"id": "missing-outer-brace", "content": "missing brace", "malformed": missing_brace},
        {"id": "unescaped-quote", "content": 'quote: " kept', "malformed": unescaped_quote},
    ]


def provider_text(request: dict[str, Any]) -> str:
    # User/system text may naturally contain tiny fixtures (notably "x").
    # Only assistant/tool replay is relevant to write-source feedback.
    messages = request.get("messages", [])
    replay = [message for message in messages if isinstance(message, dict) and message.get("role") in {"assistant", "tool"}]
    return json.dumps(replay, ensure_ascii=False)


def evidence_has_exact_content(files: list[Path], expected: str) -> bool:
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            args = record.get("args") if isinstance(record, dict) else None
            if isinstance(args, dict) and args.get("content") == expected:
                return True
    return False


def write_source_replayed(request: dict[str, Any], expected: str) -> bool:
    """Detect source only in assistant/tool write replay, not incidental prose."""
    for message in request.get("messages", []):
        if not isinstance(message, dict) or message.get("role") not in {"assistant", "tool"}:
            continue
        content = message.get("content")
        if isinstance(content, str):
            try:
                call = json.loads(content)
            except json.JSONDecodeError:
                continue
            if isinstance(call, dict) and call.get("tool") == "write":
                args = call.get("args")
                if isinstance(args, dict) and args.get("content") == expected:
                    return True
    return False


def run_case(case: dict[str, str], root: Path) -> dict[str, Any]:
    target = root / "target.txt"
    if target.exists():
        target.unlink()
    write = call(case["content"], framing=case.get("framing") == "yes", malformed=case.get("malformed"))
    with FakeOpenAIProvider([write, '{"tool":"finish","args":{"summary":"done","goal_success":true}}']) as provider:
        env = {"PATH": os.environ["PATH"], "HOME": str(root / "home"), "ONE_CODING_AGENT_DIR": str(root / "state"), "OPENAI_BASE_URL": provider.base_url, "PYTHONUTF8": "1"}
        result = subprocess.run(
            ["one", "run", "--provider", "openai", "--model", "gpt-4.1", "--api-key", "fake", "--tools", "write,finish", "--no-extensions", "--no-mcp", "--no-skills", "--json", "write diagnostic"],
            cwd=root, env=env, text=True, capture_output=True, timeout=30, check=False,
        )
        next_request = provider.requests[1] if len(provider.requests) > 1 else {}
    actual = target.read_bytes().decode("utf-8") if target.exists() else None
    evidence_files = list((root / "state" / "sessions").rglob("*.evidence"))
    expected = case["content"]
    next_text = provider_text(next_request)
    should_write = case.get("shouldWrite", "yes") == "yes"
    return {
        "id": case["id"], "parserMode": "framed-json" if case.get("framing") else "json-or-relaxed",
        "byteLength": len(expected.encode("utf-8")), "expectedSha256": sha256(expected),
        "actualSha256": sha256(actual) if actual is not None else None,
        "exact": (actual == expected) if should_write else actual is None,
        "providerContextOmission": (
            True if not should_write else "writeContentOmitted" in next_text if expected == "" else not write_source_replayed(next_request, expected)
        ),
        "evidenceExact": evidence_has_exact_content(evidence_files, expected) if should_write else actual is None,
        "cliExitCode": result.returncode, "requests": len(provider.requests),
    }


def main() -> int:
    root = Path(os.environ.get("WRITE_TOOL_WORKSPACE", "/tmp/diagnostic")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    ambiguous = {
        "id": "ambiguous-malformed-rejected", "content": "must not write", "shouldWrite": "no",
        "malformed": '{"tool":"write","args":{"path":"target.txt","content":"must not write"',
    }
    results = [run_case(case, root) for case in [*CASES, *malformed_cases(), ambiguous]]
    report = {"schemaVersion": 1, "cases": results, "ok": all(item["exact"] and item["providerContextOmission"] and item["evidenceExact"] and (item["cliExitCode"] == 0 or item["id"] == "ambiguous-malformed-rejected") for item in results)}
    path = root / REPORT_NAME
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"type": "write_tool_workload", "ok": report["ok"], "report": REPORT_NAME}))
    return 0 if report["ok"] else 4


if __name__ == "__main__":
    sys.exit(main())
