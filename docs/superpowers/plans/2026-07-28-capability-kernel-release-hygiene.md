# Capability Kernel and Release Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generalize Repogent's trusted run substrate for multiple typed workflows and make the local and CI release gates identical and portable.

**Architecture:** Add an executable capability-policy registry in front of session operations, replace patch-specific executor binding with an immutable validation target, and split common report facts from capability result payloads. Preserve the existing verified-change workflow behind adapters while normalizing the quality gate independently from behavioral changes.

**Tech Stack:** Python 3.11+, Pydantic v2, FastMCP, Typer, pytest, Ruff, mypy, Bandit, Hatchling

## Global Constraints

- Repository readiness remains synchronous and non-persistent.
- Runtime policy, not skill prose, defines mutation authority and allowed operations.
- Workflow outcome and evidence trust remain independent.
- Executor selection authorizes validation of one target digest and never checkout mutation.
- Existing repository-readiness and verified-change behavior must remain compatible.
- Local and CI entry points invoke one canonical quality gate.
- The existing format normalization is isolated from behavioral changes.

---

### Task 1: Define capability policy and complete outcome contracts

**Files:**
- Create: `src/repogent/capabilities.py`
- Modify: `src/repogent/domain.py`
- Test: `tests/unit/test_capabilities.py`
- Test: `tests/unit/test_domain.py`

**Interfaces:**
- Produces: `RunOperation`, `CapabilityPolicyError`, `CapabilityDefinition`, `CapabilityRegistry.definition(kind)` and `CapabilityRegistry.require(kind, operation)`
- Produces: complete `WorkflowOutcome` values for all six persistent workflow kinds
- Consumes: existing `WorkflowKind` and `ApprovalKind`

- [ ] **Step 1: Write failing registry and outcome tests**

```python
def test_patch_review_denies_patch_application() -> None:
    registry = CapabilityRegistry.defaults()
    with pytest.raises(CapabilityPolicyError) as caught:
        registry.require(WorkflowKind.PATCH_REVIEW, RunOperation.APPLY_PATCH)
    assert caught.value.code == "operation_not_allowed"


def test_every_kind_has_disjoint_allowed_outcomes() -> None:
    registry = CapabilityRegistry.defaults()
    assert registry.definition(WorkflowKind.RELEASE_GATE).allowed_outcomes == frozenset(
        {WorkflowOutcome.RELEASE_VERIFIED, WorkflowOutcome.RELEASE_BLOCKED}
    )
    assert WorkflowOutcome.APPLIED not in registry.definition(
        WorkflowKind.PATCH_REVIEW
    ).allowed_outcomes
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_capabilities.py tests/unit/test_domain.py -q`

Expected: collection fails because `repogent.capabilities` and release outcomes do not exist.

- [ ] **Step 3: Implement the policy types and default matrix**

```python
class RunOperation(StrEnum):
    GET = "get"
    CANCEL = "cancel"
    GET_REPORT = "get_report"
    APPROVE_REQUIREMENTS = "approve_requirements"
    APPROVE_PLAN = "approve_plan"
    SELECT_EXECUTOR = "select_executor"
    APPLY_PATCH = "apply_patch"


class CapabilityPolicyError(ValueError):
    code: str = "operation_not_allowed"

    @classmethod
    def operation_not_allowed(
        cls, kind: WorkflowKind, operation: RunOperation
    ) -> "CapabilityPolicyError":
        return cls(f"{operation.value} is not allowed for {kind.value}")


@dataclass(frozen=True)
class CapabilityDefinition:
    kind: WorkflowKind
    mutates_checkout: bool
    allowed_operations: frozenset[RunOperation]
    allowed_outcomes: frozenset[WorkflowOutcome]


class CapabilityRegistry:
    @classmethod
    def defaults(cls) -> "CapabilityRegistry":
        return cls({item.kind: item for item in DEFAULT_CAPABILITIES})

    def definition(self, kind: WorkflowKind) -> CapabilityDefinition:
        return self._definitions[kind]

    def require(self, kind: WorkflowKind, operation: RunOperation) -> None:
        if operation not in self.definition(kind).allowed_operations:
            raise CapabilityPolicyError.operation_not_allowed(kind, operation)

    def validate_outcome(self, kind: WorkflowKind, outcome: WorkflowOutcome) -> None:
        if outcome not in self.definition(kind).allowed_outcomes:
            raise ValueError(f"{outcome.value} is not valid for {kind.value}")
```

