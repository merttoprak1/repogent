# Security Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a threat-assumption-bound security-fix workflow whose evidence cannot overclaim comprehensive audit coverage.

**Architecture:** Normalize a structured finding or bounded user scenario into one typed threat contract, generate a specialized plan and patch, and compose the full mutating approval/recovery sequence. Typed results and fixed report copy limit claims to named scenarios and checks.

**Tech Stack:** Python 3.11+, Pydantic v2, pytest, Bandit, FastMCP

## Global Constraints

- Inputs are one structured finding or one bounded user-described scenario.
- Threat assumptions, exclusions, affected scope, and evidence are explicit.
- Mutation requires fresh requirements, plan, executor, and exact patch decisions.
- Results may claim only named regressions/scenarios/checks.
- No result or renderer can claim a comprehensive security audit.

---

### Task 1: Define and normalize security inputs

**Files:**
- Create: `src/repogent/security_inputs.py`
- Test: `tests/unit/test_security_inputs.py`

**Interfaces:**
- Produces: `StructuredSecurityFinding`, `SecurityScenario`, `SecurityInput`
- Produces: `ThreatAssumption`, `SecurityInputNormalizer.normalize(value)`

- [ ] **Step 1: Write failing normalization tests**

```python
def test_structured_finding_becomes_explicit_threat_assumption() -> None:
    threat = SecurityInputNormalizer().normalize(
        StructuredSecurityFinding(
            identifier="CVE-2099-0001",
            summary="path traversal",
            affected_paths=["src/app/files.py"],
            attacker_capabilities=["controls filename"],
        )
    )
    assert threat.attacker_capabilities == ["controls filename"]
    assert threat.excluded_assumptions


def test_unbounded_scenario_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SecurityScenario(description="x" * 10_001)
```

Add empty affected scope, escaping/protected paths, unknown severity, duplicate
evidence, secret redaction, and ambiguous assets cases.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_security_inputs.py -q`

Expected: module is absent.

- [ ] **Step 3: Implement discriminated input and threat models**

```python
class StructuredSecurityFinding(VersionedModel):
    kind: Literal["finding"] = "finding"
    identifier: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=2_048)
    affected_paths: list[str] = Field(min_length=1, max_length=20)
    attacker_capabilities: list[str] = Field(min_length=1, max_length=20)


class SecurityScenario(VersionedModel):
    kind: Literal["scenario"] = "scenario"
    description: str = Field(min_length=1, max_length=10_000)
    affected_paths: list[str] = Field(default_factory=list, max_length=20)


SecurityInput = Annotated[
    StructuredSecurityFinding | SecurityScenario, Field(discriminator="kind")
]


class ThreatAssumption(VersionedModel):
    summary: str
    affected_assets: list[str] = Field(min_length=1, max_length=20)
    affected_paths: list[str] = Field(max_length=20)
    attacker_capabilities: list[str] = Field(min_length=1, max_length=20)
    excluded_assumptions: list[str] = Field(min_length=1, max_length=20)
    acceptance_evidence: list[str] = Field(min_length=1, max_length=20)
```

Normalize paths against `RepositoryScope`; generate explicit exclusions for
unknown deployment, identity, network, and dependency facts rather than
silently assuming them.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_security_inputs.py -q`

Expected: all tests pass.

```bash
git add src/repogent/security_inputs.py tests/unit/test_security_inputs.py
git commit -m "feat: type bounded security assumptions"
```

---

### Task 2: Define security evidence and fixed claims

**Files:**
- Create: `src/repogent/security_fix.py`
- Modify: `src/repogent/run_reports.py`
- Test: `tests/unit/test_security_fix.py`
- Test: `tests/unit/test_reporting.py`

**Interfaces:**
- Produces: `SecurityEvidence`, `SecurityFixResult`
- Produces: `render_security_claims(result) -> list[str]`

- [ ] **Step 1: Write failing evidence and anti-overclaim tests**

```python
def test_result_names_only_observed_security_evidence() -> None:
    result = security_result(
        blocked_scenarios=["encoded ../ path rejected"],
        passed_regressions=["test_encoded_traversal"],
    )
    rendered = " ".join(render_security_claims(result)).lower()
    assert "encoded ../ path rejected" in rendered
    assert "comprehensive" not in rendered
    assert "audit" not in rendered


def test_result_requires_threat_assumption() -> None:
    with pytest.raises(ValidationError):
        SecurityFixResult.model_validate({"kind": "security_fix"})
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_security_fix.py tests/unit/test_reporting.py -q`

Expected: result types are absent.

- [ ] **Step 3: Implement closed evidence schema and fixed renderer**

```python
class SecurityEvidence(VersionedModel):
    blocked_scenarios: list[str] = Field(default_factory=list, max_length=20)
    passed_regressions: list[str] = Field(default_factory=list, max_length=20)
    passed_security_checks: list[str] = Field(default_factory=list, max_length=20)
    failed_security_checks: list[str] = Field(default_factory=list, max_length=20)


class SecurityFixResult(VersionedModel):
    kind: Literal[WorkflowKind.SECURITY_FIX] = WorkflowKind.SECURITY_FIX
    threat: ThreatAssumption
    evidence: SecurityEvidence
    applied_paths: list[str] = Field(max_length=20)
```

Add `SecurityFixResult` to the existing discriminated `CapabilityResult` union.

