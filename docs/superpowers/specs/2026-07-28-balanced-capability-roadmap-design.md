# Repogent Balanced Capability Roadmap Design

## Objective

Complete the capability model defined in
`2026-07-28-capability-workflows-design.md` while making each increment
releasable and improving the product controls needed to trust Repogent's own
releases.

The roadmap balances two goals:

- deliver the five capability workflows still absent from v0.3.0; and
- close release, CI, policy, and evidence gaps that would otherwise be copied
  into every new workflow.

Repogent remains a Python-repository verification, approval, and evidence
product. It does not become a general coding agent, package publisher, or
Git-host automation service.

## Verified v0.3.0 Baseline

The roadmap begins from the state verified on `main` on 2026-07-28:

- `repository-readiness` is a synchronous, read-only capability exposed through
  `inspect_repository_readiness`;
- `verified-change` is a persistent, mutating capability exposed through
  `start_verified_change`;
- Git scope, confined repository inspection, fixed validation, digest-bound
  approvals, deferred executor selection, evidence storage, and checkout
  recovery already exist;
- only the two corresponding plugin skills are packaged;
- the other five persistent workflow names exist only as `WorkflowKind` enum
  members;
- the outcome model, session dispatch, executor gate, and report renderer still
  encode verified-change assumptions;
- the complete pytest suite passes with 90.55% coverage, while Ruff lint, mypy,
  and Bandit pass;
- `make verify` assumes a `python` executable and does not include the format
  check documented by the previous implementation plan; and
- `ruff format --check .` reports existing format drift.

The roadmap treats the last two items as product gate defects, not capability
features.

## Design Principles

### Separate vertical workflows

Each persistent capability has its own typed start input, state machine, result,
and report payload. A shared `mode` flag must not introduce read-only and
mutating branches inside one workflow.

### One trusted substrate

Capability workflows reuse repository scope, fingerprints, evidence storage,
validation executors, digests, cancellation, bounded session access, and report
envelopes. Capability services must not reimplement these security-sensitive
mechanisms.

### Authority is data

The runtime, not a skill prompt, defines which operations a run kind allows.
Selecting an executor authorizes only validation of one digest-bound target.
It never authorizes checkout mutation.

### Releasable increments

Every roadmap phase ends with passing domain, MCP, integration, package, and
product quality gates applicable to that phase. No phase relies on a later phase
to restore a releasable repository.

### Evidence claims remain bounded

Workflow outcome and evidence trust remain independent. A successful local run
can be `REDUCED ISOLATION`; an unexecuted static review can reach a review
outcome while remaining `UNVALIDATED`. Security results never claim a
comprehensive audit.

## Target Architecture

```text
Capability start tools
  ├─ inspect_repository_readiness      synchronous, non-persistent
  ├─ start_verified_change             persistent, mutating
  ├─ start_patch_review                persistent, read-only
  ├─ start_release_gate                persistent, read-only
  ├─ start_ci_triage                   persistent, read-only
  ├─ start_dependency_update           persistent, mutating
  └─ start_security_fix                persistent, mutating
                  │
                  ▼
Capability registry and session dispatch
  ├─ allowed operations
  ├─ allowed outcomes
  ├─ mutation authority
  ├─ executor requirement
  └─ workflow factory
                  │
          ┌───────┴────────┐
          ▼                ▼
Capability workflow    Shared run substrate
state machine          scope, evidence, validation,
and result             digests, cancellation, reports
```

### Capability registry

The registry is the executable policy table for persistent workflows. For each
`WorkflowKind`, it defines:

- the workflow factory;
- whether checkout mutation is permitted;
- which approval kinds are legal;
- whether executor selection can occur;
- which outcomes are legal; and
- which capability result model belongs in the terminal report.

MCP handlers ask the session manager to perform an operation. The session
manager resolves the run kind and checks the registry before delegating to the
workflow. A disallowed operation fails before a gate, executor, or checkout
service is called.

Repository readiness remains outside this registry because it is synchronous
and creates no persistent run.

### Persistent run envelope

All persistent runs share a bounded envelope containing:

- run ID and workflow kind;
- immutable repository and evaluated-target identities;
- status, stage, outcome, and independent trust label;
- checkout state and an explicit `checkout_changed` fact;
- required, passed, failed, and skipped checks;
- cancellation and uncertain-delivery reconciliation state;
- capability-specific result data; and
- the local evidence directory.

Capability-specific data remains a discriminated typed payload rather than a
set of optional fields on one universal model.

### Validation target

Executor selection binds to a general immutable validation target:

```text
run ID + target kind + target digest + executor option digest
```

