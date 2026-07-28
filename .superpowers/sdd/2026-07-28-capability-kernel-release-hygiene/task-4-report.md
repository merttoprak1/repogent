# Task 4 Report: Common Persistent Run Report Envelope

## Status

Implemented as `da3c266` (`feat: type persistent capability reports`).

The implementation adds a versioned typed `PersistentRunReport`, persists a
validated `report.json` beside `report.md`, renders Markdown from the same
envelope instance, and changes MCP `RunReport` to return typed data plus bounded
Markdown.

## Contract decision

The plan declared `PersistentRunReport.outcome` as required, but the existing
verified-change workflow durably reports cancelled runs whose
`RunManifest.outcome` is `None`. Inventing an outcome would misstate the run and
making the field required would break the existing cancellation/report
contract. With controller approval, the implementation uses
`WorkflowOutcome | None` and includes an explicit cancelled-report regression.
All non-cancelled terminal outcomes retain the existing capability validation.

No duplicate domain types were introduced. Task 2 had already placed
`ValidationTarget` and the required status/trust enums in `domain.py`, so that
file needed no Task 4 production edit.

## RED evidence

The first envelope tests were written before `run_reports.py` existed:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest \
  tests/unit/test_run_reports.py tests/unit/test_reporting.py -q --no-cov
```

Expected RED result:

```text
ModuleNotFoundError: No module named 'repogent.run_reports'
Interrupted: 1 error during collection
```

After the model became green, tests for the report renderer and persistent
session wrapper were added before those integrations. The focused run failed
with the expected missing API:

```text
TypeError: render_report() got an unexpected keyword argument 'evidence_path'
AttributeError: 'RunReport' object has no attribute 'data'
2 failed
```

The durable reader's failure behavior received its own RED cycle. Before the
error mapping and terminal-state comparison were installed, malformed JSON
leaked a Pydantic `ValidationError` and a valid report for a different run was
accepted:

```text
FAILED test_report_rejects_invalid_persistent_json
FAILED test_report_rejects_persistent_data_for_different_run
2 failed
```

Each failure was caused by the named missing production behavior rather than a
fixture or mock assertion.

## GREEN evidence

Required focused regression:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest \
  tests/unit/test_run_reports.py tests/unit/test_reporting.py \
  tests/unit/test_run_sessions.py tests/integration/test_plugin_end_to_end.py \
  -q --no-cov
```

Result: exit `0`; 96 tests passed.

The MCP wrapper migration and its existing bounds/redaction contract were also
run explicitly:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest \
  tests/unit/test_domain.py tests/unit/test_mcp_server.py -q --no-cov
```

Result: exit `0`; 80 tests passed.

Full regression:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest -q --no-cov
```

Result: completed at 100% with expected skips and no failures. No verification
command was left incomplete.

Static verification:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m ruff check \
  src/repogent/run_reports.py src/repogent/reporting.py \
  src/repogent/mcp_models.py src/repogent/run_sessions.py \
  src/repogent/workflow.py src/repogent/run_builder.py \
  tests/unit/test_run_reports.py tests/unit/test_reporting.py \
  tests/unit/test_run_sessions.py tests/unit/test_domain.py \
  tests/unit/test_mcp_server.py tests/integration/test_plugin_end_to_end.py
# All checks passed!

PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m mypy \
  --strict src/repogent
# Success: no issues found in 33 source files

PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m ruff format \
  --check src/repogent/run_reports.py tests/unit/test_run_reports.py
