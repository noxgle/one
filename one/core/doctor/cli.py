"""Offline diagnostics and conservative settings repair CLI."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from one.core.doctor import DoctorEngine
from one.core.doctor.repairs import execute_plan, make_plan


def run_doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="one doctor", description="Run offline diagnostics and optional repairs.", allow_abbrev=False)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Check installation and settings (default).")
    modes.add_argument("--fix", action="store_true", help="Plan and apply approved safe settings repairs.")
    modes.add_argument("--setup", action="store_true", help="Set up ONE offline.")
    parser.add_argument("--dry-run", action="store_true", help="Preview fixes without writing.")
    parser.add_argument("--yes", action="store_true", help="Confirm safe fixes without prompting.")
    parser.add_argument("--json", action="store_true", help="Emit a single JSON report.")
    try:
        args = parser.parse_args(argv)
        if args.dry_run and not (args.fix or args.setup):
            parser.error("--dry-run requires --fix or --setup")
        if args.yes and not args.fix:
            parser.error("--yes requires --fix")
        if args.json and args.setup:
            parser.error("--json is not available with --setup")
    except SystemExit as exc:
        return int(exc.code)
    if args.setup:
        from one.core.doctor.setup import run_setup

        return run_setup(Path.cwd(), dry_run=args.dry_run)
    report = DoctorEngine().check(Path.cwd())
    if args.fix:
        plan = make_plan(Path.cwd(), report)
        report = plan.report
        if not args.dry_run:
            approved = args.yes
            if not approved and any(a.id == "settings.migrate" for a in report.actions) and sys.stdin.isatty():
                print(report.to_text(), file=sys.stderr)
                try:
                    print("Apply safe repairs? [y/N] ", end="", file=sys.stderr, flush=True)
                    approved = sys.stdin.readline().strip().lower() in {"y", "yes"}
                except (EOFError, KeyboardInterrupt):
                    approved = False
            report = execute_plan(Path.cwd(), plan, approved)
    print(report.to_json() if args.json else report.to_text())
    return 0 if report.result == "ok" else 1
