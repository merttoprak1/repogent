# Python Dependency Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Python-only dependency update workflow with explicit ecosystem support, manifest/lock consistency evidence, and the full mutation safety sequence.

**Architecture:** Detect one supported manifest family deterministically, construct typed dependency requirements, and compose existing proposal, approval, validation, recovery, and application services inside a dedicated workflow. Unsupported or ambiguous ecosystems fail before provider work.

**Tech Stack:** Python 3.11+, Pydantic v2, tomllib, packaging, pytest, FastMCP

## Global Constraints

- Support only `pyproject.toml`, `requirements*.txt`, `constraints*.txt`, `poetry.lock`, `uv.lock`, `Pipfile`, and `Pipfile.lock`.
- Unsupported ecosystems fail explicitly and never fall back to verified change.
- A present lockfile requires manifest/lock consistency evidence.
- Mutation occurs only after fresh requirements, plan, executor, and exact patch decisions.
- Uncertain application uses the existing reconcile-first recovery contract.

---

### Task 1: Detect and model supported dependency files

**Files:**
- Create: `src/repogent/dependency_manifests.py`
- Test: `tests/unit/test_dependency_manifests.py`

**Interfaces:**
- Produces: `DependencyEcosystem`, `DependencyFileSet`, `DependencyManifestDetector.detect(root, scope)`

- [ ] **Step 1: Write failing detection tests**

```python
@pytest.mark.parametrize(
    ("files", "ecosystem"),
    [
        (["pyproject.toml", "uv.lock"], DependencyEcosystem.UV),
        (["pyproject.toml", "poetry.lock"], DependencyEcosystem.POETRY),
        (["Pipfile", "Pipfile.lock"], DependencyEcosystem.PIPENV),
        (["requirements.txt"], DependencyEcosystem.REQUIREMENTS),
    ],
)
def test_detects_supported_family(repository, files, ecosystem) -> None:
    assert detector(repository, files).ecosystem is ecosystem
```

Add ambiguous Poetry+uv, npm-only, no manifest, symlink, ignored manifest, too
many requirements files, and constraints association cases.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_dependency_manifests.py -q`

Expected: module is absent.

- [ ] **Step 3: Implement bounded detection**

```python
class DependencyEcosystem(StrEnum):
    PEP621 = "pep621"
    REQUIREMENTS = "requirements"
    POETRY = "poetry"
    UV = "uv"
    PIPENV = "pipenv"