Add `APPROVE`, `REQUEST_CHANGES`, `INCONCLUSIVE`, `ROOT_CAUSE_IDENTIFIED`,
`CANDIDATES_FOUND`, `RELEASE_VERIFIED`, and `RELEASE_BLOCKED` to
`WorkflowOutcome`. Configure verified change, dependency update, and security
fix as mutating; configure patch review, CI triage, and release gate as
read-only. All persistent kinds allow `GET`, `CANCEL`, and `GET_REPORT`.

- [ ] **Step 4: Validate manifests against the registry**

Replace the verified-change-only manifest outcome validator with:

```python
def validate_terminal_outcome(manifest: RunManifest) -> None:
    if manifest.outcome is not None:
        CapabilityRegistry.defaults().validate_outcome(manifest.kind, manifest.outcome)
```

Call it from a `RunManifest` model validator and add parameterized tests for all
legal and representative illegal kind/outcome pairs.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/unit/test_capabilities.py tests/unit/test_domain.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/repogent/capabilities.py src/repogent/domain.py tests/unit/test_capabilities.py tests/unit/test_domain.py
git commit -m "feat: define capability operation policy"
```

---

### Task 2: Bind executor selection to a generic validation target

**Files:**
- Modify: `src/repogent/mcp_models.py`
- Modify: `src/repogent/executor_selection.py`
- Modify: `src/repogent/execution_gate.py`
- Modify: `src/repogent/workflow.py`
- Test: `tests/unit/test_executor_selection.py`
- Test: `tests/unit/test_execution_gate.py`
- Test: `tests/unit/test_workflow.py`

**Interfaces:**
- Produces: `ValidationTargetKind`, `ValidationTarget`, `ValidationDecision`
- Changes: `ExecutorRegistry.build_options(run_id: str, target: ValidationTarget, availability: Sequence[ExecutorAvailability])` and `ExecutorSelector.select(target: ValidationTarget, *, timeout_seconds: float)`
- Preserves: JSON aliases for `preview_digest` during the v0.3.x manifest read path only

- [ ] **Step 1: Write failing target-binding tests**

```python
def test_option_digest_changes_with_target_kind() -> None:
    patch = ValidationTarget(kind=ValidationTargetKind.PATCH, digest="a" * 64)
    commit = ValidationTarget(kind=ValidationTargetKind.COMMIT, digest="a" * 64)
    assert option_digest("run-1", patch, ExecutionMode.DOCKER, None) != option_digest(
        "run-1", commit, ExecutionMode.DOCKER, None
    )


def test_gate_rejects_decision_for_previous_target() -> None:
    gate.publish(ValidationTarget(kind=ValidationTargetKind.DIFF, digest="a" * 64))
    decision = decision_for(gate.pending_choice)
    gate.publish(ValidationTarget(kind=ValidationTargetKind.DIFF, digest="b" * 64))
    with pytest.raises(ExecutionGateError, match="target digest"):
        gate.submit(decision)
```

- [ ] **Step 2: Run executor tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_executor_selection.py tests/unit/test_execution_gate.py -q`

Expected: imports fail because validation-target models are absent.

- [ ] **Step 3: Add target and decision models**

```python
class ValidationTargetKind(StrEnum):
    PATCH = "patch"
    DIFF = "diff"
    COMMIT = "commit"


class ValidationTarget(VersionedModel):
    kind: ValidationTargetKind
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ValidationDecision(VersionedModel):
    run_id: str = Field(min_length=1, max_length=256)
    target: ValidationTarget
    mode: ExecutionMode
    option_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Decision
```

Rename `PendingExecutionChoice.preview_digest` to `target` and rename
`ExecutionDecision` to `ValidationDecision`. Keep input compatibility with the
old shape through `AliasChoices("target", "preview_digest")` only where stored
v0.3.x evidence is read; new responses emit `target` exclusively.
Add `evaluated_target: ValidationTarget | None = None` to `RunManifest`; verified
change sets it whenever a preview becomes current and clears obsolete consent
when that value changes.

- [ ] **Step 4: Generalize selector protocols and digests**

```python
class ExecutorSelector(Protocol):
    def select(
        self, target: ValidationTarget, *, timeout_seconds: float
    ) -> PreparedExecutor:
        """Return the explicitly selected executor for this exact target."""


def option_digest(
    run_id: str,
    target: ValidationTarget,
    mode: ExecutionMode,
    risk_statement: str | None,
) -> str:
    payload = {
        "run_id": run_id,
        "target": target.model_dump(mode="json"),
        "mode": mode.value,
        "risk_statement": risk_statement,
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()
```

Serialize `target.model_dump(mode="json")` into the option digest. Adapt verified
change by constructing `ValidationTarget(PATCH, preview.digest)` before it calls
the selector.

