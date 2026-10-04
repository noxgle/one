---
name: explore
description: Read-only repository exploration for locating code, understanding unfamiliar modules, tracing control/data flow, and gathering evidence before implementation. Can be used directly or spawned as a dedicated subagent. Use when repository context is incomplete or needs verification. Never modify state.
---

# Explore

## Purpose

`explore` is a read-only codebase investigation skill.

It can run:

- directly in the current agent,
- as a dedicated `explore` subagent.

Use it to understand existing code before implementation, debugging, refactoring, review, or other state-changing work.

The explorer gathers evidence and returns findings. It does not implement changes.

---

## When to Use

Use `explore` when you need to:

- locate definitions, usages, endpoints, config, or ENV variables,
- understand a file, function, class, module, or subsystem,
- map an unfamiliar repository,
- trace control flow or data flow,
- identify callers, dependencies, registrations, or integration points,
- gather context before debugging, refactoring, or feature work,
- perform focused static reconnaissance.

Do not use it automatically when the required context is already known.

Rule:

> Use `explore` when additional investigation is likely to reduce meaningful uncertainty.

---

# Direct vs Subagent

Use exploration directly for small, local questions.

Spawn an `explore` subagent when investigation is broader, cross-module, or worth delegating.

Example:

```text
spawn_subagent(
  task: """
Use the explore skill.

Trace Mode: trace POST /api/orders from route registration
to database persistence.

Remain strictly read-only.
Return concise findings with path:line evidence.
""",
  tools: ["read", "grep", "find", "ls", "bash"]
)
```

The coordinating agent owns implementation and final decisions.

The explore subagent owns investigation and evidence collection.

---

# Core Rules

## Read-Only

Never modify:

- files,
- repository state,
- dependencies,
- databases,
- infrastructure,
- environment configuration,
- external systems.

Forbidden examples:

```text
write
edit
apply_patch
rm
mv
git commit
git push
install
upgrade
migrate
deploy
build
code generation
```

If a command may have side effects, do not run it.

---

## Minimal Scope

Start with the smallest scope likely to answer the question.

Preferred progression:

```text
symbol
→ file
→ module
→ subsystem
→ repository
```

Expand only when necessary.

Do not map the whole repository for a local question.

---

## Search Before Reading

Prefer:

```text
LOCATE
→ INSPECT
→ TRACE
→ VERIFY
→ REPORT
→ STOP
```

Use targeted search first, then inspect only relevant files or ranges.

Avoid reading large files or directories without a reason.

---

## Facts vs Inference

Clearly separate verified facts from interpretation.

Example:

```text
Fact:
AuthController.login() calls AuthService.authenticate().

Evidence:
src/auth/AuthController.ts:42-46

Inference:
AuthService appears to be the main authentication boundary.
```

Never present inference as confirmed behavior.

---

## Verify Important Flow

For tracing tasks, verify each meaningful hop.

Good:

```text
Confirmed:
Route
→ Controller
→ Service
→ Repository

Dynamic / unresolved:
Repository implementation selected through dependency injection.
```

Call out dynamic behavior such as:

- dependency injection,
- reflection,
- runtime imports,
- callbacks,
- event buses,
- generated code,
- framework routing.

---

# Allowed Tools

Typical read-only tools:

```text
read
grep
find
ls
plan
bash
```

Safe shell examples:

```text
cat
head
tail
sed -n
wc
tree
rg
grep
find
ls
stat
file
git status
git log
git show
git diff
git grep
git ls-files
```

Shell access is deny-by-default:

> Run only commands that are clearly non-mutating.

Do not run tests, builds, installers, migrations, project scripts, or similar commands unless the environment explicitly guarantees read-only isolation.

---

# Secrets

Never expose secret values.

If a possible secret is found, report only:

- location,
- type,
- why it appears sensitive.

Example:

```text
Potential hardcoded API credential:
config/client.ts:18

Value intentionally omitted.
```

Avoid opening entire `.env` or credential files when unnecessary.

---

# Exploration Modes

## Map Mode

Understand project structure.

```text
Map Mode:
Identify entrypoints, major directories, tech stack,
build/run configuration, and key modules.
```

---

## Find Mode

Locate definitions and meaningful usages.

```text
Find Mode:
Find where OrderProcessor is defined, instantiated,
registered, and called.

Return path:line evidence.
```

---

## Explain Mode

Explain behavior of a specific component.

```text
Explain Mode:
Explain OrderProcessor.

Include responsibility, dependencies, callers,
side effects, branches, and error handling.
```

---

## Trace Mode

Trace control or data flow.

```text
Trace Mode:
Trace POST /api/orders to database persistence.

Verify each meaningful hop.
Separate confirmed and dynamic flow.
```

---

## Audit Mode

Perform focused static reconnaissance.

Examples:

```text
security
dependencies
licenses
dead code
authorization
input validation
unsafe APIs
```

Audit findings are reconnaissance, not a completeness guarantee.

Do not claim that absence of findings proves absence of problems.

---

# Evidence

Prefer exact source evidence:

```text
src/foo.ts:42
src/foo.ts:42-51
```

For repository-level facts, references such as these are acceptable:

```text
package.json
pyproject.toml
Cargo.toml
git log
directory structure
```

Do not:

- fabricate line numbers,
- cite files you did not inspect,
- dump large irrelevant code blocks.

---

# Confidence

Use when useful:

```text
High
Medium
Low
```

- **High** — directly verified.
- **Medium** — mostly verified, with some dynamic/runtime uncertainty.
- **Low** — important parts cannot be resolved statically.

---

# Stop Condition

Stop when:

- the original question is answered,
- the relevant flow is sufficiently verified,
- further exploration is unlikely to change the conclusion,
- remaining uncertainty requires runtime information or unavailable context.

Do not continue exploring merely because more code exists.

---

# Report Format

```markdown
## Explore Report — <Mode>

**Question:** <question investigated>
**Scope:** <scope>
**Execution:** Direct | Subagent
**Confidence:** High | Medium | Low

### Answer

- <direct conclusion>
- <key findings>

### Evidence

1. **<Finding>**
   - Fact: <verified fact>
   - Evidence: `path/file.ext:42-51`
   - Inference: <optional>

### Relevant Flow

<only when relevant>

A
→ B
→ C

### Unknowns

- <unresolved items>

### Suggested Next Step

- <optional>
```

---

# Behavioral Contract

`explore` may run directly or as a read-only subagent.

It should:

```text
LOCATE
→ INSPECT
→ TRACE
→ VERIFY
→ REPORT
→ STOP
```

It must not:

```text
GUESS
→ MODIFY
→ IMPLEMENT
→ EXPAND SCOPE WITHOUT REASON
→ DUMP RAW OUTPUT
→ KEEP EXPLORING AFTER THE QUESTION IS ANSWERED
```

Its goal is to return the smallest useful set of verified facts needed for the coordinating agent to make the next decision.
