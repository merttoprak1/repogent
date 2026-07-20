# Repogent Phase 2 Design

**Status:** Approved in design review on 2026-07-20

## Purpose

Phase 2 turns Repogent's vertical-slice MVP into a useful, open-source-first engineering tool for broad Python repositories. It prioritizes developer adoption, portfolio value, and the maintainer's own daily use while preserving clean boundaries for a separately hosted service in Phase 3.

The product promise is:

> Repogent turns a developer request or issue into a reviewed Python patch backed by reproducible evidence.

The target outcome is not feature parity with commercial coding agents. It is a trustworthy local workflow that five to ten developers can install, understand, use on real repositories, and recommend or star because its decisions are inspectable.

## Goals

Phase 2 must:

- support Python repositories broadly rather than only FastAPI projects;
- improve repository localization with deterministic Python structure;
- generate and compare patch alternatives only when evidence justifies the cost;
- validate patches against focused and regression evidence;
- make every run understandable through a live CLI timeline and portable evidence bundle;
- prove reliability on a published real-repository benchmark suite;
- offer a clean installation and release experience;
- add headless CI and GitHub issue or pull-request integration after the local engine is reliable;
- retain explicit approval, sandbox, budget, redaction, and transactional-apply guarantees;
- define interfaces that a future hosted service can replace without forking the workflow engine.

## Non-goals

Phase 2 does not include:

- a hosted FastAPI control plane;
- PostgreSQL-backed users, runs, organizations, authentication, RBAC, or billing;
- queue workers or distributed job orchestration;
- a web dashboard;
- semantic or vector retrieval as a prerequisite;
- support for non-Python languages;
- uncontrolled model-authored shell execution;
- autonomous merging, deployment, or production changes;
- benchmark-scale candidate sampling for ordinary developer runs.

## Competitive Lessons

Phase 2 uses three open or publicly inspectable projects as learning references without cloning their implementations.

### mini-SWE-agent: minimal core and product discipline

Repogent should adopt the principles of a small inspectable execution loop, composable configuration, portable models and environments, versioned trajectories, straightforward installation, strong documentation, release automation, and visible benchmark identity.

Repogent should improve on that model through typed workflow stages, acceptance criteria, explicit approvals, transactional patches, redacted evidence, and safer Docker defaults instead of unrestricted shell autonomy.

### AutoCodeRover: Python-aware localization

Repogent should adopt the principles of AST-backed symbol indexes, layered search, small code snippets, reproduction evidence, optional fault localization, and recorded search decisions.

Repogent should implement these ideas independently behind package-quality interfaces with graceful fallbacks. AutoCodeRover's source-available license restricts competing products, so no code is copied or derived from it.

### Agentless: candidate generation and validation

Repogent should adopt the principles of hierarchical localization, diverse patch candidates, syntax filtering, reproduction and regression tests, deduplication, and evidence-based selection.

Repogent should improve on its benchmark-oriented workflow by using an adaptive one-to-three candidate budget, typed evidence, risk-aware ranking, and a human approval gate before application.

Live-SWE-agent is a secondary reference for configuration-driven experiments. open-codex is a secondary reference for terminal UX, approval-mode vocabulary, and headless CI. Repogent does not adopt broad session-level approval bypasses or no-sandbox fallbacks.

## Design Principles

1. **Deterministic before probabilistic.** Filesystem access, symbol extraction, policy enforcement, execution, validation, budgets, and state transitions remain deterministic services.
2. **Evidence before confidence.** A recommendation cites localization, tests, policy results, and acceptance-criteria coverage rather than presenting an unexplained score.
3. **Uncertainty reduces autonomy.** Weak evidence never silently lowers sandbox, approval, or validation requirements.
4. **One candidate by default.** Extra model calls must be triggered by observable ambiguity, failure, breadth, or risk.
5. **Failures are durable results.** Every terminal state explains what happened and preserves the best safe evidence.
6. **Local-first, service-ready.** Phase 2 remains a simple local process while storage, execution, providers, and event consumption have replaceable interfaces.
7. **Adoption is part of the architecture.** Installation, documentation, reports, benchmarks, and CI compatibility are product capabilities rather than release cleanup.

## System Architecture

The Phase 2 pipeline is:

```text
Understand -> Localize -> Propose -> Validate -> Decide
```

### Understand

Repogent records the request, repository commit and working-tree state, configuration fingerprint, applicable constraints, risks, assumptions, and typed acceptance criteria. A preflight verifies repository eligibility, executor availability, configured provider access, writable paths, budgets, and required tools before expensive model work begins.

### Localize

Repogent combines deterministic signals into ranked candidate locations:

- lexical relevance to the issue and acceptance criteria;
- Python modules, classes, functions, methods, decorators, and containment;
- import relationships and symbol references;
- relationships between source files and tests;
- traceback frames and failing-test locations when available;
- repository history or changed-file evidence when explicitly available.

