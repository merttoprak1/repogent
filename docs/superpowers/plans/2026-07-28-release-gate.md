# Release Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a commit-bound, read-only release decision with deterministic checks and artifact digests.

**Architecture:** Resolve one immutable Git commit into a disposable workspace, verify identity before and after execution, run a fixed release policy, and inspect wheel/sdist outputs without publishing. Reuse P0 run, executor, error, and report contracts.

**Tech Stack:** Python 3.11+, Git CLI, Pydantic v2, build, zipfile, tarfile, pytest, FastMCP

## Global Constraints

- Reject a dirty checkout and a target that moves.
- Bind the run to one immutable commit.
- Never change source, tags, remotes, or registries.
- Return `RELEASE_VERIFIED` or `RELEASE_BLOCKED`.
- Record a bounded inventory and SHA-256 digest for every artifact.

---

### Task 1: Resolve an immutable release target

**Files:**
- Create: `src/repogent/release_target.py`
- Test: `tests/unit/test_release_target.py`

**Interfaces:**
- Produces: `ReleaseTarget`
- Produces: `ReleaseTargetResolver.resolve(root, ref) -> ReleaseTarget`
- Produces: `ReleaseTargetResolver.verify_unchanged(root, target) -> None`

- [ ] **Step 1: Write failing target tests**

```python
def test_target_binds_commit_tree_and_fingerprint(git_repository: Path) -> None:
    target = ReleaseTargetResolver().resolve(git_repository, "HEAD")
    assert target.commit == run_git(git_repository, "rev-parse", "HEAD")
    assert target.validation_target.kind is ValidationTargetKind.COMMIT


def test_dirty_checkout_is_rejected(git_repository: Path) -> None:
    (git_repository / "dirty.py").write_text("VALUE = 2\n")
    with pytest.raises(RepogentError) as caught:
        ReleaseTargetResolver().resolve(git_repository, "HEAD")
    assert caught.value.detail.code is ErrorCode.POLICY
```

Add staged, unstaged, untracked, missing-ref, non-commit, linked-worktree, and
ref-moved-after-resolution cases.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_release_target.py -q`

Expected: module is absent.

- [ ] **Step 3: Implement fixed Git resolution**

```python
class ReleaseTarget(VersionedModel):
    requested_ref: str = Field(min_length=1, max_length=256)
    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    tree: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    repository_fingerprint: str
    validation_target: ValidationTarget
```

Use fixed `git status --porcelain=v1 -z --untracked-files=all`, `git rev-parse
--verify <ref>^{commit}`, and `git show -s --format=%T <commit>` arrays.
`verify_unchanged` repeats status, commit, tree, and fingerprint checks.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_release_target.py -q`

Expected: all tests pass.

```bash
git add src/repogent/release_target.py tests/unit/test_release_target.py
git commit -m "feat: bind release checks to a commit"
```

---

### Task 2: Validate builds and inspect artifacts

**Files:**
- Create: `src/repogent/release_validation.py`
- Test: `tests/unit/test_release_validation.py`

**Interfaces:**
- Produces: `ReleasePolicy.commands(root: Path) -> Sequence[ValidationCommand]`
- Produces: `ArtifactDigest`, `PackageInspection`, `inspect_distributions(dist_dir)`

- [ ] **Step 1: Write failing policy and archive tests**

```python
def test_policy_requires_the_complete_fixed_gate(project_root: Path) -> None:
    assert [c.name for c in ReleasePolicy().commands(project_root)] == [
        "pytest", "ruff", "format", "mypy", "bandit", "build"
    ]


def test_inspector_records_wheel_and_sdist_digests(dist_dir: Path) -> None:
    results = inspect_distributions(dist_dir)
    assert {item.format for item in results} == {"wheel", "sdist"}
    assert all(len(item.digest.sha256) == 64 for item in results)
```

