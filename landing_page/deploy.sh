#!/bin/bash
# Local build + static upload only. No server-side build commands.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/deploy.py" "$@"
