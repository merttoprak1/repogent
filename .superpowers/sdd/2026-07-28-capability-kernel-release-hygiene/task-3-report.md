# Task 3 Report: Typed Errors and Capability-Aware Session Operations

## Status

Implemented and committed as `9481692` (`feat: enforce typed capability operations`).

The implementation adds the stable typed error contract, enforces the existing
`CapabilityRegistry` before mutation-gate delegation, converts the required
session boundaries to typed errors, and preserves the existing allowlisted
generic MCP error behavior for unexpected exceptions.

## RED evidence

Tests were written before production changes. The required first run was:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest \
  tests/unit/test_errors.py tests/unit/test_run_sessions.py \
  tests/unit/test_mcp_server.py -q
```

It failed during collection in all three requested modules because the new
contract did not exist:

```text
ModuleNotFoundError: No module named 'repogent.errors'
ERROR tests/unit/test_errors.py
ERROR tests/unit/test_run_sessions.py
ERROR tests/unit/test_mcp_server.py
Interrupted: 3 errors during collection
```

The RED batch covered:

- exact versioned `ErrorDetail` JSON values and field bounds;
- `RepogentError.detail`;
- wrong-operation rejection for review runs;
- the mutation policy matrix for all six `WorkflowKind` values using test-only
  sessions, with no future workflow production implementation;
- exact approval-gate-to-`RunOperation` mapping and independent executor
  selection mapping through real `CapabilityRegistry` definitions;
- typed unknown-run, stale approval digest, stale validation-target/option
  digest, and executor-unavailable boundaries; and
- sanitized typed MCP errors with no raw exception text.

## GREEN evidence

Focused service and MCP verification after implementation and formatting:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest \
  tests/unit/test_errors.py tests/unit/test_run_sessions.py \
  tests/unit/test_mcp_server.py -q --no-cov
```

Result: exit `0`, all focused tests passed.

The first GREEN attempt found two issues: one expected assertion still required
the legacy `SessionError`, and capability-checking the universally allowed
`CANCEL` operation required a snapshot that could block during terminal
publication. The final implementation keeps capability checks on the specified
mutation gates (`decide` and `select_executor`), preserving the existing
cancellation and read timing guarantees. The focused suite then passed.

The single requested full-suite attempt was:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest -q
```

Result: exit `0`; expected skips only; coverage `90.67%`, above the configured
`85%` threshold. The harness completed, so there is no incomplete run to report.

Final scoped static verification:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m ruff check \
  src/repogent/errors.py src/repogent/run_sessions.py \
  src/repogent/mcp_server.py tests/unit/test_errors.py \
  tests/unit/test_run_sessions.py tests/unit/test_mcp_server.py
# All checks passed!

PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m ruff format \
  --check src/repogent/errors.py src/repogent/run_sessions.py \
  src/repogent/mcp_server.py tests/unit/test_errors.py \
  tests/unit/test_run_sessions.py tests/unit/test_mcp_server.py
# 6 files already formatted

PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m mypy \
  src/repogent/errors.py src/repogent/run_sessions.py src/repogent/mcp_server.py
# Success: no issues found in 3 source files
```

`git diff --cached --check` also passed before the implementation commit.

## Error contract

`src/repogent/errors.py` defines the exact requested values:

- `ErrorCode`: `invalid_input`, `unknown_run`, `operation_not_allowed`,
  `stale_digest`, `limit_exceeded`, `provider_unavailable`,
  `executor_unavailable`, `validation_failed`, `policy_error`, and
  `internal_error`;
- `RetryClass`: `read_only`, `reconcile_first`, and `non_retryable`;
- bounded, versioned `ErrorDetail`; and
- `RepogentError`, whose exception message is only the safe public detail
  message.

Boundary mappings are:

| Boundary | Code | Retry class |
| --- | --- | --- |
| Unknown run ID | `unknown_run` | `non_retryable` |
| Capability-denied operation | `operation_not_allowed` | `non_retryable` |
| Stale approval/validation digest | `stale_digest` | `reconcile_first` |
| Other invalid approval state | `policy_error` | `reconcile_first` |
| Executor selection unavailable/current-state mismatch | `executor_unavailable` | `reconcile_first` |

All public messages and remediations are fixed allowlisted strings. Raw caught
exception text is used only to distinguish the internal digest-mismatch branch
and is never copied into `ErrorDetail`.

## Exact MCP error representation

Installed MCP version `1.28.1` was inspected before choosing the mechanism.
FastMCP's supported tool exception is
`mcp.server.fastmcp.exceptions.ToolError`; it accepts only a string. Its tool
wrapper converts that exception into a standard tool error result. A direct
in-memory probe confirmed the behavior.