Add traversal, absolute member, symlink, oversized archive, missing license,
missing `py.typed`, private evidence, and duplicate archive failures.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_release_validation.py -q`

Expected: module is absent.

- [ ] **Step 3: Implement fixed policy and bounded inspectors**

```python
class ArtifactDigest(VersionedModel):
    filename: str
    size_bytes: int = Field(ge=1, le=100_000_000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PackageInspection(VersionedModel):
    format: Literal["wheel", "sdist"]
    digest: ArtifactDigest
    members: list[str] = Field(max_length=10_000)
    metadata_version: str
    package_version: str
```

Run `(python, "-m", "build", "--wheel", "--sdist", "--outdir", dist)` in
the disposable commit workspace. Inspect archives without extraction. Compare
`pyproject.toml`, plugin JSON, skills, and live MCP schema versions; represent
disagreement as failed `CheckResult` values and never rewrite versions.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_release_validation.py tests/unit/test_execution.py -q`

Expected: all tests pass.

```bash
git add src/repogent/release_validation.py tests/unit/test_release_validation.py
git commit -m "feat: inspect release artifacts deterministically"
```

---

### Task 3: Implement workflow and report

**Files:**
- Create: `src/repogent/release_gate.py`
- Modify: `src/repogent/run_reports.py`
- Test: `tests/unit/test_release_gate.py`
- Test: `tests/unit/test_run_reports.py`

**Interfaces:**
- Produces: `ReleaseGateResult`
- Produces: `ReleaseGateWorkflow.run() -> RunManifest`
- Consumes: `ReleaseTargetResolver`, `ReleasePolicy`, `ExecutorSelector`

- [ ] **Step 1: Write failing workflow tests**

```python
def test_passing_gate_is_commit_bound(release_workflow) -> None:
    manifest = release_workflow.run()
    assert manifest.outcome is WorkflowOutcome.RELEASE_VERIFIED
    assert release_workflow.result.artifacts
    assert manifest.checkout_state is CheckoutState.NOT_APPLIED


def test_moving_target_blocks_release(release_workflow) -> None:
    release_workflow.after_validation = move_head
    manifest = release_workflow.run()
    assert manifest.outcome is WorkflowOutcome.RELEASE_BLOCKED
    assert release_workflow.result.target_unchanged is False
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_release_gate.py -q`

Expected: workflow is absent.

- [ ] **Step 3: Implement result and state machine**

```python
class ReleaseGateResult(VersionedModel):
    kind: Literal[WorkflowKind.RELEASE_GATE] = WorkflowKind.RELEASE_GATE
    target: ReleaseTarget
    target_unchanged: bool
    version_consistent: bool
    artifacts: list[PackageInspection] = Field(default_factory=list, max_length=10)
```

Add `ReleaseGateResult` to the existing discriminated `CapabilityResult` union.

Resolve and store the target, select an executor, validate a disposable commit
workspace, inspect artifacts, and verify the real target again. Required
failure or drift maps to `RELEASE_BLOCKED`. Add tag, publish, upload, and patch
tripwires that must receive zero calls.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_release_gate.py tests/unit/test_run_reports.py -q`

Expected: all tests pass and the report states `checkout_changed: false`.

```bash
git add src/repogent/release_gate.py src/repogent/run_reports.py tests/unit/test_release_gate.py tests/unit/test_run_reports.py
git commit -m "feat: add commit-bound release gate"
```

---

### Task 4: Expose release gate and skill

**Files:**
- Modify: `src/repogent/mcp_models.py`
- Modify: `src/repogent/run_sessions.py`
- Modify: `src/repogent/mcp_server.py`
- Create: `plugins/repogent/skills/release-gate/SKILL.md`
- Create: `plugins/repogent/skills/release-gate/agents/openai.yaml`
- Modify: `plugins/repogent/.codex-plugin/plugin.json`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/security.md`
- Modify: `tests/unit/test_mcp_server.py`
- Modify: `tests/unit/test_plugin_package.py`
- Create: `tests/integration/test_release_gate_end_to_end.py`

**Interfaces:**
- Produces: `ReleaseGateStart`
- Produces: `SessionManager.start_release_gate(request) -> RunSnapshot`
- Produces: MCP `start_release_gate` and skill `release-gate`

- [ ] **Step 1: Write failing contracts**

```python
def test_release_start_is_read_only(tool_map) -> None:
    tool = tool_map["start_release_gate"]
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert "ReleaseGateStart" in tool.inputSchema["$defs"]
```

The integration test independently hashes artifacts, blocks a dirty checkout,
simulates uncertain executor-selection delivery, submits `approve_patch` to
verify `operation_not_allowed`, and proves no tag or source change occurred.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py tests/integration/test_release_gate_end_to_end.py -q --no-cov`

Expected: tool and skill are absent.

- [ ] **Step 3: Add model, factory, tool, skill, and docs**

```python
class ReleaseGateStart(VersionedModel):
    repository: Path
    ref: str = Field(default="HEAD", min_length=1, max_length=256)
    executor: Literal["docker", "local", "deferred"] = "deferred"
    output_dir: Path | None = None
```

The skill calls readiness/start/get/select/cancel/report only and states that a
verified release is not published.

- [ ] **Step 4: Run gate and commit**

Run: `make verify PYTHON=.venv/bin/python`

Expected: all checks pass and package identity includes `release-gate`.

```bash
git add src/repogent/mcp_models.py src/repogent/run_sessions.py src/repogent/mcp_server.py plugins/repogent/skills/release-gate plugins/repogent/.codex-plugin/plugin.json README.md docs/architecture.md docs/security.md tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py tests/integration/test_release_gate_end_to_end.py
git commit -m "docs: ship release gate capability"
```

---

## Plan Self-Review

- Spec coverage: immutable target, fixed checks, version identity, archive inspection, digests, no publishing, MCP, skill, and integration are covered.
- Placeholder scan: models, commands, mappings, and assertions are explicit.
- Type consistency: selection uses P0 `ValidationTarget(COMMIT)` and reports use `ReleaseGateResult` inside `PersistentRunReport`.
