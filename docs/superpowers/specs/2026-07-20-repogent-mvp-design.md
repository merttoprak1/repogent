# Repogent Vertical-Slice MVP Design

**Status:** Approved in design review on 2026-07-20

## Purpose

Repogent's first milestone is a runnable, auditable vertical slice that turns a narrowly scoped change request for a Python FastAPI repository into an approved patch, deterministic validation evidence, an independent review, and a final report.

The milestone prioritizes bounded authority, inspectable state, honest failure, and deterministic verification. It is not a general autonomous developer and does not claim arbitrary repository or language support.

## Product Surface

The MVP exposes a command-line interface backed by reusable Python services. It supports these primary commands:

```text
repogent analyze <repository>
repogent run --repository <repository> --request <request>
```

The CLI accepts provider, execution, budget, constraint, and output-directory settings. Run artifacts default to `.repogent/runs/<run-id>/` under the invocation directory; callers may place them elsewhere. If that default resolves inside the target repository, the CLI requires an explicit external `--output-dir`. Audit artifacts are not written into the target repository.

The system uses a provider-neutral structured-generation interface, an OpenAI adapter for live runs, and a deterministic scripted provider for tests and reproducible demonstrations.

## Architecture

The workflow is a synchronous, artifact-first pipeline coordinated by an explicit Python state machine. It does not use LangGraph, a database-backed job engine, or an event bus in this milestone.

The pipeline is:

```text
Request and repository
  -> repository inspection and lexical retrieval
  -> structured requirements
  -> requirements approval
  -> structured implementation plan
  -> plan approval
  -> restricted patch proposal
  -> patch policy validation
  -> patch approval
  -> transactional patch application
  -> deterministic validation
  -> bounded diagnosis and repair when necessary
  -> independent QA review
  -> final Markdown and JSON reports
```

Agents propose schema-validated outputs. Deterministic services own repository traversal, path validation, patch application, command selection, command execution, budget enforcement, and status transitions. No model output can directly invoke the shell or override a failed deterministic check.

## Component Boundaries

### Domain models

Pydantic models define run configuration, structured requirements, implementation plans, retrieved context, patch proposals, validation evidence, repair decisions, QA findings, approval decisions, budgets, and run status.

Every serialized model includes a schema version. Unknown or incompatible versions fail explicitly rather than being silently coerced.

### Model providers

A provider protocol accepts a role-specific prompt, typed input data, and an expected Pydantic output type. Implementations are:

- an OpenAI provider for live structured generation;
- a scripted provider that returns predefined typed outputs for tests and demonstrations.

Provider failures, invalid structured output, timeouts, token usage, and estimated cost are recorded. A schema-generation failure may be retried once. A repeated failure ends the run as `human_intervention_required`.

### Repository analysis and retrieval

The repository service:

- validates and normalizes the repository root;
- traverses regular files without following symlinks outside the root;
- respects configured ignore patterns and conservative size limits;
- inventories Python source, tests, dependency manifests, and configuration;
- uses Python's AST to extract modules, imports, classes, functions, and decorators;
- identifies likely FastAPI routes from decorators and imports;
- supports exact search and lexical/BM25 ranking;
- builds a bounded context package with file paths, relevant excerpts, and ranking reasons.

Semantic embeddings and a vector database are deferred. The retrieval protocol must permit a semantic scorer to be added later without changing agent interfaces.

### Agent roles

The MVP contains thin role services for requirements, planning, implementation, repair, and QA. A role assembles approved typed context, calls the configured provider, validates the response, and records trace metadata. Roles do not access the filesystem or execute commands directly.

### Approval gates

An approval protocol supports three required gates:

1. extracted requirements;
2. implementation plan;
3. exact proposed patch.

The CLI implementation displays the artifact, accepts approval or rejection, and records the decision with optional feedback and a timestamp. Rejection ends the run as `cancelled`. Before patch approval, the target repository remains unchanged.

### Patch policy and application

The implementation agent proposes a unified diff. The patch service parses and validates it before presentation. It rejects:

- absolute paths or paths that normalize outside the repository root;
- symlink escapes;
- malformed or unsupported diff constructs;
- binary patches;
- modifications to configured protected paths;
- patches exceeding configured file-count, line-count, or byte limits.

After approval, the service snapshots every touched path, applies the patch, and restores the snapshot if application fails. Proposed, approved, applied, and repair diffs are stored separately in the run evidence.

### Execution and validation

One executor protocol has two implementations:

- Docker sandbox, used by default;
- restricted local subprocess fallback, enabled only through an explicit CLI option.

The Docker executor mounts the target checkout with only the permissions required for validation, disables network access by default, does not forward host credentials, applies process timeouts and resource limits, and captures stdout, stderr, duration, and exit status.

The local executor uses argument arrays rather than a shell, confines the working directory to the checkout, exposes a minimal environment, enforces timeouts, and accepts commands only from the validation policy. It is a documented development fallback and not equivalent to container isolation.

The validation policy selects deterministic stages from repository configuration. Supported stages are pytest, Ruff, mypy, and Bandit. The model cannot add commands. A missing optional tool is reported as `skipped` with a reason; it is never reported as passed. Exit codes determine pass or fail.

### Workflow and repair

The workflow service owns legal state transitions, approvals, retries, timeouts, and token and estimated-cost budgets.

