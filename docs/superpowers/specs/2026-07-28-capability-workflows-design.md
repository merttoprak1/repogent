# Repogent Capability Workflows Design

## Objective

Expand Repogent from one generic verified-change skill into seven explicit
capabilities with distinct inputs, permissions, state machines, evidence, and
reports:

- repository readiness;
- verified change;
- patch review;
- CI failure triage;
- Python dependency update;
- security fix; and
- release gate.

Repogent remains a Python-repository product. Git-host integrations and package
publishing remain adapters outside the core.

## Product Contract

Repogent is the verification, approval, and evidence layer for AI-produced code
changes. It is not another general-purpose coding agent. Each capability must
make its mutation authority, validation boundary, evaluated target, and evidence
claim explicit.

The plugin skills are thin chat adapters. Python code owns repository scope,
policy, state transitions, approval enforcement, validation, and evidence.
Safety rules must not depend on an agent following prose correctly.

## Architecture

The design uses a shared trusted substrate with separate, typed vertical
workflows:

```text
MCP/API tools
  ├─ inspect_repository_readiness
  ├─ start_verified_change
  ├─ start_patch_review
  ├─ start_ci_triage
  ├─ start_dependency_update
  ├─ start_security_fix
  └─ start_release_gate
            │
            ▼
Capability-specific workflows and state machines
            │
            ▼
Shared trusted substrate
  ├─ Git-based repository scope
  ├─ run and evidence storage
  ├─ bounded input and policy enforcement
  ├─ executor and deterministic validation
  ├─ digests and fingerprints
  └─ typed reports
```

A single `mode` flag is rejected because it would mix read-only and mutating
branches inside one state machine. Fully separate services are also rejected
because they would duplicate security-critical Git, evidence, executor, and
reporting behavior.

There are no existing external users, so the old generic `start_run` MCP tool
does not receive a deprecated alias. The plugin, runtime, tests, and
documentation move to the typed API in one release.

## Capability Contracts

| Skill | Start tool | Checkout mutation | Primary result |
| --- | --- | --- | --- |
| `repository-readiness` | `inspect_repository_readiness` | Never | Scope, tool, provider, executor, and remediation report |
| `verified-change` | `start_verified_change` | After exact approval | Independently validated exact patch |
| `review-patch` | `start_patch_review` | Never | Findings, validation evidence, and merge assessment |
| `ci-failure-triage` | `start_ci_triage` | Never | Root-cause candidates and a typed repair request |
| `dependency-update` | `start_dependency_update` | After exact approval | Validated Python dependency and lockfile patch |
| `security-fix` | `start_security_fix` | After exact approval | Threat-assumption-bound validated patch |
| `release-gate` | `start_release_gate` | Never | Commit-bound release decision and artifact digests |

`get_run`, `get_report`, and `cancel_run` apply to every persistent run.
Approval and patch-application tools accept only run kinds whose state machines
contain the corresponding transition. A tool called against the wrong run kind
returns a typed policy error.

Read-only workflows may require explicit executor selection before running
repository code. That decision authorizes validation only and can never expand
the run into a mutating workflow.

## Repository Scope

In a Git repository, inventory is derived from the semantics of:

```text
git ls-files --cached --others --exclude-standard
```

The resulting tracked and non-ignored untracked paths are filtered again for
sensitive paths, symlinks, special files, evidence directories, linked
worktrees, and tool caches. Existing no-follow and race-resistant file access
remain mandatory.

The aggregate byte, file count, directory entry, depth, and elapsed-time limits
apply to the selected inventory rather than every filesystem entry beneath the
repository root. A large ignored directory must not consume the aggregate byte
budget.

For a non-Git directory, Repogent uses a bounded fixed-ignore fallback and marks
the scope source explicitly in evidence. Sensitive-path filtering is independent
of `.gitignore`; an ignored secret does not become eligible for inspection.

## Workflow Data Flows

### Repository readiness

Resolve scope, inspect configuration, check provider and validation commands,
enumerate executor options, and return typed remediation. This flow does not
generate a patch, run target repository code, or mutate the checkout.

### Verified change

Proceed through requirements, plan, unvalidated exact preview, explicit executor
selection, validation, exact patch approval, one application, and final
validation. Every decision remains digest-bound.

### Patch review

Accept either the current working-copy diff or a pair of local Git refs. Resolve
the exact diff, perform bounded static review, request executor selection before
running repository code, run relevant fixed validation, and emit findings plus a
merge assessment. It never repairs or applies a patch.

GitHub, GitLab, Bitbucket, and other hosting integrations are adapters. They
resolve remote change metadata into a disposable checkout and local refs before
calling the core review workflow.

### CI failure triage

Accept bounded user-supplied logs and artifacts. Redact secrets, cluster failure
segments, correlate them with the repository, and produce root-cause candidates
and a typed proposed repair request. Raw unbounded logs are never placed in a
model prompt.

Triage is read-only. A requested repair creates a new `verified-change` run with
fresh approvals; authority never expands inside the triage run. CI-provider log
download belongs in hosting adapters.