- [ ] **Step 5: Run target, gate, and verified-change regression tests**

Run: `.venv/bin/python -m pytest tests/unit/test_executor_selection.py tests/unit/test_execution_gate.py tests/unit/test_workflow.py tests/unit/test_run_sessions.py -q`

Expected: all tests pass, including stale-digest and uncertain-delivery cases.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/repogent/mcp_models.py src/repogent/executor_selection.py src/repogent/execution_gate.py src/repogent/workflow.py tests/unit/test_executor_selection.py tests/unit/test_execution_gate.py tests/unit/test_workflow.py tests/unit/test_run_sessions.py
git commit -m "feat: bind executor consent to validation targets"
```

---

### Task 3: Add typed errors and capability-aware session operations

**Files:**
- Create: `src/repogent/errors.py`
- Modify: `src/repogent/run_sessions.py`
- Modify: `src/repogent/mcp_server.py`
- Modify: `src/repogent/mcp_models.py`
- Test: `tests/unit/test_errors.py`
- Test: `tests/unit/test_run_sessions.py`
- Test: `tests/unit/test_mcp_server.py`

**Interfaces:**
- Produces: `ErrorCode`, `RetryClass`, `ErrorDetail`, `RepogentError`
- Changes: `SessionManager.decide`, `select_executor`, and future mutation methods check `CapabilityRegistry`
- Produces: `_call_service` maps `RepogentError` to a sanitized MCP error carrying `ErrorDetail`

- [ ] **Step 1: Write failing wrong-operation and serialization tests**

```python
def test_manager_rejects_patch_approval_for_review_run(review_session) -> None:
    manager = manager_with(review_session)
    with pytest.raises(RepogentError) as caught:
        manager.decide(patch_decision(review_session.run_id))
    assert caught.value.detail.code is ErrorCode.OPERATION_NOT_ALLOWED
    assert caught.value.detail.retry is RetryClass.NON_RETRYABLE


def test_error_detail_redacts_unsafe_message() -> None:
    detail = ErrorDetail(
        code=ErrorCode.POLICY,
        message="safe message",
        remediation="inspect the run report",
        retry=RetryClass.NON_RETRYABLE,
    )
    assert "secret" not in detail.model_dump_json()
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_errors.py tests/unit/test_run_sessions.py tests/unit/test_mcp_server.py -q`

Expected: collection fails because typed errors are absent.

- [ ] **Step 3: Implement the stable error contract**

```python
class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNKNOWN_RUN = "unknown_run"
    OPERATION_NOT_ALLOWED = "operation_not_allowed"
    STALE_DIGEST = "stale_digest"
    LIMIT_EXCEEDED = "limit_exceeded"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    VALIDATION_FAILED = "validation_failed"
    POLICY = "policy_error"
    INTERNAL = "internal_error"


class RetryClass(StrEnum):
    READ_ONLY = "read_only"
    RECONCILE_FIRST = "reconcile_first"
    NON_RETRYABLE = "non_retryable"


class ErrorDetail(VersionedModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=512)
    remediation: str | None = Field(default=None, max_length=512)
    retry: RetryClass
    run_id: str | None = Field(default=None, max_length=256)
    run_kind: WorkflowKind | None = None


class RepogentError(RuntimeError):
    def __init__(self, detail: ErrorDetail) -> None:
        super().__init__(detail.message)
        self.detail = detail
