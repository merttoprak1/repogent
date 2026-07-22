# Architecture

Repogent is a synchronous, artifact-first local workflow for conventional Python libraries, command-line packages, data transforms, and the bundled FastAPI web-service MVP. Model roles produce typed proposals; deterministic services alone inspect files, validate paths, apply patches, select commands, execute checks, enforce budgets, and change workflow state.

## Codex plugin adapter

The repository-local Codex plugin adds a thin chat-facing adapter around the
same workflow. Codex loads the Repogent skill and starts `repogent mcp --stdio`
as a local child process. Typed MCP tools translate chat requests and decisions
to `DoctorService` and `SessionManager`; they do not duplicate workflow policy,
patch validation, execution policy, or state transitions. The plugin is not a
second policy engine.

The stdio process, the local Repogent installation, and its operator-visible
evidence directory form one local trust boundary. A `SessionManager` owns all
runs for that process and allows only one active run for a canonical repository
root. Each requirements, plan, and patch response carries the exact pending
artifact and digest; the corresponding tool advances only a matching gate.
When the client disconnects, the MCP lifespan finalizer cooperatively cancels
active work, closes pending approvals, waits within the bounded shutdown
deadline, and preserves the resulting manifest and report. A later client must
inspect persisted evidence rather than assuming a disconnected mutation did or
did not occur.

## Runtime flow

The detailed state machine is preserved in `RunStage`. The following are conceptual/user-facing phases, not literal emitted stage labels: **Understand → Localize → Propose → Validate → Decide**.

1. **Understand:** preflight, bounded inventory, deterministic Python graph construction, then typed requirements/specification generation and its approval gate.
2. **Localize:** consume the already-built graph after requirements and acceptance criteria are known.
3. **Propose:** planning after localization, plan approval, and candidate generation.
4. **Validate:** disposable-copy policy checks and deterministic candidate validation with retained evidence.
5. **Decide:** evidence selection or ambiguity escalation; after patch approval, a single real-checkout apply, isolated final validation, QA, and finalization.

`candidate-1` is the default low-risk path. Validation failure, incomplete acceptance coverage, high risk, a broad patch, or unresolved ambiguity may create a further candidate; the policy caps the sequence at `candidate-3`. Candidates with required failures cannot be selected. Equal evidence is explicitly ambiguous and ends in human intervention rather than a tie-break mutation.

## Boundaries

- `domain.py`: versioned contracts and status enums.
- `repository.py`, `symbols.py`, and `localization.py`: confined and aggregate-bounded traversal, sensitive-path exclusion, source-root-aware deterministic Python AST graph, and explainable hybrid localization.
- `provider_context.py`, `providers.py`, `agents.py`, and `sanitization.py`: metadata-only inventory DTOs, top-ranked complete-line snippets, globally allocated structured context, capped failure summaries, deadline-bounded schema generation, final-boundary recursive redaction, and untrusted-content prompts.
- `codex_cli.py`: an interchangeable, local Codex CLI proposal provider. It verifies executable/capability/login readiness before workflow construction, makes one structured `codex exec` call for each model role, validates the typed result, and returns typed readiness and per-call evidence. Its provider-owned temporary work directory and Codex read-only sandbox are a practical boundary, not strict OS-enforced isolation of the host or target repository.
- `approvals.py`: requirements, plan, patch, and repair decisions.
- `patching.py`: default-deny unified-diff validation and transactional application.
- `preflight.py`: repository/executor and per-command readiness plus repository/configuration fingerprints before provider construction, with fail-closed pytest discovery and no-follow, regular-file-only configuration reads.
- `execution.py` and `validation.py`: fixed commands through Docker by default or an explicit local fallback; Docker readiness probes the actual tool inside the configured image and caches the result.
- `candidates.py` and `workflow.py`: isolated candidate transactions, legal transitions, bounded expansion, evidence selection, budgets, recovery, and terminal outcomes.
- `events.py`, `artifacts.py`, and `reporting.py`: monotonic event JSONL, structurally sanitized versioned evidence, and final reports.
- `doctor.py`, `run_sessions.py`, `mcp_models.py`, and `mcp_server.py`: typed readiness and session services, canonical-root locking, digest-bound decisions, bounded report access, and the local stdio MCP adapter.

## Terminal statuses

- `completed`: validation passed and QA approved.
- `completed_with_findings`: validation passed and QA reported non-blocking findings.
- `changes_requested`: validation passed but QA found blocking issues.
- `cancelled`: a human rejected an approval gate or interrupted normal execution.
- `human_intervention_required`: policy, provider, timeout, budget, or repair limits stopped the run.

## Evidence

Each external run directory contains `run.json`, `events.jsonl`, `report.md`, inventory/graph/localization/context artifacts, role inputs and outputs, approvals, candidate records and evidence, selection, diffs, validation output, and, as applicable, provider readiness; successful-call usage and per-call evidence; or provider-failure evidence, plus repairs and QA results. Versioned domain/model artifacts use `schema_version: "1"`; raw role-input JSON/text payloads are retained but are not versioned model envelopes. `run.json` atomically records fingerprints, candidate IDs, selected candidate, typed checkout state (`not_applied`, `applied`, or `recovery_unknown`), applied paths, final-validation state, recovery guidance, generated-but-not-consumed artifacts, event linkage, and terminal state while stage artifacts remain append-only.

Failed preflight writes its report, manifest, and terminal event without constructing a provider. Candidate workspaces must restore to the captured baseline; that disposable recovery state is distinct from the approved real patch. Before real mutation, `run.json` durably records a `recovery_unknown` write-ahead intent. A successful apply is immediately persisted as `applied`; a pre-durability exception becomes `not_applied` only when a detect-only baseline comparison proves identity, otherwise the write-ahead state remains. Failed recovery, required validation failures, ambiguous selection, root drift, changed final evidence, provider/budget/timeout errors, interruption, or persistence trouble all preserve evidence and end in an explicit terminal status. Typed provider output and usage are persisted before budget enforcement; budget-crossing output is retained without starting the next stage.

Real-repository benchmarks, public packaging/release automation, headless CI policy, and GitHub issue/PR adapters intentionally follow this increment rather than introducing another workflow engine.
