#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${ONE_INSTALL_ROOT:-$HOME/.one}"
VENV_DIR="$INSTALL_ROOT/venv"
BIN_DIR="${ONE_BIN_DIR:-$HOME/.local/bin}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "[one] Installing from: $ROOT_DIR"
echo "[one] Install root: $INSTALL_ROOT"
echo "[one] Bin dir: $BIN_DIR"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[one] ERROR: Python not found: $PYTHON_BIN" >&2
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12+ is required")
print(f"[one] Python OK: {sys.version.split()[0]}")
PY

mkdir -p "$INSTALL_ROOT"
"$PYTHON_BIN" -m venv "$VENV_DIR"

"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel >/dev/null
"$VENV_DIR/bin/pip" install -e "$ROOT_DIR" >/dev/null

mkdir -p "$BIN_DIR"
cat >"$BIN_DIR/one" <<EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/one" "\$@"
EOF
chmod +x "$BIN_DIR/one"

echo "[one] Installed launcher: $BIN_DIR/one"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "[one] Add to PATH (bash/zsh):"
  echo "export PATH=\"$BIN_DIR:\$PATH\""
fi

echo "[one] Verifying installation..."
"$BIN_DIR/one" --version
echo "[one] Done. You can now run: one"
