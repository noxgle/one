from scripts.diagnostic_findings import SCHEMA_VERSION, detect, redact, validate_report


def test_redaction_and_bounds_credentials() -> None:
    value = redact({"apiKey": "secret", "message": "Bearer sk-abc AKIA1234567890ABCDEF ghp_abcdefghijklmnopqrstuv " + "x" * 5000})
    assert value["apiKey"] == "[REDACTED]"
    assert "sk-abc" not in value["message"]
    assert "AKIA1234567890ABCDEF" not in value["message"]
    assert value["message"].endswith("…[truncated]")


def test_lifecycle_and_malformed_detectors_are_evidence_based() -> None:
    findings = detect([{"type": "agent_start"}, {"type": "malformed", "raw": "bad"}, {"type": "tool_call_start", "toolCallId": "x"}])
    assert {item["id"] for item in findings} >= {"missing-agent-end", "malformed-rpc", "tool-lifecycle-mismatch"}
    assert all("probableCause" in item and "confidence" in item for item in findings)


def test_agent_end_is_grouped_by_turn_but_same_turn_duplicates_remain_findings() -> None:
    valid = detect([{"type": "agent_end", "turnId": "turn-1"}, {"type": "agent_end", "turnId": "turn-2"}])
    duplicate = detect([{"type": "agent_end", "turnId": "turn-1"}, {"type": "agent_end", "turnId": "turn-1"}])
    assert "duplicate-agent-end" not in {item["id"] for item in valid}
    assert "duplicate-agent-end" in {item["id"] for item in duplicate}


def test_malformed_finding_exposes_bounded_redacted_record_evidence() -> None:
    findings = detect([{"type": "malformed", "source": "stdout", "raw": "token=secret " + "x" * 1000}])
    evidence = findings[0]["evidence"][0]
    assert "events[0]" in evidence and "stdout" in evidence
    assert "secret" not in evidence and len(evidence) < 400


def test_coverage_and_context_findings_reference_recorded_evidence() -> None:
    findings = detect(
        [{"type": "provider_error", "message": "context window limit exceeded"}],
        {
            "coverage": {
                "read": {"expected": True, "observed": True},
                "apply_patch": {"expected": True, "observed": False},
                "wait_for_idle": {"kind": "rpc", "expected": True, "observed": False},
            },
            "missingCoverage": ["apply_patch", "wait_for_idle"],
        },
    )
    by_id = {item["id"]: item for item in findings}
    assert by_id["tool-coverage"]["severity"] == "medium"
    assert by_id["tool-coverage"]["evidence"] == ["coverage:apply_patch"]
    assert by_id["context-window-exhaustion"]["severity"] == "high"
    assert by_id["context-window-exhaustion"]["evidence"] == ["events[0]"]


def test_report_validation() -> None:
    report = {"schemaVersion": SCHEMA_VERSION, "run": {}, "findings": [], "modelAnalysis": {}, "cleanup": {}, "reportPaths": {}}
    validate_report(report)


def test_report_validation_rejects_bad_shapes() -> None:
    for report in ([], {"schemaVersion": SCHEMA_VERSION, "run": {}, "findings": [{}], "modelAnalysis": {}, "cleanup": {}, "reportPaths": {}}):
        try:
            validate_report(report)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:
            raise AssertionError("invalid report accepted")