Do not include a free-form overall security conclusion. Render the fixed prefix
“Evidence for the specified bounded scenario” followed by typed fields.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_security_fix.py tests/unit/test_reporting.py -q`

Expected: all tests pass.

```bash
git add src/repogent/security_fix.py src/repogent/run_reports.py tests/unit/test_security_fix.py tests/unit/test_reporting.py
git commit -m "feat: constrain security evidence claims"
```

---

### Task 3: Implement dedicated security workflow

**Files:**
- Modify: `src/repogent/security_fix.py`
- Modify: `src/repogent/run_builder.py`
- Test: `tests/unit/test_security_fix_workflow.py`
- Create: `tests/integration/test_security_fix_end_to_end.py`

**Interfaces:**
- Produces: `SecurityFixWorkflow.run() -> RunManifest`
- Produces: `build_security_fix(options: SecurityFixOptions, approver_factory: Callable[[str], Approver], *, executor_selector_factory: ExecutorSelectorFactory | None = None, cancel_requested: Callable[[], bool] | None = None) -> PreparedRun`

- [ ] **Step 1: Write failing state-machine tests**

```python
def test_security_fix_uses_fresh_full_mutation_sequence(workflow) -> None:
    snapshot = workflow.start()
    assert snapshot.pending_approval.kind is ApprovalKind.REQUIREMENTS
    snapshot = approve_current(snapshot)
    assert snapshot.pending_approval.kind is ApprovalKind.PLAN
    snapshot = approve_current(snapshot)
    assert snapshot.pending_execution.target.kind is ValidationTargetKind.PATCH


def test_failed_attack_regression_prevents_patch_gate(workflow) -> None:
    terminal = workflow.with_failed_scenario().run()
    assert terminal.outcome is WorkflowOutcome.HUMAN_INTERVENTION_REQUIRED
    assert terminal.checkout_state is CheckoutState.NOT_APPLIED
```

Add rejected assumptions, out-of-scope path, validation failure, uncertain
executor/application delivery, post-apply failure, and successful bounded fix.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_security_fix_workflow.py -q`

Expected: workflow is absent.

- [ ] **Step 3: Compose specialized workflow**

Own security requirements, plan, patch policy, and evidence mapping. Reuse
trusted approval, executor, candidate, recovery, application, and validation
services without a mode branch in verified change. Patch paths must intersect
approved affected scope or approved regression tests.

- [ ] **Step 4: Run integration tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_security_fix_workflow.py tests/integration/test_security_fix_end_to_end.py -q --no-cov`

Expected: all tests pass; report language remains bounded after success/failure.

```bash
git add src/repogent/security_fix.py src/repogent/run_builder.py tests/unit/test_security_fix_workflow.py tests/integration/test_security_fix_end_to_end.py
git commit -m "feat: add bounded security fix workflow"
```

---

### Task 4: Expose MCP and ship security skill

**Files:**
- Modify: `src/repogent/mcp_models.py`
- Modify: `src/repogent/run_sessions.py`
- Modify: `src/repogent/mcp_server.py`
- Create: `plugins/repogent/skills/security-fix/SKILL.md`
- Create: `plugins/repogent/skills/security-fix/agents/openai.yaml`
- Modify: `plugins/repogent/.codex-plugin/plugin.json`
- Modify: `README.md`
- Modify: `docs/security.md`
- Modify: `tests/unit/test_mcp_server.py`
- Modify: `tests/unit/test_plugin_package.py`
- Modify: `tests/plugin/evals.json`

**Interfaces:**
- Produces: `SecurityFixStart`, `SessionManager.start_security_fix`, MCP `start_security_fix`

- [ ] **Step 1: Write failing contracts and pressure cases**

```python
class SecurityFixStart(VersionedModel):
    repository: Path
    security_input: SecurityInput
    provider: Literal["openai", "codex-cli", "scripted"] = "codex-cli"
    executor: Literal["docker", "local", "deferred"] = "deferred"
    output_dir: Path | None = None
```

Pressure “audit everything”, automatic apply, ambiguous “continue”, and missing
threat scope; require bounded claims, fresh gates, or explicit rejection.
Add MCP cases for a wrong approval kind and a stale patch digest, asserting
stable typed policy errors before any mutation service is invoked.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py -q`

Expected: tool and skill are absent.

- [ ] **Step 3: Register tool, session, skill, and docs**

The skill displays assumptions/exclusions before requirements approval and
renders only typed evidence after validation.

- [ ] **Step 4: Run gate and commit**

Run: `make verify PYTHON=.venv/bin/python`

Expected: all checks pass.

```bash
git add src/repogent/mcp_models.py src/repogent/run_sessions.py src/repogent/mcp_server.py plugins/repogent/skills/security-fix plugins/repogent/.codex-plugin/plugin.json README.md docs/security.md tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py tests/plugin/evals.json
git commit -m "docs: ship security fix capability"
```

---

## Plan Self-Review

- Spec coverage: both input forms, explicit threat/exclusions, bounded evidence, full mutation safety, anti-overclaim schema/rendering, integration, MCP, and pressure tests are covered.
- Placeholder scan: models, mappings, commands, and rejection behavior are explicit.
- Type consistency: `SecurityFixStart.security_input` normalizes to the same `ThreatAssumption` stored by `SecurityFixResult`.
