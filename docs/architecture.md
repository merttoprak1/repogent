# Architecture

Repogent is a synchronous, artifact-first workflow. Model roles produce typed proposals; deterministic services alone inspect files, validate paths, apply patches, select commands, execute checks, enforce budgets, and change workflow state.

## Runtime flow

Request → inspect and retrieve → requirements approval → plan approval → patch policy → patch approval → transactional apply → deterministic validation → at most two approved repairs → independent QA → final report.

## Boundaries

- `domain.py`: versioned contracts and status enums.
- `repository.py`: confined and aggregate-bounded traversal, sensitive-path exclusion, AST metadata, and lexical ranking.
- `providers.py`, `agents.py`, and `sanitization.py`: deadline-bounded schema generation, final-boundary recursive redaction, and untrusted-content prompts.
- `approvals.py`: requirements, plan, patch, and repair decisions.
- `patching.py`: default-deny unified-diff validation and transactional application.
- `execution.py` and `validation.py`: fixed commands through Docker or explicit local fallback.
- `workflow.py`: legal transitions, budgets, repairs, and terminal outcomes.
- `artifacts.py` and `reporting.py`: structurally sanitized JSON evidence and final reports.

## Terminal statuses

- `completed`: validation passed and QA approved.
- `completed_with_findings`: validation passed and QA reported non-blocking findings.
- `changes_requested`: validation passed but QA found blocking issues.
- `cancelled`: a human rejected an approval gate.
- `human_intervention_required`: policy, provider, timeout, budget, or repair limits stopped the run.

## Evidence

Each external run directory contains `run.json`, `report.md`, versioned inventories and context, role outputs, approvals, diffs, validation output, provider usage, repairs, and QA results. `run.json` is replaced atomically; stage artifacts are append-only.