# 2 files already formatted
```

`git diff --check` passed before the implementation commit.

## Persistent envelope

`src/repogent/run_reports.py` defines:

- `CheckSummary` with required, passed, failed, and skipped check names;
- `VerifiedChangeResult` with its literal capability discriminator, selected
  candidate, bounded applied paths, and final validation status;
- the initial `CapabilityResult = VerifiedChangeResult` alias, ready to become
  a discriminated union when patch review lands; and
- `PersistentRunReport` with the versioned common fields, typed errors,
  evidence path, and capability result.

The model rejects a result whose discriminator differs from the report kind.
It also rejects a `checkout_changed` value that contradicts durable
`CheckoutState`. The builder derives the fact only from that state:
`NOT_APPLIED` is false; `APPLIED` and safety-conservative
`RECOVERY_UNKNOWN` are true. It never derives checkout mutation from the legacy
`selected_patch_applied` flag.

`CheckStatus.TIMED_OUT` is classified as failed. Required membership is
independent of pass/fail/skip classification, preserving whether a skipped or
failed check was mandatory.

## Persistence and rendering

Normal workflow completion and pre-workflow terminal failure both build one
validated envelope, write its sanitized versioned JSON to `report.json`, and
pass the same object to `render_persistent_report` for `report.md`. Existing
direct `render_report` callers remain compatible; that function constructs the
envelope internally before rendering.

The common Markdown header precedes verified-change-specific details and
includes kind, outcome, exact evaluated target, checkout fact/state, trust
label, all four check summaries, and evidence path. Existing requirements,
planning, localization, candidate, deterministic validation, QA, cost, and
recovery sections remain intact.

## Secure retrieval and MCP shape

`RunSession` reads both final files through the existing descriptor-relative,
no-follow, regular-file and identity checks. Markdown remains bounded to 64,000
characters. Persistent JSON is prefix-bounded to 1,000,000 characters before
Pydantic validation.

`SessionManager.get_report` rejects malformed JSON and validates run ID, kind,
status, outcome, checkout state, and evidence path against the terminal
snapshot. It returns:

```text
RunReport(data=PersistentRunReport(...), markdown=<bounded redacted text>)
```

The MCP schema therefore exposes typed persistent data rather than duplicating
selected envelope fields around a Markdown-only payload. Existing recursive
MCP sanitization and output-schema tests pass with the new shape.

## Changed files

- `src/repogent/run_reports.py` — typed envelope, result, check summary, and
  builder.
- `src/repogent/reporting.py` — common-envelope header and same-object Markdown
  renderer.
- `src/repogent/workflow.py` — normal terminal JSON and Markdown persistence.
- `src/repogent/run_builder.py` — early terminal-failure JSON and Markdown
  persistence.
- `src/repogent/run_sessions.py` — secure JSON read, validation, reconciliation,
  and typed retrieval.
- `src/repogent/mcp_models.py` — typed data plus bounded Markdown wrapper.
- `tests/unit/test_run_reports.py` — envelope construction, kind mismatch,
  durable checkout fact, check classification, and cancellation contract.
- `tests/unit/test_reporting.py` — common-before-capability Markdown behavior.
- `tests/unit/test_run_sessions.py` — persistence, bounds, malformed JSON, and
  terminal mismatch behavior.
- `tests/unit/test_domain.py` and `tests/unit/test_mcp_server.py` — wrapper
  schema, bounds, structured content, and redaction compatibility.

The pre-existing unstaged plan change remains untouched and is excluded from
both Task 4 commits.

## Self-review

- Mutation check: changing result kind, deriving checkout mutation from the
  legacy flag, dropping a check bucket, rendering capability details before the
  common header, omitting `report.json`, accepting malformed/mismatched JSON,
  reading either artifact without a prefix bound, or exposing unredacted
  Markdown fails a focused test.
- The wrapper schema change is intentional and required by Task 4; old
  top-level duplicated report metadata is now under `data`, while human text is
  explicitly named `markdown`.
- Existing approval, executor consent, checkout recovery, validation, trust,
  sanitization, and terminalization semantics are preserved.
- Typed `errors` is present but remains empty for verified-change because the
  existing manifest stores only a bounded human reason, not durable
  `ErrorDetail` objects. No untyped reason was promoted into a false typed
  error.

## Commits

- Task 4 implementation: `da3c266` (`feat: type persistent capability reports`).
- This evidence report is committed separately so it can record the exact
  implementation hash.

## Review fix 3/3: typed terminal errors

Final review identified that the original envelope declared `errors` but never
populated it. As a result, provider, validation, policy, timeout, and checkout
recovery failures persisted as an empty list despite otherwise having safe
terminal evidence.

Implemented in `b2c729e` (`fix: persist typed terminal report errors`):

- `PersistentRunReport.errors` is bounded to ten `ErrorDetail` values.
- `build_persistent_report` accepts trusted typed error input and otherwise
  derives one safe error from durable failure state. It classifies provider,
  required validation, policy, execution-limit, and recovery-unknown failures.
- Mapping uses fixed public messages and remediations only; it never puts a
  terminal exception or `manifest.reason` into `ErrorDetail`. Input messages
  are redacted again and run ID/kind are rebound to the report manifest.
- `Workflow` carries a provider failure as a direct typed input, retaining its
  retry class without relying on exception text. This covers provider messages
  such as authentication failures that do not contain the word “provider”.
- Successful and cancelled reports retain `errors: []`.

TDD RED evidence:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest \
  tests/unit/test_run_reports.py \
  tests/unit/test_workflow.py::test_non_retryable_provider_failure_writes_evidence_and_requires_human \
  -q --no-cov
```

Result: six expected failures. The five derived terminal categories all had an
empty errors list, and the durable provider-failure report contained `[]`.

GREEN verification:

```text
PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest \
  tests/unit/test_run_reports.py tests/unit/test_reporting.py \
  tests/unit/test_workflow.py tests/unit/test_run_sessions.py -q --no-cov
# 147 passed

PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m ruff check \
  src/repogent/run_reports.py src/repogent/workflow.py \
  tests/unit/test_run_reports.py tests/unit/test_workflow.py
# All checks passed!

PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m ruff format \
  --check src/repogent/run_reports.py src/repogent/workflow.py \
  tests/unit/test_run_reports.py tests/unit/test_workflow.py
# 4 files already formatted

PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m mypy --strict src/repogent
# Success: no issues found in 33 source files
```