The localizer produces small context bundles rather than a repository dump. Every snippet includes its path, line range, symbol identity, selection reason, contributing signals, and score. File, byte, and token budgets remain explicit.

The first implementation uses Python's AST and bounded lexical retrieval. Semantic retrieval can later contribute another signal through the same localization interface; it is not required for Phase 2.

### Propose

The implementation role receives the approved requirements, plan, context bundle, relevant test evidence, and patch constraints. It returns a typed candidate containing:

- a unified diff;
- a concise rationale;
- expected acceptance-criteria coverage;
- assumptions and risks;
- proposed focused tests or validation targets.

Repogent generates one candidate initially. It may generate up to two alternatives only when the adaptive candidate policy triggers.

### Validate

Every candidate is validated independently without letting one candidate mutate the baseline used by another. Validation includes:

- unified-diff and patch-policy checks;
- Python parsing or compilation for changed Python files;
- focused reproduction or affected-area tests when discoverable;
- repository regression checks selected by deterministic policy;
- configured lint, type, and security checks;
- changed-area and patch-breadth risk analysis;
- acceptance-criteria evidence mapping.

Required failures are blockers. Optional unavailable tools are recorded as skipped with reasons and reduce evidence strength; they are never reported as passed.

### Decide

Repogent filters invalid candidates, deduplicates equivalent patches, and compares survivors. The comparison includes:

- correctness evidence from focused or reproduction tests;
- regression evidence from the existing suite;
- acceptance-criteria coverage;
- risk from affected paths, public APIs, dependencies, and patch breadth;
- unnecessary changes and diff size;
- model tokens, estimated cost, validation time, and total duration.

The result is an explainable recommendation, not an opaque aggregate score. Hard blockers cannot be hidden by other metrics. When candidates have materially comparable evidence, Repogent presents the choice to the developer instead of manufacturing certainty.

The configured approval gate shows the exact recommended diff, alternatives, evidence, risks, cost, and selection reason. An approved patch is applied transactionally and receives final validation and QA review.

## Adaptive Candidate Policy

Repogent remains with one candidate when:

- the patch passes policy and syntax checks;
- focused tests pass when applicable;
- required regression checks pass;
- the affected code matches the acceptance criteria;
- the patch is not unexpectedly broad;
- risk is low or medium and evidence is sufficiently concentrated.

Repogent may generate up to two alternatives when any of these conditions holds:

- validation fails;
- localization remains ambiguous after the normal search pass;
- acceptance-criteria coverage is incomplete;
- the change is classified as high risk;
- the first patch touches substantially more code than the plan predicted;
- required evidence cannot distinguish whether the proposed behavior is correct.

Alternative prompts receive the same approved requirements and baseline but include the previous candidate's objective failure evidence. They must not be told simply to make a different-looking patch.

Candidate generation stops when a clearly dominant valid candidate exists, the three-candidate maximum is reached, or a run budget is exhausted.

## Component Boundaries

### Python symbol graph

The symbol graph records modules, classes, functions, methods, decorators, imports, definitions, containment, and statically discoverable references with source locations. Parse failures are isolated to individual files and reported. The graph is serializable and cacheable by repository fingerprint.

The graph augments the existing repository inventory; it does not replace path-security, size, symlink, secret, or traversal controls.

### Localization service

The localization service accepts the repository inventory, symbol graph, request, acceptance criteria, and optional runtime evidence. It returns ranked context snippets and a localization report. Scorers contribute named signals so their effects remain inspectable.

### Candidate service

The candidate service owns generation attempts and candidate identities. It does not apply patches. Each attempt records its input context, provider information, output, usage, and relationship to an earlier failure.

### Candidate validator

The validator creates an isolated candidate workspace or restores an identical baseline before each evaluation. It returns a typed evidence record containing policy, syntax, command, test, risk, and acceptance results.

### Candidate selector

The selector applies hard eligibility rules first and then produces a comparison and recommendation from the remaining evidence. A deterministic tie or ambiguity rule requests human selection.

### Run event store

The workflow emits versioned events for stage transitions, model calls, approvals, command starts and finishes, candidate creation, validation results, warnings, and terminal outcomes. Phase 2 stores events locally and atomically. The CLI consumes the same events used by future API or dashboard clients.

### Artifact store

The existing filesystem artifact store remains the default. Its interface supports a future object-storage implementation without changing workflow logic. Artifacts remain outside the target repository, sanitized, bounded, and linked from the manifest.

### Provider and executor interfaces

The existing provider and executor boundaries remain. Provider-specific routing and cost optimization are deferred unless necessary to maintain model compatibility. Docker remains the default execution environment; restricted local execution remains an explicit weaker fallback.