Target kinds include a candidate patch, reviewed diff, and release commit. A
new target digest invalidates the previous executor decision. Read-only
workflows cannot turn validation consent into patch approval because their
registry entries contain no mutation transition.

### Typed errors

Core services expose errors with:

- stable error code;
- safe user-facing message;
- remediation when available;
- run kind and run ID when known; and
- retry classification: read-only, reconcile-first, or non-retryable.

MCP maps these errors to a stable structured error payload without returning
raw exceptions, paths outside the approved evidence boundary, command output,
or secrets. Wrong-run-kind errors identify the denied operation and capability
without revealing internal state.

### Reports

Every persistent report has one common header followed by a capability result:

- exact evaluated target and digest;
- whether the real checkout changed;
- workflow outcome;
- independent trust label;
- required, passed, failed, and skipped checks;
- typed errors and remediation;
- evidence directory; and
- capability-specific findings, candidates, dependency facts, threat
  assumptions, or artifact digests.

The existing verified-change report moves behind this envelope without changing
its approval or recovery semantics.

## Prioritized Delivery

## P0: Capability Kernel and Release Hygiene

P0 makes the existing trusted substrate safe to reuse before another workflow
is added.

It delivers:

- the capability registry and operation policy matrix;
- capability-aware persistent session dispatch;
- complete outcome enums and per-capability outcome validation;
- the general validation-target executor contract;
- the common report envelope and discriminated result payloads;
- typed service and MCP policy errors;
- regression compatibility for repository readiness and verified change;
- a portable Makefile using an overridable Python interpreter;
- one canonical quality gate shared by local development and CI;
- format enforcement, with the existing mechanical format normalization isolated
  from behavioral changes; and
- package tests that bind runtime, plugin, skill bundle, and MCP schema versions.

P0 does not add a new plugin skill. It is complete only when the two v0.3.0
capabilities retain their current behavior through the new dispatch and report
boundaries.

## P1A: Patch Review

Patch review is the first new persistent workflow because it exercises the
kernel without adding mutation authority.

Inputs are exactly one of:

- the current working-copy diff; or
- two existing local Git refs.

The workflow resolves and fingerprints the exact diff, performs bounded static
review, optionally pauses for executor selection before fixed repository
validation, and returns:

- `APPROVE`;
- `REQUEST_CHANGES`; or
- `INCONCLUSIVE`.

Its result contains ordered findings, affected paths, validation evidence, and
a merge assessment. It has no requirements, plan, repair, patch approval, or
patch application transition. Hosting adapters may later resolve a remote
change into local refs, but no hosting client belongs in this phase.

## P1B: Release Gate

Release gate follows patch review and applies the same read-only execution
boundary to an immutable commit.

The workflow:

- requires a Git repository and a clean checkout;
- resolves the requested target to one commit;
- records a repository fingerprint and rejects movement during execution;
- checks runtime, plugin, skill bundle, and schema version consistency;
- runs fixed tests, lint, type, and security checks;
- builds wheel and sdist in a disposable workspace;
- inspects archive contents and package metadata; and
- records SHA-256 digests for every release artifact.

It returns `RELEASE_VERIFIED` or `RELEASE_BLOCKED`. It never edits source,
creates a tag, publishes a package, uploads an artifact, or changes a remote.

## P2: CI Failure Triage

CI triage accepts bounded user-supplied logs and artifact summaries. CI-provider
download remains an adapter concern.

The workflow:

- rejects input above explicit count and byte limits;
- redacts secrets before persistence or provider context construction;
- segments and clusters failures;
- correlates candidates with the bounded repository inventory;
- emits ranked root-cause candidates; and
- produces a typed `RepairRequest` containing bounded objective, affected
  paths, evidence references, assumptions, and acceptance criteria.

It returns `ROOT_CAUSE_IDENTIFIED`, `CANDIDATES_FOUND`, or `INCONCLUSIVE`.
Starting a repair creates a new verified-change run with a source run ID and
repair-request digest. Requirements, executor selection, and patch approval are
all fresh; authority never expands inside the triage run.

## P3A: Python Dependency Update

Dependency update is a separate mutating workflow built on the P0 substrate.

It recognizes:

- `pyproject.toml`;
- `requirements*.txt`;
- `constraints*.txt`;
- `poetry.lock`;
- `uv.lock`;
- `Pipfile`; and
- `Pipfile.lock`.

The start request names the dependency, desired constraint or update intent, and
allowed manifest scope. Unsupported ecosystems fail before provider or executor
work. The workflow requires manifest and lockfile consistency evidence when a
lockfile is present and never silently falls back to verified change.

It uses fresh requirements, plan, executor, and exact patch decisions and
retains the existing uncertain patch-application recovery contract.

## P3B: Security Fix

