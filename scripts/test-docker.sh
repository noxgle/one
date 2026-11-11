#!/bin/sh
set -eu

# Bind mounts are created by this user, never by the Docker daemon.
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
image="one-agent-test-$$"
project="one-test-$$"
cleanup() {
    docker compose -p "$project" --project-directory "$tmp" -f "$root/compose.yaml" -f "$tmp/override.yaml" down --remove-orphans >/dev/null 2>&1 || true
    docker image rm "$image" >/dev/null 2>&1 || true
    rm -rf "$tmp"
}
trap cleanup EXIT HUP INT TERM
mkdir -p "$tmp/docker-data/config" "$tmp/docker-data/workspace"
cat > "$tmp/override.yaml" <<OVERRIDE
services:
  one:
    image: $image
    volumes:
      - type: bind
        source: $tmp/docker-data/config
        target: /config
      - type: bind
        source: $tmp/docker-data/workspace
        target: /workspace
OVERRIDE
export ONE_UID=$(id -u) ONE_GID=$(id -g)
compose() {
    docker compose -p "$project" --project-directory "$tmp" -f "$root/compose.yaml" -f "$tmp/override.yaml" "$@"
}

docker build -t "$image" "$root"
compose config --quiet
# No command is passed by default: ONE chooses its normal interactive mode.
[ "$(docker image inspect --format '{{json .Config.Cmd}}' "$image")" = 'null' ]
compose run --rm -T --no-deps one --version
compose run --rm -T --no-deps one --help >/dev/null
# Invalid headless mode must propagate the CLI's usage exit status.
status=0
compose run --rm -T --no-deps one --mode invalid >/dev/null 2>&1 || status=$?
[ "$status" -eq 2 ] || { echo "Expected exit 2, got $status" >&2; exit 1; }
status=0
compose run --rm -T --no-deps one run >/dev/null 2>&1 || status=$?
[ "$status" -eq 2 ] || { echo "Expected headless usage exit 2, got $status" >&2; exit 1; }
compose run --rm -T --no-deps -e EXPECTED_UID="$ONE_UID" -e EXPECTED_GID="$ONE_GID" --entrypoint python one -c '
import os, pathlib, shutil
assert os.getuid() == int(os.environ["EXPECTED_UID"])
assert os.getgid() == int(os.environ["EXPECTED_GID"])
assert os.environ["HOME"] == "/config"
assert os.getcwd() == "/workspace"
for name in ("bash", "git", "curl", "ssh", "rg"):
    assert shutil.which(name), name
pathlib.Path(os.environ["HOME"], "home-test").write_text("home")
pathlib.Path(os.environ["ONE_CODING_AGENT_DIR"]).mkdir(parents=True, exist_ok=True)
pathlib.Path(os.environ["ONE_CODING_AGENT_DIR"], "persist-test").write_text("persist")
pathlib.Path("workspace-test").write_text("workspace")
'
compose run --rm -T --no-deps --entrypoint python one -c '
import os, pathlib
assert pathlib.Path(os.environ["HOME"], "home-test").read_text() == "home"
assert pathlib.Path(os.environ["ONE_CODING_AGENT_DIR"], "persist-test").read_text() == "persist"
assert pathlib.Path("workspace-test").read_text() == "workspace"
'
[ "$(stat -c %u:%g "$tmp/docker-data/workspace/workspace-test")" = "$ONE_UID:$ONE_GID" ]
echo 'Docker smoke tests passed'
