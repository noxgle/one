# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

We actively support the latest 0.1.x release. Security fixes are backported
when feasible.

## Reporting a vulnerability

Please report security vulnerabilities privately via
[GitHub Security Advisories](https://github.com/picon/one/security/advisories/new).
If advisories are unavailable (e.g. in a fork), email **TODO-MAINTAINER:
security@example.com** with "[SECURITY] " in the subject. Do **not** open a
public issue for security-sensitive bugs.

Include:

- A description of the vulnerability and its impact.
- Steps to reproduce (code, commands, or configuration).
- Your assessment of severity (if any).

We aim to acknowledge reports within 48 hours and provide a fix or mitigation
plan within 30 days for confirmed vulnerabilities.

## What this project handles

### Sensitive local state

- `auth.json` — API keys and OAuth tokens; written with mode `0600` on POSIX.
- `settings.json` / `models.json` — configuration; written atomically.
- Session JSONL files (`sessions/*.jsonl`) — conversation history; written atomically.
- `ONE_CODING_AGENT_DIR` can override the default `~/.config/one`.

Text configuration files and session JSONL are written through atomic
`tempfile + os.replace` to prevent truncation on interruption, and new
directories are created with mode `0700`. The blob store for image attachments
uses its own dedicated 0600/0700 handling (content-addressed blobs with
exclusive temp files and fsync).

### Code execution boundaries

- **Project extensions** (`.one/extensions/*.py`) are imported and executed at
  session startup. They run with the same permissions as `one` — **not sandboxed**.
- **MCP server commands** (from `.one/settings.json`) are spawned as child
  processes. They run with the same permissions as `one` — **not sandboxed**.
- **Bash tool** executes shell commands in the project working directory.
- **File tools** (read/write/edit/grep/find/ls) operate on the filesystem.
- **Cooperation mode** is an **approval gate only**, not a sandbox. Tools
  still execute with full permissions.

### Provider data transmission

Provider API keys and OAuth tokens are sent to the respective provider
endpoints over HTTPS. `one` does not proxy or log this traffic. Token
redaction is applied to error messages and RPC responses.

### OAuth / subscription login

- Anthropic and ChatGPT/Codex subscription logins are supported production
  features, but they use reverse-engineered OAuth flows. These endpoints are
  **not part of any public API** and are externally controlled by the respective
  providers.
- They may change without notice. Third-party use of subscription credentials
  against these backends is at the user's own risk and may violate the provider's
  Terms of Service.
- Tokens are stored in `auth.json` and automatically refreshed before expiry.

### Environment variables

API keys can be supplied via environment variables (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, etc.) as an alternative to `auth.json`. Environment
variables take precedence over stored keys but are not persisted.

## Recommendations

1. **Only run `one` in trusted repositories.** Use `one --no-extensions
   --no-mcp` for untrusted code.
2. **Keep `auth.json` private.** Verify file permissions (`stat auth.json`
   should show `600`).
3. **Use `ONE_CODING_AGENT_DIR`** to isolate session data when testing.
4. **Pin dependencies** in production environments.
5. **Review extensions** before loading untrusted custom packages.

If you need to contact maintainers without using advisories, email
**TODO-MAINTAINER: security@example.com** with "[SECURITY] " in the subject.
