# Skills

Skills are modular, discoverable instruction sets that extend the agent's capabilities on demand. They follow a YAML frontmatter + Markdown body format (`.SKILL.md` files) and are loaded via the resource loader.

## Discovery

Skills are discovered from:

1. `~/.config/one/skills/` — global agent skills
2. `~/.agents/skills/` — platform-wide skills
3. `.one/skills/` — per-project skills
4. Explicit paths via `--skill <path>` CLI flag

The resource loader scans for files named `SKILL.md` (case-sensitive). Each skill's **name** is the parent directory name.

## Frontmatter

Skills use YAML frontmatter at the top of the file:

```yaml
---
name: example-skill
description: "Performs X, Y, Z for the user's project"
license: MIT
compatibility: ">=0.1.0"
metadata:
  author: "your-name"
  version: "1.0.0"
---
```

### Required fields

- **`name`** (string, 1–64 chars): A unique identifier matching `[a-z0-9]` with optional single hyphens (no edge hyphens).
- **`description`** (string, 1–1024 chars): A concise summary of what the skill does. Displayed in `/help` and system prompt.

### Optional fields

- **`license`** (string): SPDX identifier or custom license text.
- **`compatibility`** (string): Version constraint (e.g., `>=0.1.0`).
- **`metadata`** (object): Arbitrary key-value pairs (author, version, tags, etc.).
- **`allowed-tools`** (array): Restrict which tools the skill can use (e.g., `["read", "bash"]`). If omitted, all tools are allowed.
- **`disable-model-invocation`** (boolean, default `false`): When `true`, prevents the model from auto-invoking the skill. The skill can still be explicitly invoked via `/skill:<name>`.

### Unknown fields

Unknown frontmatter fields are silently ignored (forward compatibility).

## Body

The Markdown body contains the skill's instructions, workflows, and references. It is loaded **on demand** via `/skill:<name>` — not injected into the system prompt (progressive disclosure).

## Resources

Skills may include subdirectories for additional assets:

- **`scripts/`**: Executable scripts (bash, Python, etc.)
- **`references/`**: Reference documents, templates, or examples
- **`assets/`**: Images, diagrams, or other binary assets

Resource paths are **relative to the skill's base directory** (the directory containing `SKILL.md`).

## System Prompt Integration

The system prompt includes a **metadata section** listing all discovered skills:

```
# Skills

Available skills (metadata only — full skill loaded on demand via `/skill:<name>`):

- **example-skill**: Performs X, Y, Z for the user's project (filePath: /home/user/.config/one/skills/example-skill/SKILL.md)

When a skill matches the task, load the full `SKILL.md` with the `read` tool using the skill's exact `filePath`. Resource paths are relative to the skill directory. Use `/skill:<name> [args]` to explicitly invoke a skill — arguments are appended as user input.
```

### Skill invocation syntax — important distinction

**`/skill:<name>` is a TUI/interactive UI command, NOT a filesystem path.**

- The user types `/skill:<name>` in the TUI or interactive prompt. The agent's UI layer intercepts it and loads the full skill body before sending it to the provider.
- The autonomous model MUST **NEVER** pass `/skill:<name>` or `skill:<name>` as the `path` argument to the `read` tool.
- When the model decides to use a skill autonomously, it must call `read` with the **exact `filePath`** shown in the skill metadata (e.g. `/home/user/.config/one/skills/example-skill/SKILL.md`).
- If the model accidentally passes `/skill:<name>` to `read`, the agent returns a structured diagnostic explaining the syntax and pointing to the correct `filePath`.

This distinction prevents the model from confusing the user-facing UI command with a tool argument and ensures the progressive-disclosure flow works correctly.

## Invocation

### Interactive / TUI

- **`/skill:<name>`** — List available skills if no name provided.
- **`/skill:<name> [args]`** — Load the full skill and append `[args]` as user input.
- **`/reload`** — Reload all resources (skills, extensions, prompts).

### RPC

- **`invoke_skill`** ctype:
  ```json
  {
    "id": 1,
    "type": "invoke_skill",
    "name": "example-skill",
    "arguments": "optional user arguments"
  }
  ```
- **`get_skills`** returns skill metadata and diagnostics.
- **`reload_resources`** returns updated skills and diagnostics.

## Trust and Approval

Skills are loaded as **user input** — not as system instructions. This means:

1. The agent can review skill content before executing actions.
2. Cooperation mode approval gates apply to mutating tools invoked by skill instructions.
3. A trust warning is displayed before the first invocation of any skill.

### Cooperation

In cooperation mode, the agent must ask for approval before:

- Writing/editing files (as specified by the skill)
- Running destructive bash commands
- Any tool in the `approvalTools` list

The skill invocation itself does **not** bypass approval gates.

## Non-Auto-Execution

Skills are **never auto-executed**. They are:

- Listed in the system prompt (metadata only)
- Loaded explicitly via `/skill:<name>` or RPC `invoke_skill`
- Read as regular files (e.g., via the `read` tool) if the agent chooses

This ensures the agent remains in control and can review skill content before acting.

## Error Handling

- **Malformed frontmatter**: Non-fatal diagnostic; skill omitted from active list.
- **Invalid name**: Non-fatal diagnostic; skill omitted.
- **Missing description**: Non-fatal diagnostic; skill omitted.
- **Duplicate names**: First valid skill wins; subsequent duplicates produce diagnostics.
- **Unreadable file**: Non-fatal diagnostic; skill omitted.

Diagnostics are accessible via:
- TUI/interactive: displayed on `/reload` or `/skill:` (no args)
- RPC: `get_skills` diagnostics array
- System prompt: skills with errors are silently excluded

## Examples

### Basic Skill

```yaml
---
name: code-review
description: "Reviews code for best practices, security, and performance"
---

# Code Review Skill

When the user asks for code review:

1. Use `read` to load the target files.
2. Analyze for:
   - Security vulnerabilities
   - Performance issues
   - Style consistency
3. Provide a structured report with suggestions.
```

### Skill with Resources

```yaml
---
name: deployment
description: "Automates deployment to AWS, GCP, or Azure"
allowed-tools:
  - read
  - bash
  - write
---

# Deployment Skill

## Prerequisites

Load `scripts/deploy.sh` from the skill's `scripts/` directory.

## Workflow

1. Check the current environment.
2. Run the deployment script.
3. Verify health checks.
```

## Testing

When writing tests for skills:

1. Create a temporary directory with a `SKILL.md` file.
2. Instantiate `DefaultResourceLoader` with the directory in `additional_skill_paths`.
3. Assert on:
   - `get_skills()` returns the skill with correct name/description.
   - `get_skill(name)` returns the full body.
   - Diagnostics for malformed skills.
   - Deduplication behavior.

Example:

```python
async def test_skill_discovery():
    loader = DefaultResourceLoader(
        cwd="/tmp",
        agent_dir="/tmp",
        settings_manager=SettingsManager.in_memory(),
        additional_skill_paths=["/tmp/test-skills"],
    )
    await loader.reload()
    skills = loader.get_skills()
    assert len(skills["skills"]) == 1
    assert skills["skills"][0]["name"] == "test-skill"
```

## Versioning

Skills follow semantic versioning via frontmatter metadata. The resource loader does not enforce version constraints, but tools (e.g., CLI, TUI) may display compatibility warnings.
