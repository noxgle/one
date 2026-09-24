#!/bin/sh
# Focused pytest entry points for local development. CI continues to run full.
set -eu

ROOT_DIR=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    PYTHON="$ROOT_DIR/.venv/bin/python"
else
    PYTHON=python
fi

usage() {
    cat <<'EOF'
Usage: scripts/test.sh <suite> [pytest arguments...]

Suites:
  quick       Fast baseline: settings and SDK smoke tests
  mcp         MCP client and shutdown tests
  tui         Textual TUI tests, including golden snapshots
  providers   Provider adapter and payload tests
  tools       Tool behavior, approval, budget, and image-tool tests
  auth        Authentication, credentials, OAuth, and config-path tests
  extensions  Extension runtime/UI, resource, and skill tests
  cli         Headless/run, intake, image, cross-platform, and diagnostic CLI tests
  rpc         JSON-RPC mode and snapshot tests
  core        Agent-loop, settings, event, and session tests
  sessions    Session-manager and session-branching tests
  failed, last Alias for previously failing tests (pytest --lf)
  all, full   Complete suite (equivalent to: .venv/bin/python -m pytest -q)

The repository virtual environment is used when available; otherwise python is
used. Additional arguments are passed directly to pytest. failed and last use
pytest --lf and may run all tests when no last-failure cache exists.

Examples:
  scripts/test.sh mcp -x
  scripts/test.sh tui -k paste
  scripts/test.sh full
EOF
}

suite=${1:-help}
if [ "$#" -gt 0 ]; then
    shift
fi

case "$suite" in
    help|-h|--help)
        usage
        exit 0
        ;;
    quick)
        set -- tests/test_settings.py tests/test_sdk_smoke.py "$@"
        ;;
    mcp)
        set -- tests/test_mcp.py tests/test_mcp_shutdown.py "$@"
        ;;
    tui)
        set -- tests/test_tui_commands.py tests/test_tui_cooperation.py tests/test_tui_image_input.py \
            tests/test_tui_input.py tests/test_tui_navigation.py tests/test_tui_rendering.py \
            tests/test_tui_retry.py tests/test_tui_snapshots.py tests/test_tui_streaming.py "$@"
        ;;
    providers)
        set -- tests/test_providers.py tests/test_provider_payloads.py tests/test_extra_providers.py \
            tests/test_ollama_provider.py tests/test_provider_timeout_regression.py "$@"
        ;;
    tools)
        set -- tests/test_tools.py tests/test_apply_patch.py tests/test_plan_tool.py tests/test_ask_user.py \
            tests/test_approval.py tests/test_budget.py tests/test_tool_output_pruning.py \
            tests/test_read_image_tool.py tests/test_unlimited_steps.py \
            tests/test_capability_error_policy.py tests/test_agent_tool_calls.py \
            tests/test_agent_tool_parsing.py tests/test_subagents.py tests/test_spawn_subagent_errors.py "$@"
        ;;
    auth)
        set -- tests/test_auth_and_cli.py tests/test_auth_precedence.py tests/test_login_validation.py \
            tests/test_oauth.py tests/test_oauth_production.py tests/test_complete_credential_removal.py \
            tests/test_config_paths.py "$@"
        ;;
    extensions)
        set -- tests/test_extension_runtime.py tests/test_extension_ui_hooks.py tests/test_resource_loader.py \
            tests/test_skills.py tests/test_frontmatter_parser.py "$@"
        ;;
    cli)
        set -- tests/test_run_mode.py tests/test_headless_e2e.py tests/test_intake.py \
            tests/test_image_cli_rpc.py tests/test_cross_platform_smoke.py \
            tests/test_diagnostic_runner.py tests/test_diagnostic_workload.py \
            tests/test_diagnostic_findings.py "$@"
        ;;
    rpc)
        set -- tests/test_rpc_mode.py tests/test_rpc_snapshots.py "$@"
        ;;
    core)
        set -- tests/test_agent_retry_abort.py tests/test_agent_steering.py tests/test_agent_timeouts_limits.py \
            tests/test_agent_tool_calls.py tests/test_agent_tool_parsing.py tests/test_event_snapshots.py \
            tests/test_session_manager.py tests/test_session_branching_export.py tests/test_settings.py \
            tests/test_unlimited_steps.py "$@"
        ;;
    sessions)
        set -- tests/test_session_manager.py tests/test_session_branching_export.py "$@"
        ;;
    failed|last)
        set -- --lf "$@"
        ;;
    all|full)
        ;;
    *)
        echo "Unknown test suite: $suite" >&2
        usage >&2
        exit 2
        ;;
esac

exec "$PYTHON" -m pytest -q "$@"
