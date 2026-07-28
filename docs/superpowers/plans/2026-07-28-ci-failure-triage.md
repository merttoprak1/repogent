# CI Failure Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, read-only CI failure analysis that produces typed root-cause candidates and a provenance-bound repair request.

**Architecture:** Sanitize and segment caller-supplied logs before persistence or provider context, correlate segments with the Git-bounded repository, and run a dedicated read-only workflow. A repair is a new verified-change run with fresh authority, linked only by source run ID and repair digest.

**Tech Stack:** Python 3.11+, Pydantic v2, FastMCP, pytest

## Global Constraints

- Core never downloads CI-provider logs.
- Raw unbounded logs never enter evidence or a model prompt.
- CI triage never edits or applies a patch.
- Repair handoff carries provenance but no approval or executor consent.
- Outcomes are `ROOT_CAUSE_IDENTIFIED`, `CANDIDATES_FOUND`, or `INCONCLUSIVE`.

---

### Task 1: Bound, redact, and segment CI inputs

**Files:**
- Create: `src/repogent/ci_inputs.py`
- Test: `tests/unit/test_ci_inputs.py`

**Interfaces:**
- Produces: `CILogInput`, `CIArtifactSummary`, `SanitizedCIBundle`, `CIBundleProcessor.process(logs: Sequence[CILogInput], artifacts: Sequence[CIArtifactSummary]) -> SanitizedCIBundle`

- [ ] **Step 1: Write failing safety tests**

```python
def test_processor_redacts_before_segment_persistence() -> None:
    bundle = CIBundleProcessor(max_total_bytes=10_000).process(
        [CILogInput(name="pytest", text="token=ghp_abcdefghijklmnopqrstuvwxyz123")], []
    )
    assert "ghp_" not in bundle.model_dump_json()
    assert bundle.segments[0].text == "token=[REDACTED]"


def test_total_input_limit_fails_closed() -> None:
    with pytest.raises(RepogentError) as caught:
        CIBundleProcessor(max_total_bytes=10).process(
            [CILogInput(name="job", text="x" * 11)], []
        )
    assert caught.value.detail.code is ErrorCode.LIMIT_EXCEEDED
```

Add count, per-log, line-length, binary/NUL, ANSI, repeated-line collapse, and
secret split-across-chunks cases.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_ci_inputs.py -q`

Expected: module is absent.

- [ ] **Step 3: Implement bounded models and processor**

```python
class CILogInput(VersionedModel):
    name: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=256_000)


class CIArtifactSummary(VersionedModel):
    name: str = Field(min_length=1, max_length=128)
    media_type: str = Field(max_length=128)
    summary: str = Field(min_length=1, max_length=8_192)


class SanitizedCIBundle(VersionedModel):
    segments: list[FailureSegment] = Field(max_length=200)
    artifacts: list[CIArtifactSummary] = Field(max_length=20)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    redaction_count: int = Field(ge=0)
    omitted_bytes: int = Field(ge=0)
```

Apply `sanitize_data` and explicit secret patterns to the whole bounded input
before segmentation. Persist only `SanitizedCIBundle`; retain counts and digest
instead of raw input.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_ci_inputs.py tests/unit/test_sanitization.py -q`

Expected: all tests pass.

```bash
git add src/repogent/ci_inputs.py tests/unit/test_ci_inputs.py
git commit -m "feat: sanitize bounded CI evidence"
```

---

### Task 2: Implement triage candidates and repair request

**Files:**
- Create: `src/repogent/ci_triage.py`
- Modify: `src/repogent/run_reports.py`
- Test: `tests/unit/test_ci_triage.py`
- Test: `tests/unit/test_run_reports.py`

**Interfaces:**
- Produces: `RootCauseCandidate`, `RepairRequest`, `CITriageResult`
- Produces: `CITriageWorkflow.run() -> RunManifest`

- [ ] **Step 1: Write failing workflow tests**

```python
def test_identified_cause_produces_digest_bound_repair(triage_workflow) -> None:
    manifest = triage_workflow.run()
    assert manifest.outcome is WorkflowOutcome.ROOT_CAUSE_IDENTIFIED
    assert triage_workflow.result.repair_request.source_run_id == manifest.run_id
    assert len(triage_workflow.result.repair_request.digest) == 64
    assert manifest.checkout_state is CheckoutState.NOT_APPLIED
```

Add multiple candidates, no correlation, cancellation, provider failure,
mutation-tripwire, and report-redaction cases.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_ci_triage.py -q`

Expected: workflow is absent.

- [ ] **Step 3: Implement typed correlation output**

```python
class RootCauseCandidate(VersionedModel):
    rank: int = Field(ge=1, le=10)
    summary: str = Field(min_length=1, max_length=1_024)
    paths: list[str] = Field(max_length=20)
    evidence_segment_ids: list[str] = Field(max_length=20)
    confidence: float = Field(ge=0, le=1)


class RepairRequest(VersionedModel):
    source_run_id: str
    objective: str = Field(min_length=1, max_length=4_096)
    affected_paths: list[str] = Field(max_length=20)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    assumptions: list[str] = Field(max_length=20)
    evidence_refs: list[str] = Field(max_length=20)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CITriageResult(VersionedModel):
    kind: Literal[WorkflowKind.CI_TRIAGE] = WorkflowKind.CI_TRIAGE
    candidates: list[RootCauseCandidate] = Field(max_length=10)
    repair_request: RepairRequest | None
