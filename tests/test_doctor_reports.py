# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import json
from pathlib import Path

from one.core.doctor import DoctorCheck, DoctorEngine, DoctorIssue, DoctorReport, RepairAction, safe_diff


def test_report_order_and_registered_messages_are_deterministic() -> None:
    issues = (
        DoctorIssue("settings.unknown", "info", "project", "untrusted"),
        DoctorIssue("settings.missing", "error", "global", "untrusted"),
    )
    first = DoctorReport(issues)
    second = DoctorReport(tuple(reversed(issues)))
    assert first.to_json() == second.to_json()
    assert first.to_text() == second.to_text()
    document = json.loads(first.to_json())
    assert document["formatVersion"] == 1
    assert [item["id"] for item in document["issues"]] == ["settings.missing", "settings.unknown"]
    assert [item["level"] for item in document["issues"]] == ["info", "warning"]
    assert document["result"] == "action_required"


def test_report_and_diff_reject_arbitrary_secrets() -> None:
    secret = "sk-canary-api-123"
    oauth = "oauth-canary-456"
    url = f"https://user:{secret}@host/path?token={oauth}"
    issue = DoctorIssue(url, secret, url, oauth, url)
    known = DoctorIssue("settings.unknown", "warning", "project", url)
    action = RepairAction(url, url, url, url)
    report = DoctorReport((issue, known), (action,))
    before = {secret: {"nested": [url, {"token": oauth}]}}
    after = {"unknown": {oauth: secret}}
    output = report.to_text() + report.to_json() + safe_diff(before, after)
    assert secret not in output and oauth not in output and url not in output
    assert report.result == "action_required"
    assert safe_diff(before, before) == "[no changes]"


def test_check_exception_is_safe_and_continues(tmp_path: Path) -> None:
    class BadCheck(DoctorCheck):
        def run(self, cwd: Path) -> list[DoctorIssue]:
            raise RuntimeError("oauth-canary-exception https://secret-canary@host")

    class GoodCheck(DoctorCheck):
        def run(self, cwd: Path) -> list[DoctorIssue]:
            return [DoctorIssue("settings.missing", "info", "global", "ignored")]

    report = DoctorEngine((BadCheck(), GoodCheck())).check(tmp_path)
    assert "canary" not in report.to_json() + report.to_text()
    assert {i["id"] for i in json.loads(report.to_json())["issues"]} == {"check.failed", "settings.missing"}
    assert report.result == "action_required"
