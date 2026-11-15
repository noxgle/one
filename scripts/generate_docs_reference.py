#!/usr/bin/env python3
"""Generate offline reference inventories; run from any directory with project Python."""
# Copyright (c) 2026 picon
# SPDX-License-Identifier: MIT
# Source: https://github.com/noxgle/one
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from one.core.settings_manager import DEFAULT_SETTINGS  # noqa: E402
from one.resources.resource_loader import TOOL_ARG_SCHEMAS  # noqa: E402
from one.tools.index import DEFAULT_TOOL_NAMES, all_tools  # noqa: E402

OUT = ROOT / "website/src/content/docs/reference"


def page(title: str, body: str) -> str:
    return f'---\ntitle: "{title}"\n---\n\n{body.rstrip()}\n'


def write(name: str, title: str, body: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.md").write_text(page(title, body), encoding="utf-8")


def main() -> None:
    tree = ast.parse((ROOT / "one/cli/args.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"]
    calls.sort(key=lambda node: node.lineno)
    cli = """Generated from `one/cli/args.py` by `scripts/generate_docs_reference.py`.
The declarations below show parser defaults and constraints, not every runtime
validation rule. `one run` is a public subcommand: it accepts a task (including
`@file`) and the run-specific options below. Other subcommands include `doctor`,
`install`, `remove`, `update`, `list`, and `config`; their own arguments are not
fully described by the shared parser. Consult `one --help`, the subcommand help,
and `one/cli/main.py` for dispatch and validation. See [automation](../guides/automation-rpc.md).

```python
""" + "\n".join(ast.unparse(call) for call in calls) + "\n```"
    write("cli", "CLI reference", cli)

    settings = """Generated from `DEFAULT_SETTINGS` in `one/core/settings_manager.py`.
The JSON values give declared defaults and their JSON types; `null` does not
specify the type of an accepted non-null value. Objects may accept additional
keys not present in this inventory, including normalized/optional settings.
This is not a complete validation schema: accepted ranges, merge exceptions,
and runtime-derived values require the settings manager and their consumers.
See [configuration and security](../guides/configuration-security.md).

```json
""" + json.dumps(DEFAULT_SETTINGS, indent=2, ensure_ascii=False) + "\n```"
    write("settings", "Settings reference", settings)

    tools = ["""Generated from `one/tools/index.py` and `one/resources/resource_loader.py`.
Schemas below are provider-facing native JSON schemas; prompt syntax is listed
separately and is not a substitute for execution validation. Availability depends
on policy, mode and configuration. MCP and extension tools are dynamic and are
not included. Defaults mark `DEFAULT_TOOL_NAMES`, not guaranteed availability.
Consult `one/core/agent_session.py` and each implementation for execution semantics.
"""]
    for name, tool in all_tools.items():
        tools.append(f"## {name}\n\n{tool.description}\n\nDefault list: "
                     f"{'yes' if name in DEFAULT_TOOL_NAMES else 'no'}.\n\n"
                     f"Prompt syntax: `{TOOL_ARG_SCHEMAS.get(name, '(not declared)')}`\n\n"
                     "```json\n" + json.dumps(tool.schema, indent=2, ensure_ascii=False) + "\n```\n")
    write("tools", "Built-in tools reference", "\n".join(tools))


if __name__ == "__main__":
    main()