## Run and Evidence Model

Each run has a stable identifier and versioned manifest. The manifest records:

- request and acceptance criteria;
- repository path, commit, dirty-state summary, and fingerprint;
- Repogent version and effective configuration fingerprint;
- provider and executor identities;
- current stage and terminal status;
- budgets and cumulative use;
- candidate identifiers and recommended candidate;
- approval records;
- links to artifacts and events;
- timestamps and terminal reason.

Each candidate records its parent attempt, exact diff hash, generation rationale, validation evidence, cost, duration, eligibility, and selection outcome. Rejected candidates are retained.

The evidence bundle exports stable JSON plus a human-readable Markdown report. The format is versioned so a future hosted service can ingest bundles produced locally.

## Developer Experience

The primary flow is one command from a Git repository:

```text
repogent run "Fix issue #123"
```

Repository and request flags remain available for automation. The interactive CLI shows:

- preflight results;
- the current workflow stage;
- localization progress and number of relevant symbols;
- candidate and validation progress;
- required, failed, passed, and skipped checks;
- elapsed time, token use, and estimated cost;
- the final diff, evidence summary, risks, and approval choices.

The default output stays concise, with a verbose or evidence command for full detail. Errors include the failed stage, consequence, evidence location, and actionable next step.

Installation must have one recommended `uvx` or `pipx` path plus a documented development installation. Release CI verifies that a clean environment can install the published package and display CLI help. Docker preflight reports actionable host-specific guidance.

## Headless CI and GitHub Integration

Headless mode uses the same workflow and event model as the interactive CLI. It requires explicit policies for approvals, budgets, allowed repository state, and terminal exit codes. It writes reports and a concise machine-readable result suitable for CI annotations.

GitHub integration follows the stable local and headless workflows. It is a thin adapter that:

- ingests an issue or pull-request request;
- checks out an exact commit in an isolated environment;
- invokes the normal Repogent engine;
- publishes a status summary and evidence link or artifact;
- creates or updates a pull request only under an explicit configured policy.

GitHub authentication, event handling, and comments do not enter the core workflow. A hosted GitHub App can replace the adapter in Phase 3.

## Failure Model

### Weak localization

Repogent broadens deterministic retrieval once and incorporates available failing-test evidence. If location evidence remains diffuse, the run stops with ranked locations and `human_intervention_required` rather than proposing a broad speculative patch.

### Executor or required tool unavailable

Preflight stops before model spending. If policy permits restricted local execution, Repogent offers it explicitly and marks the resulting evidence as weaker. It never silently falls back from Docker.

### Candidate validation failure

The failure and its raw evidence are persisted. The adaptive policy may generate an alternative within budget. A failed candidate is never overwritten or misreported as a repair success.

### Budget or timeout exhaustion

Repogent stops cleanly, preserves the best unapplied candidate and complete evidence, and returns `human_intervention_required` with the exhausted limit.

### Ambiguous recommendation

Repogent presents the candidate comparison and requests a human choice. Non-interactive mode ends with an explicit ambiguity result unless its configured policy defines a safe deterministic choice.

### Apply, validation, or cleanup failure

Transactional snapshot semantics preserve recoverability. Repogent stops further mutation when restoration cannot be confirmed and reports affected paths and exact recovery instructions. It never claims the checkout is clean without verification.

## Terminal Statuses

- `completed`: all required checks pass and QA recommends approval;
- `completed_with_findings`: required checks pass and non-blocking risks are explicit;
- `changes_requested`: validation passes but QA finds blocking product or review concerns;
- `cancelled`: a human rejects a required approval;
- `human_intervention_required`: no safe validated candidate exists, evidence is ambiguous in non-interactive mode, the environment fails, restoration is uncertain, or a budget expires.

## Testing Strategy

### Unit tests

Unit tests cover symbol extraction, graph relationships, localization scorers, context budgets, adaptive triggers, candidate deduplication, selection eligibility, comparison explanations, schemas, event serialization, and all policy branches.

### Contract tests

Contract suites verify that provider, executor, artifact-store, and run-event-store implementations conform to stable behavior. Filesystem stores and Docker or local executors are tested as concrete implementations.

### Workflow tests

Scripted providers and fixture repositories prove the complete one-candidate success path, alternative generation, ambiguous selection, weak localization, validation failure, rejection, budget exhaustion, executor failure, and every terminal status.

### Security tests

Tests cover path traversal, symlink and hard-link hazards where applicable, secret and credential redaction, protected files, malicious diffs, output limits, Docker network isolation, restricted environment forwarding, transactional restoration, and evidence-root confinement.

### Real-repository benchmark tests

Containerized benchmark fixtures cover multiple Python repository shapes, including libraries, CLI applications, web services, and data or ML-oriented packages. Tasks include bug fixes, small features, and test changes.

