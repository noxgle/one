---
title: "Installation"
---

### From source

```bash
git clone https://github.com/noxgle/one.git
cd one
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
one --help
```

### From PyPI

```bash
pip install one-agent
one --help
```

### Docker

With Docker Compose, create the bind-mount directories before starting ONE:

```bash
mkdir -p docker-data/config docker-data/workspace
export ONE_UID="$(id -u)" ONE_GID="$(id -g)"
docker compose build
docker compose run --rm one
```

The workspace and ONE configuration persist on the host. For configurable
mount paths, provider credentials, headless use, and host Ollama networking,
see the [Docker guide](../library/docker.md).

The distribution name is `one-agent`; the import and console-script name are
`one`. The version source is `one/config.py:VERSION`.

Verify an installation with `one --version`, `one --help`, or
`python -m one --help`.