### Python dependency update

Support `pyproject.toml`, `requirements*.txt`, `constraints*.txt`,
`poetry.lock`, `uv.lock`, `Pipfile`, and `Pipfile.lock`. Reject unsupported
ecosystems explicitly rather than silently falling back to a generic workflow.
The run uses the full mutating approval and executor sequence and treats manifest
and lockfile consistency as required evidence.

### Security fix

Accept either a structured advisory/finding or a user-described security
scenario. Requirements convert the input into a typed threat assumption,
affected scope, and acceptance evidence. The run uses the full mutating approval
and executor sequence.

Reports may claim that a specified regression test passed, a bounded attack
scenario was blocked, and fixed security checks passed. They must not describe
those results as a comprehensive security audit.

### Release gate

Bind the run to an immutable Git commit and repository fingerprint. Reject a
dirty checkout or a target that moves during execution. Validate version
consistency, fixed tests, lint, type checking, security checks, wheel and sdist
construction and contents, plugin/runtime version compatibility, and artifact
digests.

This workflow does not create tags, publish packages, upload artifacts, or change
code. Those actions belong to future publishing adapters.

## Outcomes and Trust

Workflow outcome and evidence trust are independent axes.

Workflow outcomes include:

- readiness: `READY` or `BLOCKED`;
- review: `APPROVE`, `REQUEST_CHANGES`, or `INCONCLUSIVE`;
- CI triage: `ROOT_CAUSE_IDENTIFIED`, `CANDIDATES_FOUND`, or `INCONCLUSIVE`;
- release: `RELEASE_VERIFIED` or `RELEASE_BLOCKED`; and
- change: `PATCH_READY`, `APPLIED`, or `HUMAN_INTERVENTION_REQUIRED`.

Evidence trust remains:

- `ISOLATED VERIFIED` only when required checks pass in Docker;
- `REDUCED ISOLATION` when required checks pass after explicit local-execution
  consent; and
- `UNVALIDATED` when no executor completed required validation.

A static review can therefore return `REQUEST_CHANGES` with `UNVALIDATED`, while
a passing local release gate can return `RELEASE_VERIFIED` with
`REDUCED ISOLATION`.

## Errors, Recovery, and Reporting

Scope, limit, provider, executor, policy, and validation failures use typed error
codes, a safe user-facing message, and remediation. Every terminal report states:

- whether the real checkout changed;
- the exact commit, diff, or patch evaluated;
- required, passed, failed, and skipped checks;
- the workflow outcome and independent trust label; and
- the local evidence directory.

After timeout or uncertain delivery, non-idempotent executor-selection and
patch-application operations are never retried blindly. The client reconciles
the run first. Read-only workflows have no transition to patch application, even
after an error or retry.

## Skills

The plugin ships seven skill directories matching the capability names. Their
frontmatter descriptions contain triggering conditions, not abbreviated
workflows. Each skill:

- selects only its capability's MCP tools;
- preserves typed run and digest boundaries;
- renders outcomes and trust labels separately;
- refuses implicit authority expansion; and
- defers policy enforcement to the runtime.

Agent metadata and default prompts use the same capability names. The generic
`repogent` skill is replaced by `verified-change`; explicit plugin invocation
without a sufficiently specific request asks the user which capability they
intend.

## Testing and Acceptance

Each capability requires:

1. Domain tests for typed inputs, state transitions, digests, outcomes, and
   reports.
2. MCP contract tests for schemas, annotations, and wrong-run-kind rejection.
3. Integration tests using real Git fixtures and executor boundaries.
4. Skill pressure tests that first demonstrate unsafe or incorrect baseline
   behavior, then verify compliance with the skill.

Required cross-capability scenarios include:

- ignored large tool directories do not exhaust repository inventory limits;
- sensitive and symlinked files remain excluded regardless of Git ignore state;
- patch review never repairs or applies the reviewed diff;
- ambiguous words such as `continue` do not approve execution or a patch;
- CI triage creates a typed repair request rather than mutating its checkout;
- a release gate rejects a dirty or moving target commit;
- security evidence does not overclaim a comprehensive audit;
- unsupported dependency ecosystems fail explicitly; and
- plugin manifest, skill bundle, MCP schema, and runtime package versions agree.

Every persistent workflow has golden-path, limit-failure, and policy-error
coverage. Executor-backed workflows also cover validation failure and uncertain
executor-selection delivery. Mutating workflows additionally cover uncertain
patch-application delivery.

## Delivery Sequence

1. Shared foundation: Git scope, typed run kinds, common evidence and report
   contracts, migration of the existing workflow and skill to
   `verified-change`, and removal of generic `start_run`.
2. Repository readiness capability.
3. Read-only patch review and CI failure triage.
4. Commit-bound release gate.
5. Python dependency update and security fix.
6. Cross-capability policy and report integration.
7. Seven plugin skills, agent metadata, documentation, pressure tests, and the
   integrated release gate.

Each stage is independently testable and must leave the repository releasable.
Hosting-provider adapters and publishing operations are deliberately outside
this design.