Security fix accepts either a structured advisory/finding or a bounded
user-described scenario.

The workflow converts the input into:

- a typed threat assumption;
- affected assets and paths;
- attacker capabilities explicitly in scope;
- excluded assumptions;
- regression evidence; and
- acceptance criteria.

It uses the complete mutating approval and recovery sequence. The terminal
result can claim only that named regression tests passed, a bounded scenario was
blocked, and fixed security checks passed. The schema and renderer reject
language equating those results with a comprehensive audit.

Security fix follows dependency update so the first specialized mutating
workflow establishes the shared composition boundary before higher-risk
security claims are introduced.

## P4: Cross-Capability Product Completion

P4 makes the seven-capability product coherent:

- package all seven skills and matching agent metadata;
- route explicit and natural-language requests to one capability;
- ask for capability clarification when invocation is ambiguous;
- add baseline and post-skill pressure tests for every skill;
- run the complete wrong-operation matrix across all persistent run kinds;
- add stable headless CI exit codes and a reusable workflow;
- add the first real-repository benchmark harness and publish its methodology;
- run Repogent's own integrated release gate; and
- release runtime, plugin, skill bundle, schema, and documentation together.

The initial benchmark measures bounded completion, policy compliance,
validation success, false-positive review findings, and time-to-terminal-state.
It does not claim broad language or ecosystem coverage.

## Data and Authority Flows

### Read-only validation

```text
typed start input
  → bounded target resolution
  → static analysis
  → validation target preview and digest
  → explicit executor selection
  → disposable validation
  → typed outcome + trust label
```

No read-only workflow obtains a patch approval object or calls checkout mutation
services.

### Mutating specialized workflows

```text
typed specialized input
  → capability requirements
  → requirements approval
  → capability plan
  → plan approval
  → exact unvalidated patch preview
  → executor selection
  → disposable validation
  → exact validated patch approval
  → write-ahead recovery intent
  → one checkout application
  → final validation
  → typed outcome + trust label
```

Dependency and security workflows compose trusted services from verified change
but own distinct state machines and results.

### CI repair handoff

```text
CI triage run
  → terminal typed RepairRequest + digest
  → explicit new verified-change start
  → fresh requirements and approvals
```

The new run records provenance but inherits no consent or mutation authority.

## Testing Strategy

Every persistent capability requires:

1. domain tests for typed inputs, legal transitions, digests, outcomes, and
   reports;
2. generated policy-matrix tests for every allowed and denied operation;
3. MCP schema, annotation, structured-error, and reconciliation tests;
4. real-Git integration tests with executor boundaries;
5. golden-path, limit-failure, validation-failure, cancellation, and policy-error
   cases;
6. uncertain executor-selection delivery coverage;
7. uncertain patch-application delivery coverage for mutating workflows; and
8. baseline plus post-skill pressure tests.

Cross-capability acceptance includes:

- no read-only run can reach a patch mutation service;
- executor consent is invalid after its evaluated target changes;
- CI repair provenance carries no approval;
- a dirty or moving release target is rejected;
- unsupported dependency ecosystems fail explicitly;
- security reports cannot overclaim audit coverage;
- all terminal reports state whether the real checkout changed; and
- runtime, plugin, skills, schemas, and documentation agree on the released
  capability set and version.

The canonical product gate runs tests with coverage, Ruff lint and format
checks, mypy, Bandit, package builds, archive inspection, isolated wheel
installation, plugin package tests, and stdio MCP integration. Local and CI
entry points invoke the same gate.

## Plan and Release Boundaries

This design is implemented through seven independently reviewable plans:

1. capability kernel and release hygiene;
2. patch review;
3. release gate;
4. CI failure triage;
5. Python dependency update;
6. security fix; and
7. cross-capability product completion.

Each plan produces working, testable software and its own documentation. Later
plans consume only interfaces delivered and tested by earlier plans.

The expected release progression is:

- P0 may ship as a v0.3.x hardening release;
- P1 through P3 may ship capability increments without waiting for all seven
  skills; and
- P4 produces the integrated seven-capability release.

Version numbers are chosen at release time from the actual compatibility impact;
this design does not predeclare the integrated release as v0.4.0.

## Explicit Non-Goals

This roadmap does not include:

- GitHub, GitLab, or Bitbucket API adapters;
- tag creation, package publishing, or artifact upload;
- automated installation of arbitrary target dependencies;
- model-authored shell commands;
- a web dashboard or remote control plane;
- PostgreSQL or resumable distributed workers;
- semantic vector storage;
- non-Python repository support; or
- autonomous deployment.

Adapters added later must resolve remote data into the bounded local contracts
defined here and must not bypass core policy, approvals, validation, or
evidence.
