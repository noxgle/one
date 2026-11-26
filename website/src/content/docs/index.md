---
title: ONE documentation
description: Install, configure and automate the local-first terminal agent.
---

ONE is a local-first autonomous Python terminal agent. Use your hardware and
models, hosted providers, or a hybrid setup. It can inspect files, execute tools,
plan work, delegate to subagents and preserve sessions.

## Start with a small task

1. Follow the [installation guide](handbook/installation.md) (Python 3.12+).
2. Run the [five-minute quickstart](library/quickstart.md), or use the
   [local-first path](guides/local-first-quickstart.md) with Ollama or llama.cpp.
3. Choose [TUI, text, JSON or RPC](handbook/choose-a-mode.md).
4. Review [security and configuration](guides/configuration-security.md) before
   letting tools act on your files.

## Find the right manual

- **Use ONE:** [TUI and slash commands](handbook/choose-a-mode.md),
  [sessions and models](handbook/sessions-and-configuration.md),
  [images](handbook/images.md), [checkpoints](library/checkpoints.md).
- **Configure:** [providers](handbook/providers-and-authentication.md),
  [settings inventory](reference/settings.md), [run budgets](library/runs.md),
  [context and compaction](library/compaction.md).
- **Extend:** [MCP](handbook/integrations.md), [skills](library/skills.md),
  [extensions](library/extensions.md), [presets](library/presets.md).
- **Automate:** [CLI inventory](reference/cli.md), [tool schemas](reference/tools.md),
  [one run and RPC](guides/automation-rpc.md).
- **Troubleshoot:** [FAQ](library/faq.md), [Doctor](library/doctor.md).
- **Develop:** [contributing and testing](handbook/development.md),
  [workflow and runtime boundary](guides/developer-workflow.md),
  [architecture](library/architecture.md), [release checklist](library/release_checklist.md).

## Stability and platform limits

ONE is alpha. APIs, storage and tool contracts can change. Linux is the primary
platform; Windows is best-effort, without Windows CI. POSIX shell examples do not
imply Windows support. Experimental compaction V2, native replay and implementation
phase reports are described separately in the in-depth chapters; offline tests
are not evidence of production provider support.

The reference inventories are generated from code. Their introductions explicitly
identify semantic gaps: they are not yet a complete field-by-field contract audit.
See the [migration and audit report](maintainers/migration.md) before publication.