`_call_service` now sanitizes `error.detail.model_dump(mode="json")`, validates
the sanitized object back through `ErrorDetail`, compact-serializes it with
sorted keys, and raises `ToolError`. The observable contract is:

```json
{
  "isError": true,
  "structuredContent": null,
  "content": [
    {
      "type": "text",
      "text": "Error executing tool get_run: {\"code\":\"operation_not_allowed\",\"message\":\"This operation is not allowed for the run kind.\",\"remediation\":\"Use an operation supported by the run capability.\",\"retry\":\"non_retryable\",\"run_id\":\"run-1\",\"run_kind\":\"patch_review\",\"schema_version\":\"1\"}"
    }
  ]
}
```

No unsupported `structuredContent` emulation or low-level FastMCP internals are
used. Contract tests parse the JSON suffix, assert `code`, `retry`, kind, and
schema version, and prove that raw exception text, a private path, and secret
markers are absent.

Unexpected exceptions still produce the existing exact shape and allowlisted
category text:

```text
Error executing tool <tool_name>: <existing allowlisted generic message>
```

## Capability dispatch

`SessionManager.__init__` accepts
`registry: CapabilityRegistry = CapabilityRegistry.defaults()` and preserves an
injected registry directly. `decide` maps requirements, plan, and patch gates to
`APPROVE_REQUIREMENTS`, `APPROVE_PLAN`, and `APPLY_PATCH`; `select_executor`
maps to `SELECT_EXECUTOR`. The manager resolves the session snapshot kind and
calls `registry.require` before delegating.

The six-kind matrix uses `CapabilitySession`, a test-only session double. No
patch-review, CI-triage, dependency-update, security-fix, or release-gate
workflow production code was created.

## Changed files

- `src/repogent/errors.py` — new typed error and retry contract.
- `src/repogent/run_sessions.py` — typed boundary conversion and capability-aware
  mutation dispatch.
- `src/repogent/mcp_server.py` — sanitized supported `ToolError` mapping while
  preserving generic unexpected failures.
- `tests/unit/test_errors.py` — stable serialization, bounds, and exception
  contract.
- `tests/unit/test_run_sessions.py` — all-kind capability matrix, exact operation
  mapping, unknown/stale/executor boundary tests, and compatibility regression.
- `tests/unit/test_mcp_server.py` — exact MCP representation and sanitization
  contract.

`src/repogent/mcp_models.py` required no production change: MCP 1.28.1 represents
tool failures as error `TextContent`, not typed tool `structuredContent`, and the
shared transport model lives in the requested `errors.py`. Adding an unused
response model or unsupported output-schema branch would misrepresent the
actual FastMCP API.

## Compatibility and self-review

- Intentional exception-type change: unknown-run, stale-decision,
  executor-selection, and capability-policy boundaries now raise
  `RepogentError` instead of `SessionError`.
- Existing `SessionError` behavior remains for unrelated session lifecycle,
  report, cancellation, construction, and security failures.
- Unexpected service exceptions preserve every prior bounded generic MCP
  message; the existing exhaustive nine-tool test passes unchanged.
- Read and cancellation paths do not take a new snapshot solely for capability
  policy, avoiding the terminal-publication deadlock found by the first GREEN
  run.
- Error JSON is bounded by `ErrorDetail`, recursively sanitized, validated after
  sanitization, deterministic, and contains no raw exception message.
- Mutation check: removing any gate mapping, swapping a `RunOperation`, allowing
  a read-only workflow, returning raw typed exception text, changing retry
  classes, or falling back to generic text for `RepogentError` fails at least
  one focused test.
- Pre-existing unstaged changes to
  `docs/superpowers/plans/2026-07-28-capability-kernel-release-hygiene.md` were
  preserved and excluded from the implementation commit.

## Commits

- Capability registry prerequisite: `a9cf2ed` (`feat: define capability operation policy`).
- Validation-target prerequisite through: `65e5b1d` (`fix: preserve executor preview consent binding`).
- Task 3 implementation: `9481692` (`feat: enforce typed capability operations`).
- This report is committed separately after the implementation so it can record
  the implementation hash.

## Concerns

- FastMCP 1.28.1 has no supported structured error-data channel for decorated
  typed tools. Consumers must remove the standard
  `Error executing tool <name>: ` prefix and parse the compact JSON suffix. This
  exact TextContent representation is locked by the MCP contract test.
- Digest classification currently recognizes the stable internal gate message
  containing `digest`. If lower-level gate exceptions become typed in a future
  task, this branch should switch to their typed discriminator and retain the
  same public `ErrorDetail` values.
