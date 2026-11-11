# Running ONE with Docker

Build the local image with Docker and the Docker Compose plugin. Run these
commands from the repository root. The image runs the `one` CLI by default;
without arguments ONE normally starts its TUI, so use an interactive terminal.

**Linux**, from the repository root:

```bash
mkdir -p docker-data/config docker-data/workspace
cp .env.example .env
export ONE_UID="$(id -u)" ONE_GID="$(id -g)"
docker compose build
docker compose run --rm one
docker compose run --rm -T one --version
docker compose run --rm -T one doctor --check
docker compose run --rm -T one run "Analyze the current workspace" --provider openai --model gpt-4o-mini
```

Configure the provider key in `.env` or in ONE before running a task. Do not
overwrite an existing `.env` containing your configuration.

The directories must exist before Compose starts: bind mount creation by the
Docker daemon is disabled. The selected UID/GID (default `1000:1000`) must be
able to write both directories. Match them to their host owner or grant suitable
permissions; on systems with user namespace remapping, host ownership may need
additional adjustment. Set `ONE_UID` and `ONE_GID` in the environment or `.env`
for subsequent commands too.

The workspace is mounted at `/workspace`, the working directory. Files created
or edited there persist on the host. Configuration and sessions persist under
`/config`; this directory can contain credentials and other sensitive data.
`HOME` is `/config`, so other tools may also store data there. Back it up and
restrict host access accordingly; do not commit it or `.env`.

Set `ONE_CONFIG_DIR` and `ONE_WORKSPACE_DIR` in `.env` to existing host
paths. Relative paths are resolved from the Compose project directory; use
forward slashes in Windows paths, for example `C:/Users/alice/one-workspace`.
ONE writes `settings.json`, `auth.json`, `models.json`, sessions, MCP and
extension state directly under `/config` using its normal state layout. Project
`.one` files, if used, live in the workspace. Rebuilding or removing the
container does not remove bind-mounted data; back up both directories separately.

## Requirements and installation

Linux needs Docker Engine and the Compose V2 plugin: follow the official
[Engine installation guide](https://docs.docker.com/engine/install/) for your
distribution and [Compose plugin guide](https://docs.docker.com/compose/install/linux/).
On Windows 11 install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/),
enable its WSL 2 backend, and start Docker Desktop. Windows 10 also requires a
Docker Desktop version and WSL 2 backend supported by that Windows build;
consult Docker's current system requirements. This image runs Linux containers,
not native Windows containers. Windows and ARM64 have not been validated here.
Use a terminal with interactive TTY support for the TUI. On Windows, files in the
WSL filesystem generally perform better than Windows filesystem mounts; Docker
Desktop must be permitted to access the chosen host paths.

**Windows PowerShell**, from the repository root, after starting Docker Desktop
with the WSL2 backend (no host shell script required):

```powershell
New-Item -ItemType Directory -Force docker-data/config, docker-data/workspace
Copy-Item .env.example .env
docker compose build
docker compose run --rm one
docker compose run --rm -T one --version
docker compose run --rm -T one doctor --check
docker compose run --rm -T one run "Analyze the current workspace" --provider openai --model gpt-4o-mini
```

Edit `.env` for custom paths and provider keys. Compose reads it for substitution;
changing it does not modify existing data. Linux users should set UID/GID to the
host directory owner as shown above. Windows/WSL bind mount ownership and chmod
semantics depend on Docker Desktop and the filesystem; test write access instead
of assuming Linux ownership behavior. If access fails, create the directories as
the intended host user and adjust their ownership or ACLs; never use `chmod 777`.
Rootless Docker or user namespace remapping can require different mappings.

## Direct Docker and updates

On Linux, after creating both directories, a direct invocation is:

```bash
docker build -t one-agent:local .
docker run --rm -it --init --user "$(id -u):$(id -g)" \
  -e HOME=/config -e ONE_CODING_AGENT_DIR=/config \
  -v "$(pwd)/docker-data/config:/config" \
  -v "$(pwd)/docker-data/workspace:/workspace" one-agent:local
```

Pass CLI arguments after the image name; use `-i` without `-t` for headless
runs and pass provider variables with `-e`. Windows PowerShell can use
`${PWD}/docker-data/config:/config` and `${PWD}/docker-data/workspace:/workspace`
with `--mount` or `-v`; use Docker Desktop-accessible absolute paths. To update,
fetch the desired source revision and run `docker compose build` again, then
start a new container. Do not delete the host directories: image rebuilds and
`docker compose down` do not remove these bind mounts.

## Credentials and CLI

Copy `.env.example` to `.env` and supply only the provider keys you need, or
export them in your shell. Compose passes `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, and `GEMINI_API_KEY` to the container. Treat `.env` as a
secret. ONE can also save credentials via the TUI; these persist in the config
mount. Local Ollama does not require an API key.

Arguments after the service name are passed to `one`, not to a shell:

```bash
docker compose run --rm -T one --version
docker compose run --rm -T one --help
docker compose run --rm -T one doctor --check
docker compose run --rm -T one run "summarize the files in this workspace" --provider openai --model gpt-4o-mini
docker compose run --rm one --no-extensions --no-mcp
```

`-T` disables the pseudo-TTY for headless use. `run` is a CLI subcommand;
`--mode` accepts `tui`, `text`, `json`, and `rpc`, not `cli`. For a shell,
explicitly override the entrypoint: `docker compose run --rm --entrypoint bash
one`. Exit the TUI normally to end a one-off container. `--rm` removes the
container, not its bind-mounted data.

## Host Ollama

ONE's local `ollama` provider defaults to `http://localhost:11434/v1`. Inside a
container, `localhost` refers to that container, not the host. Point ONE at an
address reachable from the container and use a model installed in Ollama. For
example, on Docker Desktop, if Ollama is listening on an interface reachable
from containers:

```bash
docker compose run --rm one --provider ollama --model YOUR_MODEL --ollama-url http://host.docker.internal:11434/v1
```

On Linux Docker Engine, `host.docker.internal` may need an explicit mapping.
Add `extra_hosts: ["host.docker.internal:host-gateway"]` to the `one` service
in a local Compose override file, or use another host address reachable from
the container.

Alternatively configure `OLLAMA_BASE_URL` in the container environment (for
example with `docker compose run -e OLLAMA_BASE_URL=...`); Compose does not
forward it automatically. Include `/v1`: ONE uses Ollama's OpenAI-compatible
chat completions endpoint. `--ollama-url` overrides that environment value.
Ollama must listen beyond host loopback for bridge-network access; exposing it
to a network can allow others to use it, so restrict access appropriately. No
Ollama server or model is bundled in this image.

## Security

ONE can execute commands and modify files available to the container, including
the writable workspace and config mounts. Extensions and MCP servers execute
with the agent's permissions; `--no-extensions --no-mcp` disables them for
untrusted projects. A container and non-root UID are not a complete sandbox:
avoid mounting sensitive directories, the Docker socket, or credentials that
are not needed. Network access and provider costs remain possible.
