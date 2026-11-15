---
title: "CLI reference"
---

Generated from `one/cli/args.py` by `scripts/generate_docs_reference.py`.
The declarations below show parser defaults and constraints, not every runtime
validation rule. `one run` is a public subcommand: it accepts a task (including
`@file`) and the run-specific options below. Other subcommands include `doctor`,
`install`, `remove`, `update`, `list`, and `config`; their own arguments are not
fully described by the shared parser. Consult `one --help`, the subcommand help,
and `one/cli/main.py` for dispatch and validation. See [automation](../guides/automation-rpc.md).

```python
parser.add_argument('messages', nargs='*')
parser.add_argument('--help', '-h', action='store_true')
parser.add_argument('--version', '-v', action='store_true')
parser.add_argument('--provider')
parser.add_argument('--model')
parser.add_argument('--api-key')
parser.add_argument('--llama-cpp-url')
parser.add_argument('--ollama-url')
parser.add_argument('--system-prompt')
parser.add_argument('--append-system-prompt')
parser.add_argument('--preset')
parser.add_argument('--thinking')
parser.add_argument('--temperature')
parser.add_argument('--temperature-mode')
parser.add_argument('--continue', '-c', dest='continue_session', action='store_true')
parser.add_argument('--resume', '-r', action='store_true')
parser.add_argument('--mode')
parser.add_argument('--no-session', action='store_true')
parser.add_argument('--session')
parser.add_argument('--fork')
parser.add_argument('--session-dir')
parser.add_argument('--models')
parser.add_argument('--tools')
parser.add_argument('--no-tools', action='store_true')
parser.add_argument('--extension', '-e', action='append', default=[])
parser.add_argument('--no-extensions', '-ne', action='store_true')
parser.add_argument('--print', '-p', action='store_true', dest='print_mode')
parser.add_argument('--export')
parser.add_argument('--export-format', choices=['html', 'jsonl'], default='html')
parser.add_argument('--skill', action='append', default=[])
parser.add_argument('--no-skills', '-ns', action='store_true')
parser.add_argument('--no-mcp', action='store_true', dest='no_mcp', help='Do not start MCP servers from settings.json.')
parser.add_argument('--prompt-template', action='append', default=[])
parser.add_argument('--no-prompt-templates', '-np', action='store_true')
parser.add_argument('--theme', action='append', default=[])
parser.add_argument('--no-themes', action='store_true')
parser.add_argument('--list-models', nargs='?', const=True)
parser.add_argument('--offline', action='store_true')
parser.add_argument('--verbose', action='store_true')
parser.add_argument('--cooperation', action='store_true', help='Cooperation mode: ask the user before running mutating tools (bash/write/edit/plan/apply_patch); rejections require a reason that is fed back to the model.')
parser.add_argument('--no-subagents', action='store_true', help='Disable subagents: the spawn_subagent tool is not registered.')
parser.add_argument('--no-bash-output', action='store_true', help='Do not print bash command output; show only the exit code.')
parser.add_argument('--json', action='store_true', dest='json_output', help='Print the run result as JSON (summary, goalSuccess, finished).')
parser.add_argument('--answer-file', dest='answer_file', help='Headless answer channel for ask_user: questions are written to this file and answers are read back (polled).')
parser.add_argument('--steer-file', dest='steer_file', help='Headless steering channel: write a message to this file while the run is active; it is read and cleared.')
parser.add_argument('--image', dest='image_paths', action='append', default=[], help='Image file path for vision input (repeatable); equivalent to the read_image tool.')
parser.add_argument('--param', '-P', action='append', default=[], help='Template parameter name=value (repeatable); {{name}} in @file content is replaced.')
```