class DependencyFileSet(VersionedModel):
    ecosystem: DependencyEcosystem
    manifests: list[str] = Field(min_length=1, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    lockfiles: list[str] = Field(default_factory=list, max_length=5)
```

Read only files selected by `RepositoryScope`; reject ambiguity with
`ErrorCode.INVALID_INPUT` and remediation naming supported families.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_dependency_manifests.py -q`

Expected: all tests pass.

```bash
git add src/repogent/dependency_manifests.py tests/unit/test_dependency_manifests.py
git commit -m "feat: detect Python dependency manifests"
```

---

### Task 2: Define dependency intent and consistency evidence

**Files:**
- Create: `src/repogent/dependency_update.py`
- Modify: `src/repogent/run_reports.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/test_dependency_update.py`

**Interfaces:**
- Produces: `DependencyIntent`, `DependencyConsistency`, `DependencyUpdateResult`
- Produces: `check_dependency_consistency(root, files, intent)`

- [ ] **Step 1: Write failing intent and consistency tests**

```python
def test_lockfile_must_record_requested_dependency(uv_project: Path) -> None:
    result = check_dependency_consistency(
        uv_project,
        detected_uv_files(),
        DependencyIntent(name="httpx", constraint=">=0.28,<1"),
    )
    assert result.manifest_consistent is True
    assert result.lock_consistent is True


def test_missing_lock_update_is_required_failure(uv_project_with_stale_lock: Path) -> None:
    result = check_dependency_consistency(
        uv_project_with_stale_lock,
        detected_uv_files(),
        DependencyIntent(name="httpx", constraint=">=0.28,<1"),
    )
    assert "lockfile_consistency" in result.required_failures
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_dependency_update.py -q`

Expected: dependency models are absent.

- [ ] **Step 3: Implement typed intent and parsers**

```python
class DependencyIntent(VersionedModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    constraint: str | None = Field(default=None, max_length=256)
    allow_prerelease: bool = False
    manifest_paths: list[str] = Field(default_factory=list, max_length=20)


class DependencyConsistency(VersionedModel):
    ecosystem: DependencyEcosystem
    manifest_consistent: bool
    lock_consistent: bool | None
    observed_constraints: list[str] = Field(max_length=20)
    required_failures: list[str] = Field(max_length=20)


class DependencyUpdateResult(VersionedModel):
    kind: Literal[WorkflowKind.DEPENDENCY_UPDATE] = WorkflowKind.DEPENDENCY_UPDATE
    intent: DependencyIntent
    files: DependencyFileSet
    consistency: DependencyConsistency
    applied_paths: list[str] = Field(max_length=20)
```

Add `DependencyUpdateResult` to the existing discriminated `CapabilityResult` union.

Use `tomllib` and `packaging` for manifests; parse only the minimum lock fields
needed to prove name/version presence. Never execute a package manager to infer
state. Add `packaging>=24,<27` as a direct runtime dependency because constraint
parsing must not rely on a transitive development dependency.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_dependency_update.py -q`

Expected: all tests pass.

```bash
git add src/repogent/dependency_update.py src/repogent/run_reports.py pyproject.toml tests/unit/test_dependency_update.py
git commit -m "feat: validate dependency consistency"
```

---

### Task 3: Implement dedicated mutating workflow

**Files:**
- Modify: `src/repogent/dependency_update.py`
- Modify: `src/repogent/run_builder.py`
- Test: `tests/unit/test_dependency_update_workflow.py`
- Create: `tests/integration/test_dependency_update_end_to_end.py`

**Interfaces:**
- Produces: `DependencyUpdateWorkflow.run() -> RunManifest`
- Produces: `build_dependency_update(options: DependencyUpdateOptions, approver_factory: Callable[[str], Approver], *, executor_selector_factory: ExecutorSelectorFactory | None = None, cancel_requested: Callable[[], bool] | None = None) -> PreparedRun`
- Consumes: existing approver, candidate, executor, patch, recovery, and validation services

- [ ] **Step 1: Write failing state-machine tests**

```python
def test_update_requires_all_fresh_decisions(update_workflow) -> None:
    snapshot = update_workflow.start()
    assert snapshot.pending_approval.kind is ApprovalKind.REQUIREMENTS
    snapshot = approve_current(snapshot)
    assert snapshot.pending_approval.kind is ApprovalKind.PLAN
    snapshot = approve_current(snapshot)
    assert snapshot.pending_execution.target.kind is ValidationTargetKind.PATCH


def test_patch_with_stale_lock_never_reaches_patch_gate(update_workflow) -> None:
    terminal = update_workflow.with_stale_lock_candidate().run()
    assert terminal.outcome is WorkflowOutcome.HUMAN_INTERVENTION_REQUIRED
    assert terminal.checkout_state is CheckoutState.NOT_APPLIED
```

Add unsupported ecosystem before provider, wrong-file patch, validation failure,
uncertain executor delivery, uncertain application, final validation failure,
and successful apply cases.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_dependency_update_workflow.py -q`

Expected: workflow is absent.

- [ ] **Step 3: Compose the dedicated workflow**

The workflow owns dependency-specific requirements, plan, candidate policy, and
consistency check. It may reuse verified-change services but not subclass or
branch `Workflow` by mode. Restrict patch paths to `DependencyFileSet` plus
tests explicitly named in the approved plan.

- [ ] **Step 4: Run unit and real-fixture integration tests**

Run: `.venv/bin/python -m pytest tests/unit/test_dependency_update_workflow.py tests/integration/test_dependency_update_end_to_end.py -q --no-cov`

Expected: all tests pass and successful evidence includes manifest/lock facts.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/repogent/dependency_update.py src/repogent/run_builder.py tests/unit/test_dependency_update_workflow.py tests/integration/test_dependency_update_end_to_end.py
git commit -m "feat: add dependency update workflow"
```

---

### Task 4: Expose MCP and ship dependency skill

**Files:**
- Modify: `src/repogent/mcp_models.py`
- Modify: `src/repogent/run_sessions.py`
- Modify: `src/repogent/mcp_server.py`
- Create: `plugins/repogent/skills/dependency-update/SKILL.md`
- Create: `plugins/repogent/skills/dependency-update/agents/openai.yaml`
- Modify: `plugins/repogent/.codex-plugin/plugin.json`
- Modify: `README.md`
- Modify: `tests/unit/test_mcp_server.py`
- Modify: `tests/unit/test_plugin_package.py`
- Modify: `tests/plugin/evals.json`

**Interfaces:**
- Produces: `DependencyUpdateStart`, `SessionManager.start_dependency_update`, MCP `start_dependency_update`

- [ ] **Step 1: Write failing MCP and pressure tests**

```python
class DependencyUpdateStart(VersionedModel):
    repository: Path
    intent: DependencyIntent
    provider: Literal["openai", "codex-cli", "scripted"] = "codex-cli"
    executor: Literal["docker", "local", "deferred"] = "deferred"
    output_dir: Path | None = None
```

Pressure npm-only input, “update everything”, ambiguous “continue”, and stale
lockfile; require explicit rejection/bounds and fresh gates.
Submit a legal approval kind to a synthetic read-only run and a review-only
operation to the dependency run to verify both wrong-kind policy directions.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py -q`

Expected: tool and skill are absent.

- [ ] **Step 3: Register surface, skill, metadata, and docs**

The skill selects only dependency tools and renders consistency evidence before
requesting exact patch approval.

- [ ] **Step 4: Run gate and commit**

Run: `make verify PYTHON=.venv/bin/python`

Expected: all checks pass.

```bash
git add src/repogent/mcp_models.py src/repogent/run_sessions.py src/repogent/mcp_server.py plugins/repogent/skills/dependency-update plugins/repogent/.codex-plugin/plugin.json README.md tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py tests/plugin/evals.json
git commit -m "docs: ship dependency update capability"
```

---

## Plan Self-Review

- Spec coverage: every supported family, explicit unsupported failure, consistency evidence, full approvals/recovery, MCP, integration, and pressure tests are covered.
- Placeholder scan: models, parser boundaries, commands, and expected failures are explicit.
- Type consistency: `DependencyUpdateStart.intent` and `DependencyUpdateResult.intent` use the same `DependencyIntent`.
