# Task 2 Report: Bind executor consent to validation targets

## Status

Implemented the generic validation-target consent boundary while preserving the
bounded, sanitized patch preview shown to operators. New interactive contracts
use `target`; stored v0.3.x manifests containing `preview_digest` are accepted
and migrated to a PATCH `evaluated_target`, while new manifest serialization
does not re-emit `preview_digest`.

## Clarification and resolution

The brief conflicted with the actual API: `RunManifest` is defined in
`domain.py`, the existing gate has no `publish` method, and its `PatchPreview`
argument was the only source of the human-visible pending preview. Work paused
before implementation. The task owner resolved the migration as follows:

- define `ValidationTargetKind` and `ValidationTarget` in `domain.py`;
- preserve `PendingExecutionChoice.preview` as non-authoritative display data;
- use `select(target, preview, *, timeout_seconds)`;
- migrate legacy manifest `preview_digest` input to a PATCH `evaluated_target`;
- expand scope to the session and MCP server boundaries as required.

## RED evidence

1. Initial target models and binding tests:

   `PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest tests/unit/test_executor_selection.py tests/unit/test_execution_gate.py -q`

   Exit 2. Both modules failed collection with:
   `ImportError: cannot import name 'ValidationTarget' from 'repogent.domain'`.

2. Legacy manifest migration:

   `PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest tests/unit/test_domain.py::test_legacy_preview_digest_is_read_as_patch_target_but_not_reemitted -q --no-cov`

   Exit 1. `RunManifest` had no `evaluated_target` attribute.

3. Workflow selector migration:

   `PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest tests/unit/test_workflow.py::test_each_repair_gets_a_new_preview_and_executor_selection -q --no-cov`

   Exit 1. The old workflow call omitted the new `preview` positional argument.

4. Session boundary migration:

   `PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest tests/unit/test_run_sessions.py -q --no-cov`

   Exit 2. Collection failed because the old `ExecutionDecision` import no
   longer existed.

## GREEN evidence

- Executor/gate first cycle: 21 tests passed with `--no-cov`.
- Legacy manifest focused test: 1 passed with `--no-cov`.
- Workflow suite: 58 tests passed with `--no-cov`.
- Run-session suite: 43 tests passed with `--no-cov`.
- Execution-model/MCP-server suites: 48 tests passed with `--no-cov`.
- All unit tests passed with `--no-cov`.
- Plugin end-to-end integration: 14 tests passed with `--no-cov`.
- Final required focused command with `--no-cov`: 122 tests passed.
- Ruff on all changed Python files: `All checks passed!`.
- `git diff --check`: exit 0.

The brief's focused command without `--no-cov` ran all 122 tests successfully
but exited 1 solely because subset coverage was 65.59%, below the repository's
global 85% threshold. This is the expected focused-test harness limitation.

## Full-suite result

One full-suite attempt was made after the initial unit migration. It terminated
during collection because `tests/integration/test_plugin_end_to_end.py` still
imported `ExecutionDecision`. That integration contract was migrated and its
entire 14-test file then passed. Per the parent task's bounded-run direction, no
second unbounded full-suite run was started; the full-suite result is therefore
incomplete rather than claimed green.

## Compatibility behavior

- `ValidationTargetKind` supports `patch`, `diff`, and `commit`.
- Option digests include the canonical JSON target, including target kind.
- Pending choices and validation decisions bind authority to the exact target
  kind and digest; stale target and option digests are rejected.
- The pending preview remains recursively sanitized, bounded, and human-visible,
  but is not the authority token.
- New MCP schemas expose `ValidationDecision.target` and
  `PendingExecutionChoice.target`; they do not accept the old interactive
  `preview_digest` shape.
- `RunManifest` accepts stored v0.3.x `preview_digest` input, converts it to a
  PATCH `evaluated_target`, and serializes only `evaluated_target`.
- Verified-change clears executor mode, isolation, and verification consent when
  publishing a current target, persists that target before exposing candidate
  evidence, and restores the selected candidate's target.

## Files changed

Production: `domain.py`, `mcp_models.py`, `executor_selection.py`,
`execution_gate.py`, `workflow.py`, `run_sessions.py`, `mcp_server.py`, and
`reporting.py`.

Tests: executor selection, execution gate, workflow, run sessions, domain,
execution models, MCP server, candidate preview, reporting, and the affected
end-to-end integration contracts.

The pre-existing modified roadmap plan was intentionally not staged.

## Self-review

- Authority is derived only from `ValidationTarget`; preview data never feeds an
  option digest or decision comparison.
- Target objects passed to selectors are deep copies and are checked after the
  call, retaining the prior mutation-defense property.
- Legacy aliasing is isolated to the persisted manifest before-validator; no
  permissive alias was added to live MCP requests.
- Error wording remains untyped in this task; Task 3 was not started.
- No report envelope or capability result work from Task 4 was introduced.

## Commits

- `e37e85f feat: bind executor consent to validation targets` — implementation,
  tests, and initial evidence report.
- A report-only finalization commit records the implementation hash above; see
  the task handoff for that commit hash.

## Concerns

The canonical full suite did not complete after the final integration fixture
migration. Focused, all-unit, and affected integration evidence is green, but a
later release gate should run the complete suite with the global coverage
threshold.

## Fix round 1: selector preview integrity

Review found that the workflow passed a detached mutable preview dictionary to
the selector but verified only the untouched original `PatchPreview` after the
selector returned. A selector could therefore mutate nested human-visible
preview content without invalidating consent.

The workflow now deep-copies the exact dictionary supplied to the selector and
compares that dictionary with its immutable baseline after return. The existing
deep-copied target comparison remains independent and unchanged.

### RED evidence

`PYTHONPATH=src /Users/mert/Documents/Repogent/.venv/bin/python -m pytest tests/unit/test_workflow.py::test_nested_selector_preview_mutation_fails_before_candidate_evaluation -q --no-cov`

Exit 1. The workflow continued past executor selection and eventually reported
`changed validation evidence` instead of rejecting the nested preview mutation
at the consent boundary.

### GREEN evidence

- Nested preview mutation and target mutation regressions: 2 passed.
- Same digest with a different `ValidationTargetKind`: 1 passed; the pending
  choice remained usable for the correct target afterward.
- Covering focused suite: 124 passed with `--no-cov`.
- Ruff for the three changed Python files: `All checks passed!`.
- `git diff --check`: exit 0.

### Files changed

- `src/repogent/workflow.py`
- `tests/unit/test_workflow.py`
- `tests/unit/test_execution_gate.py`
- this evidence report

### Self-review

- Preview equality is checked against a deep copy of the exact mutable object
  passed across the selector boundary, including nested values.
- Target authority still uses the independent exact-target comparison.
- The original `PatchPreview` digest check remains as an additional binding
  defense.
- No Task 3 typed-error or Task 4 report-envelope behavior was introduced.
