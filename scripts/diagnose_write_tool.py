#!/usr/bin/env python3
"""Build and run the offline Docker write-tool regression diagnostic.

Exit codes: 0 pass, 2 invalid arguments, 3 Docker unavailable/build failure,
4 workload assertion failure, 5 malformed report/internal failure.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EXIT_OK, EXIT_USAGE, EXIT_DOCKER, EXIT_WORKLOAD, EXIT_INTERNAL = range(5)
IMAGE = "one-write-tool-diagnostic:local"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", default="write-tool-reports")
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def load_report(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    cases = value.get("cases") if isinstance(value, dict) else None
    if not isinstance(cases, list) or not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in cases):
        raise ValueError("workload report has no valid cases")
    return value


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not docker_available():
        print("Docker daemon is unavailable", file=sys.stderr)
        return EXIT_DOCKER
    repo = Path(__file__).resolve().parents[1]
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    artifacts = Path(tempfile.mkdtemp(prefix="one-write-tool-", dir=report_dir))
    try:
        build = subprocess.run(["docker", "build", "-f", str(repo / "docker/write-tool.Dockerfile"), "-t", IMAGE, str(repo)], text=True, capture_output=True, timeout=180)
        if build.returncode:
            print("Docker build failed", file=sys.stderr)
            return EXIT_DOCKER
        command = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "256m", "--cpus", "1", "--mount", f"type=bind,src={artifacts},dst=/tmp/diagnostic", "--workdir", "/tmp/diagnostic", IMAGE]
        run = subprocess.run(command, text=True, capture_output=True, timeout=120)
        report = load_report(artifacts / "write-tool-report.json")
        destination = report_dir / "write-tool-report.json"
        shutil.copy2(artifacts / "write-tool-report.json", destination)
        ok = run.returncode == 0 and report.get("ok") is True
        output = {"ok": ok, "report": str(destination), "cases": len(report["cases"])}
        print(json.dumps(output, sort_keys=True) if args.json else f"write-tool diagnostic {'passed' if ok else 'failed'}: {destination}")
        return EXIT_OK if ok else EXIT_WORKLOAD
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        print(f"write-tool diagnostic failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_INTERNAL
    finally:
        if not args.keep_artifacts:
            shutil.rmtree(artifacts, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
