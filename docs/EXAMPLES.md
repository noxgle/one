# Prompt examples

These prompts are deliberately explicit about scope and safety. Replace the
paths and constraints with the context of your project.

## Understand a repository

```text
Inspect this repository without editing files. Summarize the architecture,
entry points, test commands, and the five files I should read first. Cite paths
for every claim.
```

## Review a change

```text
Review the current git diff for correctness, regressions, security issues, and
missing tests. Do not modify files. Report findings by severity and include
file paths and line numbers.
```

## Debug a failing test

```text
Investigate why tests/test_auth.py::test_login_status fails. First inspect the
relevant code and test, then run only focused, safe tests. Do not change files.
Explain the root cause and propose the smallest fix.
```

## Implement a focused feature

```text
Implement [feature] in [file/module]. Before editing, inspect the existing
pattern and related tests. Keep the change minimal, preserve public behavior,
add focused tests, and run the relevant test plus lint. Summarize every changed
file.
```

## Refactor safely

```text
Refactor [module] to remove [duplication/problem]. Preserve the public API and
behavior. Do not broaden the scope. Show the plan first, then edit, run focused
tests, and report any remaining risk.
```

## Documentation

```text
Read the implementation and update the documentation so it matches actual
behavior. Do not change code. Add one copy-and-paste example and call out any
provider, platform, or security limitations.
```

## Research or comparison

```text
Compare [options] for this project using only the available documentation and
repository evidence. Separate confirmed facts from recommendations, list
trade-offs, and end with a short recommendation.
```

## Work with an image

```text
Inspect the attached screenshot. Describe the visible error, identify likely
UI elements involved, and propose a debugging checklist. Do not edit files or
claim certainty where the screenshot is ambiguous.
```

## Small release checklist

```text
Prepare a release-readiness report without changing files. Check the working
tree, version consistency, documentation links, tests, lint, and git diff.
Separate blocking issues from follow-ups and include the exact verification
commands.
```

## Prompting tips

- State whether the agent may edit files, run commands, or only inspect.
- Name the expected files, tests, and acceptance criteria.
- Ask for a plan before a multi-file change.
- Prefer focused tasks over “fix everything”.
- Ask for evidence: paths, test names, and command output summaries.
- Do not include secrets, access tokens, or private customer data.
