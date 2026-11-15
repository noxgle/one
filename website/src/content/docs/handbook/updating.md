---
title: "Updating"
---

### Installation from PyPI

Upgrade the installed distribution with:

```bash
python -m pip install --upgrade one-agent
one --version
```

On Windows PowerShell, use the same command with the Python launcher if needed:

```powershell
py -3.12 -m pip install --upgrade one-agent
one --version
```

### Installation from source

Pull the latest changes and refresh the editable installation:

```bash
git pull
python -m pip install -e '.[dev]'
one --version
```

Updating the package does not remove user configuration, credentials, or saved
sessions. These are stored outside the repository under `~/.config/one` by
default (or under `ONE_CODING_AGENT_DIR` when configured). For a virtual
environment, activate it before running the commands above or use that
environment's Python executable explicitly.