Every task pins the repository and base commit, provides an issue statement and acceptance oracle, records expected setup, and can be rerun without hidden manual steps. Benchmark reporting includes failures and limitations, not only successful demonstrations.

## Benchmark Metrics

The benchmark publishes:

- setup-success rate;
- localization recall at file and symbol level when ground truth is available;
- applicable-patch rate;
- focused-test and regression pass rates;
- end-to-end task success rate;
- candidate count and selection accuracy where alternatives exist;
- human-intervention rate;
- median and tail duration;
- tokens and estimated cost per task;
- failure categories and evidence completeness.

Benchmark claims name the model, configuration, executor, repository commits, dataset version, and run date. Phase 2 avoids treating a single saturated benchmark as proof of general reliability.

## Phase 3 Service Seams

Phase 2 defines replaceable interfaces for:

- local filesystem or object-storage artifacts;
- local manifest/event files or database-backed run storage;
- in-process execution or queued workers;
- CLI event rendering or API and dashboard streaming;
- local ownership context or hosted users and organizations.

These interfaces carry identifiers and data needed by a hosted service, but Phase 2 does not implement hosted identity, tenancy, billing, or distributed coordination.

The open-source engine remains independently useful. A Phase 3 service may add managed execution, persistent collaboration, organization controls, hosted evidence, GitHub App automation, and premium operational features without making the local engine incomplete.

## Delivery Sequence

Phase 2 is delivered in this order:

1. **v0.2 local reliability:** Python symbol graph, explainable localization, adaptive candidates, candidate validation and selection, event-backed CLI timeline, portable evidence schema, and failure behavior.
2. **Real-repository benchmark:** pinned fixtures, reproducible container execution, metrics, reports, and documented limitations.
3. **Public v0.2 release:** polished installation, quick start, architecture and security documentation, examples, release automation, and benchmark results.
4. **Headless CI:** stable non-interactive policy, exit codes, artifacts, and a reusable example workflow.
5. **GitHub issue and pull-request integration:** thin adapter over the same headless engine, status output, and explicit pull-request policy.

Work within each milestone is test-driven and preserves a runnable vertical slice. GitHub integration does not precede a reproducible local benchmark.

## Phase 2 Acceptance Criteria

Phase 2 is complete when:

1. A clean environment can install Repogent through the recommended package path and run preflight.
2. Repogent supports representative Python libraries, CLIs, web services, and data or ML packages without framework-specific assumptions.
3. Localization returns symbol-aware snippets with recorded reasons and bounded context.
4. A normal successful task uses one patch candidate; objective ambiguity or failure can trigger at most two alternatives.
5. Every candidate is evaluated from the same baseline and receives independent typed evidence.
6. Required validation failures cannot be overridden by ranking or QA.
7. Candidate selection is explainable, preserves rejected candidates, and requests human input when evidence is materially ambiguous.
8. The CLI presents a live timeline, cost and duration, validation outcomes, diff, risks, and approval action.
9. A run exports versioned Markdown and JSON evidence that can be inspected without Repogent's process still running.
10. Docker remains the default validator and no weaker fallback happens silently.
11. Every documented failure mode ends safely with a clear status, evidence, and recovery guidance.
12. The published real-repository benchmark is reproducible and reports configuration, cost, time, failures, and limitations.
13. Headless CI invokes the same workflow engine and produces stable exit codes and artifacts.
14. GitHub integration remains an adapter over the headless engine rather than duplicating orchestration.
15. Core storage, execution, provider, and event interfaces can support Phase 3 implementations without changing the approved workflow semantics.

## Risks and Mitigations

### Scope growth

Combining repository intelligence, evaluation, adoption work, and integrations could expand indefinitely. The delivery sequence and acceptance criteria keep v0.2 centered on local reliability; CI and GitHub remain subsequent increments.

### False confidence from tests

Passing available tests may not prove the request is satisfied. Repogent separately records acceptance-criteria coverage, missing evidence, and QA findings, and it refuses to translate missing evidence into a pass.

### Candidate cost growth

Alternative generation can multiply model and validation costs. One candidate remains the default, expansion uses explicit triggers, and three candidates is a hard maximum.

### Python analysis incompleteness

Static AST relationships cannot resolve all dynamic Python behavior. Parse gaps and uncertain edges stay visible, runtime evidence can supplement them, and the system stops rather than pretending the graph is complete.

### Benchmark overfitting

Published tasks may shape implementation toward narrow fixtures. The suite includes different repository types and task categories, separates development from evaluation fixtures where practical, publishes failures, and records real user outcomes separately.

### Phase 3 leakage

Premature SaaS abstractions could complicate the local tool. Only interfaces with immediate local implementations are introduced; hosted infrastructure remains deferred.