```

Convert unknown-run, stale-decision, executor, and wrong-operation boundaries to
this contract. Do not place raw exception text in `ErrorDetail.message`.

- [ ] **Step 4: Enforce operation policy in session dispatch**

Add `registry: CapabilityRegistry = CapabilityRegistry.defaults()` to
`SessionManager.__init__`. Resolve `session.snapshot().kind`, map the requested
gate to `RunOperation`, and call `registry.require` before delegation. Add a
test double session for every workflow kind so matrix tests do not depend on
future workflow implementations.

- [ ] **Step 5: Map typed errors at MCP boundary**

Use FastMCP's supported tool-error mechanism to return the sanitized
`ErrorDetail.model_dump(mode="json")`; keep the current generic internal error
for unexpected exceptions. Contract tests must assert code, retry class, and
absence of raw exception text.

- [ ] **Step 6: Run service and MCP tests**

Run: `.venv/bin/python -m pytest tests/unit/test_errors.py tests/unit/test_run_sessions.py tests/unit/test_mcp_server.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/repogent/errors.py src/repogent/run_sessions.py src/repogent/mcp_server.py src/repogent/mcp_models.py tests/unit/test_errors.py tests/unit/test_run_sessions.py tests/unit/test_mcp_server.py
git commit -m "feat: enforce typed capability operations"
```

---

### Task 4: Introduce the common report envelope

**Files:**
- Create: `src/repogent/run_reports.py`
- Modify: `src/repogent/domain.py`
- Modify: `src/repogent/reporting.py`
- Modify: `src/repogent/mcp_models.py`
- Modify: `src/repogent/run_sessions.py`
- Test: `tests/unit/test_run_reports.py`
- Test: `tests/unit/test_reporting.py`

**Interfaces:**
- Produces: `CheckSummary`, `VerifiedChangeResult`, `CapabilityResult`, `PersistentRunReport`
- Changes: MCP `RunReport` wraps `PersistentRunReport` rather than an untyped Markdown-only result
- Preserves: `report.md` as the human-readable rendering of the same typed model

- [ ] **Step 1: Write failing envelope tests**

```python
def test_verified_change_report_states_checkout_fact_and_trust() -> None:
    report = build_persistent_report(applied_manifest(), verified_change_result())
    assert report.checkout_changed is True
    assert report.trust_label is TrustLabel.REDUCED_ISOLATION
    assert isinstance(report.result, VerifiedChangeResult)


def test_report_rejects_result_for_wrong_kind() -> None:
    with pytest.raises(ValidationError, match="result kind"):
        PersistentRunReport(
            **base_report_fields(kind=WorkflowKind.PATCH_REVIEW),
            result=verified_change_result(),
        )
```

- [ ] **Step 2: Run report tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_run_reports.py tests/unit/test_reporting.py -q`

Expected: imports fail because the envelope is absent.

- [ ] **Step 3: Implement typed common and verified-change results**

```python
class CheckSummary(VersionedModel):
    required: list[str] = Field(default_factory=list)
    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class VerifiedChangeResult(VersionedModel):
    kind: Literal[WorkflowKind.VERIFIED_CHANGE] = WorkflowKind.VERIFIED_CHANGE
    selected_candidate_id: str | None
    applied_paths: list[str] = Field(default_factory=list, max_length=20)
    final_validation_status: FinalValidationStatus


CapabilityResult = VerifiedChangeResult


class PersistentRunReport(VersionedModel):
    run_id: str
    kind: WorkflowKind
    status: RunStatus
    outcome: WorkflowOutcome
    evaluated_target: ValidationTarget | None
    checkout_changed: bool
    checkout_state: CheckoutState
    checks: CheckSummary
    trust_label: TrustLabel
    errors: list[ErrorDetail] = Field(default_factory=list)
    evidence_path: str
    result: CapabilityResult
```

The alias becomes a discriminated union when the next result type lands. Validate `result.kind == kind` and derive
`checkout_changed` only from durable checkout state.

- [ ] **Step 4: Render Markdown from the envelope**

Make `render_report` construct `PersistentRunReport` first and render its common
header before verified-change details. Store a versioned `report.json` beside
`report.md`. `SessionManager.get_report` reads and validates `report.json`, then
returns both typed data and bounded Markdown.

- [ ] **Step 5: Run reporting, session, and integration regressions**

Run: `.venv/bin/python -m pytest tests/unit/test_run_reports.py tests/unit/test_reporting.py tests/unit/test_run_sessions.py tests/integration/test_plugin_end_to_end.py -q --no-cov`

Expected: all tests pass.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/repogent/run_reports.py src/repogent/domain.py src/repogent/reporting.py src/repogent/mcp_models.py src/repogent/run_sessions.py tests/unit/test_run_reports.py tests/unit/test_reporting.py tests/unit/test_run_sessions.py tests/integration/test_plugin_end_to_end.py
git commit -m "feat: type persistent capability reports"
```

---

### Task 5: Make the quality gate portable and canonical

**Files:**
- Modify: `Makefile`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Test: `tests/unit/test_package_data.py`

**Interfaces:**
- Produces: `make verify` as the single local and CI gate
- Guarantees: interpreter override through `PYTHON`, defaulting to `python3`
- Guarantees: lint, format, type, security, build, wheel inspection, plugin tests, and stdio integration are not duplicated with divergent arguments

- [ ] **Step 1: Write failing gate contract tests**

```python
def test_make_verify_is_portable_and_complete() -> None:
    makefile = Path("Makefile").read_text()
    assert "PYTHON ?= python3" in makefile
    for target in ("test", "lint", "format-check", "typecheck", "security", "package-check"):
        assert target in makefile


def test_ci_invokes_only_canonical_verify_gate() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text()
    assert workflow.count("make verify") == 1
    assert "python -m pytest tests/unit/test_plugin_package.py" not in workflow
