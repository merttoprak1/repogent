# Architecture

Repogent is a synchronous, artifact-first local workflow for conventional Python libraries, command-line packages, data transforms, and the bundled FastAPI web-service MVP. Model roles produce typed proposals; deterministic services alone inspect files, validate paths, apply patches, select commands, execute checks, enforce budgets, and change workflow state.

## Runtime flow

The detailed state machine is preserved in `RunStage`. The following are conceptual/user-facing phases, not literal emitted stage labels: **Understand → Localize → Propose → Validate → Decide**.

1. **Understand:** preflight, bounded inventory, then typed requirements/specification generation and its approval gate.
2. **Localize:** graph construction and localization after requirements are generated.
3. **Propose:** planning after localization, plan approval, and candidate generation.
4. **Validate:** disposable-copy policy checks and deterministic candidate validation with retained evidence.
5. **Decide:** evidence selection or ambiguity escalation; after patch approval, a single real-checkout apply, isolated final validation, QA, and finalization.

`candidate-1` is the default low-risk path. Validation failure, incomplete acceptance coverage, high risk, a broad patch, or unresolved ambiguity may create a further candidate; the policy caps the sequence at `candidate-3`. Candidates with required failures cannot be selected. Equal evidence is explicitly ambiguous and ends in human intervention rather than a tie-break mutation.

## Boundaries

- `domain.py`: versioned contracts and status enums.
- `repository.py`, `symbols.py`, and `localization.py`: confined and aggregate-bounded traversal, sensitive-path exclusion, deterministic Python AST graph, and explainable hybrid localization.
- `providers.py`, `agents.py`, and `sanitization.py`: deadline-bounded schema generation, final-boundary recursive redaction, and untrusted-content prompts.
- `approvals.py`: requirements, plan, patch, and repair decisions.
- `patching.py`: default-deny unified-diff validation and transactional application.
- `preflight.py`: repository/executor readiness and repository/configuration fingerprints before provider construction.
- `execution.py` and `validation.py`: fixed commands through Docker by default or an explicit local fallback.
- `candidates.py` and `workflow.py`: isolated candidate transactions, legal transitions, bounded expansion, evidence selection, budgets, recovery, and terminal outcomes.
- `events.py`, `artifacts.py`, and `reporting.py`: monotonic event JSONL, structurally sanitized versioned evidence, and final reports.

## Terminal statuses

- `completed`: validation passed and QA approved.
- `completed_with_findings`: validation passed and QA reported non-blocking findings.
- `changes_requested`: validation passed but QA found blocking issues.
- `cancelled`: a human rejected an approval gate.
- `human_intervention_required`: policy, provider, timeout, budget, or repair limits stopped the run.

## Evidence

Each external run directory contains `run.json`, `events.jsonl`, `report.md`, inventory/graph/localization/context artifacts, role inputs and outputs, approvals, candidate records and evidence, selection, diffs, validation output, provider usage, repairs, and QA results. Versioned domain/model artifacts use `schema_version: "1"`; raw role-input JSON/text payloads are retained but are not versioned model envelopes. `run.json` atomically records fingerprints, candidate IDs, selected candidate, event linkage, and terminal state while stage artifacts remain append-only.

Failed preflight writes its report and manifest without constructing a provider. Candidate workspaces must restore to the captured baseline; failed recovery, required validation failures, ambiguous selection, root drift, changed final evidence, provider/budget/timeout errors, or persistence trouble all preserve evidence and end in an explicit terminal status.

Real-repository benchmarks, public packaging/release automation, headless CI policy, and GitHub issue/PR adapters intentionally follow this increment rather than introducing another workflow engine.