When validation fails, the service records the failed stage and raw output, retrieves relevant context, asks the repair role for a targeted replacement diff, subjects that diff to the same patch policy, obtains patch approval, applies it transactionally, and reruns validation. The MVP permits at most two repair attempts.

The run ends as `human_intervention_required` when it encounters an unsafe repair, repeated schema failure, timeout, exhausted budget, or two unsuccessful repairs. It must not describe these outcomes as success.

### QA and reporting

After deterministic validation succeeds, an independent QA role assesses acceptance-criteria coverage, test quality, security, regression risk, backward compatibility, dependency risk, and unnecessary changes. It returns structured findings and a merge recommendation.

QA cannot override failed validation. Depending on its findings, a validated run ends as `completed`, `completed_with_findings`, or `changes_requested`.

The reporting service writes a human-readable `report.md` and machine-readable `run.json`.

## Evidence and Observability

Each run produces an append-only evidence bundle under its configured run directory. It contains:

- original request and constraints;
- repository inventory;
- retrieved-context manifest and excerpts;
- each structured agent input and result;
- provider, model, latency, token, and estimated-cost metadata;
- approval decisions and feedback;
- proposed, approved, applied, and repair diffs;
- raw validation commands, output, exit codes, and durations;
- repair history;
- QA review;
- final Markdown report;
- current and terminal run status.

`run.json` is updated atomically and identifies the current stage. Stage artifacts are immutable once written; a subsequent attempt receives a distinct artifact name. Logs distinguish objective tool evidence from model-generated interpretation.

## Error and Security Model

Repository content is untrusted input. The MVP uses default-deny controls:

- no model-authored shell commands;
- no target-repository network access during validation by default;
- no host credential or secret forwarding;
- no symlink traversal outside the repository;
- no write outside approved target paths and the configured evidence directory;
- command allowlists, timeouts, output limits, and Docker resource limits;
- prompt boundaries that label repository content as untrusted data;
- redaction of configured secret patterns from persisted logs;
- patch path and size limits;
- explicit approval before all repository modification.

Patch application failures restore touched files. Validation failures do not roll back a successfully applied approved patch automatically, because its exact state and subsequent repair history must remain inspectable; the final report clearly states that the checkout contains unvalidated changes. A future cleanup command may restore a recorded snapshot, but it is outside this milestone.

## Testing Strategy

The project targets Python 3.11 or newer and uses pytest, coverage.py, Ruff, mypy, and Bandit.

Unit tests cover:

- domain validation and serialization;
- repository-root confinement and symlink handling;
- Python and FastAPI structure extraction;
- lexical ranking and bounded context assembly;
- provider contracts and invalid structured output;
- approval outcomes;
- unified-diff parsing and every patch-policy rejection;
- transactional application and restoration on failure;
- command policies, timeouts, and output capture;
- legal workflow transitions, budgets, and terminal statuses;
- report generation.

Integration tests use temporary FastAPI repositories and the scripted provider to prove:

- `analyze` inventories and ranks relevant files;
- an approved run applies a real patch, runs actual tests, and emits reports;
- rejection at any gate leaves the target checkout unchanged;
- malicious paths and non-allowlisted commands are refused;
- failed validation can be repaired and revalidated;
- two failed repairs end as `human_intervention_required`;
- skipped tools remain visibly skipped.

Docker execution is tested when Docker is available. The local fallback is always tested. OpenAI adapter tests mock the provider boundary and require neither a network connection nor an API key.

The repository's completion gate is:

```text
pytest with at least 85% coverage
Ruff passes
mypy passes
Bandit passes
```

## Reproducible Demonstration

The repository includes one deliberately small FastAPI example and a scripted engineering task. The scripted provider produces the same structured requirements, plan, patch, repair if exercised, and QA review on every run. The demonstration therefore executes the real inspection, approval, patch-policy, application, validation, state-machine, and reporting code without an external API key.

A separate documented command shows how to select the OpenAI provider for a live request.

## MVP Acceptance Criteria

The milestone is complete when:

1. `repogent analyze <repository>` inventories a FastAPI repository and returns ranked relevant files.
2. `repogent run` executes the full approved workflow and emits `report.md` and `run.json`.
3. The bundled demonstration applies an approved patch and records actual passing tests.
4. Requirements, plans, patches, repairs, QA reviews, and state transitions are schema-validated.
5. Rejection at any approval gate leaves the repository unchanged at that gate.
6. Unsafe paths, symlink escapes, binary patches, oversized patches, and non-allowlisted commands are refused.
7. A failed validation can trigger a targeted repair and successful revalidation.
8. Two unsuccessful repairs end as `human_intervention_required`.
9. Docker is the default executor, and restricted local execution requires explicit selection.
10. The project passes its deterministic quality gate with at least 85% test coverage.

## Deferred Scope

This milestone does not include:

- semantic embeddings, Qdrant, or another vector database;
- LangGraph;
- PostgreSQL or resumable background workers;
- a web dashboard or FastAPI control API;
- GitHub issue ingestion or pull-request creation;
- automated dependency installation;
- arbitrary model-authored commands;
- autonomous deployment;
- support for languages other than Python;
- the 20–30-task benchmark suite.

These are future milestones and must not be represented as capabilities of the first release.