```

- [ ] **Step 2: Run package tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_package_data.py -q`

Expected: assertions fail because the current Makefile uses `python` and omits format/package checks.

- [ ] **Step 3: Define the canonical Make targets**

```make
PYTHON ?= python3

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

format-check:
	$(PYTHON) -m ruff format --check .

typecheck:
	$(PYTHON) -m mypy src

security:
	$(PYTHON) -m bandit -q -r src/repogent

package-check:
	$(PYTHON) -m build
	$(PYTHON) -m pytest tests/unit/test_plugin_package.py tests/integration/test_plugin_end_to_end.py -q --no-cov

verify: test lint format-check typecheck security package-check
```

Move wheel archive and isolated-install assertions into
`tests/unit/test_package_data.py`, invoked by `package-check`, so CI contains no
second implementation of package policy.

- [ ] **Step 4: Normalize formatting in an isolated mechanical commit**

Run: `.venv/bin/python -m ruff format .`

Expected: Ruff reports the existing drift reformatted. Review `git diff --stat`
and confirm this step contains formatting only.

- [ ] **Step 5: Commit mechanical formatting**

```bash
git add src tests
git commit -m "style: normalize Ruff formatting"
```

- [ ] **Step 6: Update CI and documentation, then run the canonical gate**

Set CI's gate step to `make verify PYTHON=python`. Document
`make verify PYTHON=.venv/bin/python` for an existing local environment and
`make verify` when `python3` resolves to the development interpreter.

Run: `make verify PYTHON=.venv/bin/python`

Expected: all tests and package checks pass; format check reports no changes.

- [ ] **Step 7: Commit Task 5**

```bash
git add Makefile .github/workflows/ci.yml README.md docs/architecture.md tests/unit/test_package_data.py
git commit -m "build: unify local and CI verification"
```

---

### Task 6: Lock v0.3.x compatibility and package identity

**Files:**
- Modify: `tests/unit/test_plugin_package.py`
- Modify: `tests/integration/test_plugin_end_to_end.py`
- Modify: `docs/security.md`
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: capability registry, validation target, typed error, and report envelope contracts
- Produces: compatibility assertions for the two shipped skills and their MCP surfaces
- Guarantees: runtime version, plugin version, skill set, MCP schema snapshot, and documented capability set agree

- [ ] **Step 1: Add failing package identity assertions**

```python
def test_release_identity_agrees_across_surfaces() -> None:
    identity = released_identity()
    assert identity.runtime_version == identity.plugin_version
    assert identity.skills == {"repository-readiness", "verified-change"}
    assert identity.start_tools == {
        "inspect_repository_readiness",
        "start_verified_change",
    }
```

Add an MCP integration case that submits `approve_patch` to a synthetic
patch-review session and asserts `operation_not_allowed` without invoking its
decision channel.

- [ ] **Step 2: Run package and integration tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_plugin_package.py tests/integration/test_plugin_end_to_end.py -q --no-cov`

Expected: the new identity helper and synthetic run fixture are absent.

- [ ] **Step 3: Implement release identity helper and fixtures**

Keep the helper test-local. Parse `pyproject.toml`, `plugin.json`, skill
directories, and the live FastMCP tool list. Do not duplicate production
version parsing code solely for tests.

- [ ] **Step 4: Document the new runtime policy boundary**

Update architecture and security docs with the operation registry,
validation-target consent, typed error retry classes, and the fact that skills
cannot grant runtime authority.

- [ ] **Step 5: Run the complete P0 gate**

Run: `make verify PYTHON=.venv/bin/python`

Expected: all checks pass and package identity reports exactly the two v0.3.x
capabilities.

- [ ] **Step 6: Commit Task 6**

```bash
git add tests/unit/test_plugin_package.py tests/integration/test_plugin_end_to_end.py docs/security.md docs/architecture.md
git commit -m "test: lock capability kernel compatibility"
```

---

## Plan Self-Review

- Spec coverage: this plan covers the capability registry, operation policy,
  generic validation target, typed errors, report envelope, portable quality
  gate, format normalization, and v0.3.x compatibility.
- Placeholder scan: every implementation step names its concrete types, files,
  command, and expected result.
- Type consistency: later plans consume `CapabilityRegistry`, `RunOperation`,
  `ValidationTarget`, `ValidationDecision`, `ErrorDetail`, and
  `PersistentRunReport` exactly as defined here.
- Release continuity: Task 5 isolates the existing mechanical format drift;
  Tasks 1-4 retain verified-change behavior before the gate is tightened.