```

Add `CITriageResult` to the existing discriminated `CapabilityResult` union.

Use existing lexical localization against sanitized segments. Only create a
repair request when at least one bounded candidate has repository evidence.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_ci_triage.py tests/unit/test_run_reports.py -q`

Expected: all tests pass.

```bash
git add src/repogent/ci_triage.py src/repogent/run_reports.py tests/unit/test_ci_triage.py tests/unit/test_run_reports.py
git commit -m "feat: add typed CI failure triage"
```

---

### Task 3: Add fresh-authority repair handoff

**Files:**
- Modify: `src/repogent/mcp_models.py`
- Modify: `src/repogent/run_sessions.py`
- Test: `tests/unit/test_run_sessions.py`
- Test: `tests/unit/test_domain.py`

**Interfaces:**
- Extends: `VerifiedChangeStart` with `source_repair: RepairProvenance | None`
- Produces: `RepairProvenance(source_run_id, repair_digest)`

- [ ] **Step 1: Write failing non-inheritance tests**

```python
def test_repair_handoff_starts_at_fresh_requirements_gate(manager, triage_result) -> None:
    start = VerifiedChangeStart(
        repository=repository,
        request=triage_result.repair_request.objective,
        source_repair=RepairProvenance(
            source_run_id=triage_result.repair_request.source_run_id,
            repair_digest=triage_result.repair_request.digest,
        ),
    )
    snapshot = manager.start_verified_change(start)
    assert snapshot.pending_approval.kind is ApprovalKind.REQUIREMENTS
    assert snapshot.execution_mode is None
```

Add unknown source, non-terminal source, stale digest, and attempted approval
reuse cases.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_run_sessions.py tests/unit/test_domain.py -q`

Expected: provenance model is absent.

- [ ] **Step 3: Implement provenance validation**

```python
class RepairProvenance(VersionedModel):
    source_run_id: str = Field(min_length=1, max_length=256)
    repair_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
```

Validate the source report and digest, copy only provenance into the new
manifest, and construct fresh approver/executor channels.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_run_sessions.py tests/unit/test_domain.py -q`

Expected: all tests pass.

```bash
git add src/repogent/mcp_models.py src/repogent/run_sessions.py tests/unit/test_run_sessions.py tests/unit/test_domain.py
git commit -m "feat: link CI repair with fresh authority"
```

---

### Task 4: Expose triage and ship skill

**Files:**
- Modify: `src/repogent/mcp_models.py`
- Modify: `src/repogent/run_sessions.py`
- Modify: `src/repogent/mcp_server.py`
- Create: `plugins/repogent/skills/ci-failure-triage/SKILL.md`
- Create: `plugins/repogent/skills/ci-failure-triage/agents/openai.yaml`
- Modify: `plugins/repogent/.codex-plugin/plugin.json`
- Modify: `README.md`
- Modify: `tests/unit/test_mcp_server.py`
- Modify: `tests/unit/test_plugin_package.py`
- Create: `tests/integration/test_ci_triage_end_to_end.py`

**Interfaces:**
- Produces: `CITriageStart`, `SessionManager.start_ci_triage`, MCP `start_ci_triage`
- Produces: `ci-failure-triage` skill

- [ ] **Step 1: Write failing contracts**

```python
class CITriageStart(VersionedModel):
    repository: Path
    logs: list[CILogInput] = Field(min_length=1, max_length=20)
    artifacts: list[CIArtifactSummary] = Field(default_factory=list, max_length=20)
    provider: Literal["openai", "codex-cli", "scripted"] = "codex-cli"
    output_dir: Path | None = None
```

Assert the tool is read-only and the skill contains no executor or patch tools.
Pressure “triage and fix” to require a displayed repair request and a new run.
Submit `select_executor` and `approve_patch` against the triage run and assert
typed `operation_not_allowed` responses without invoking workflow channels.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py -q`

Expected: tool and skill are absent.

- [ ] **Step 3: Register tool, session, skill, integration, and docs**

The end-to-end test proves raw secrets are absent from evidence and chat-facing
results, checkout fingerprint is unchanged, and repair starts at requirements.

- [ ] **Step 4: Run gate and commit**

Run: `make verify PYTHON=.venv/bin/python`

Expected: all checks pass.

```bash
git add src/repogent/mcp_models.py src/repogent/run_sessions.py src/repogent/mcp_server.py plugins/repogent/skills/ci-failure-triage plugins/repogent/.codex-plugin/plugin.json README.md tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py tests/integration/test_ci_triage_end_to_end.py
git commit -m "docs: ship CI failure triage"
```

---

## Plan Self-Review

- Spec coverage: bounded ingestion, pre-prompt redaction, clustering, correlation, outcomes, typed repair, fresh authority, MCP, integration, and skill pressure are covered.
- Placeholder scan: limits, models, mappings, and commands are explicit.
- Type consistency: repair handoff uses `RepairRequest.digest` through `RepairProvenance.repair_digest` and starts existing `VerifiedChangeStart`.
