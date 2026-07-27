---
name: repository-readiness
description: Use when the user wants a read-only Repogent readiness diagnosis for a Python repository, including bounded scope, validation commands, provider availability, and executor options.
---

# Repogent repository readiness

Use this skill to explain whether a repository can enter a Repogent workflow and
what would block it. This is a read-only diagnostic capability.

## Hard boundary

You must not edit repository files, configuration, dependencies, credentials, or
host settings. You must not run target repository code. Do not install, start,
repair, or reconfigure anything automatically.

Call only `inspect_repository_readiness`. Do not begin a workflow or submit any
approval, executor selection, patch application, or cancellation.

## Procedure

1. Resolve exactly one repository root. Ask if the path is ambiguous.
2. Call `inspect_repository_readiness` with the intended provider and model and
   with `executor="deferred"`.
3. Report `READY` when all required base checks pass; otherwise report `BLOCKED`.
4. Summarize the Git-bounded scope: selected file count, selected bytes, scope
   source, and skipped files. Explain a scope-limit failure as a bounded-input
   safety failure, not as a claim that every file is source code.
5. Separate required base checks from optional executor availability. Missing
   Docker is an unavailable isolation option, not a base-readiness blocker when
   the executor is deferred.
6. Give the exact remediation supplied by the diagnostic. Describe actions the
   user can take, but do not perform them.

Keep the result factual. Do not claim that tests ran, that a patch exists, or
that the repository is validated. Readiness proves only that a later workflow
may start.
