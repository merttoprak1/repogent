# Patch Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent, read-only review for an exact working-copy diff or pair of local Git refs.

**Architecture:** Resolve one bounded immutable diff, run deterministic static review, and reuse the P0 validation-target gate for optional fixed checks. A dedicated workflow emits typed findings and has no mutation channel.

**Tech Stack:** Python 3.11+, Pydantic v2, Git CLI, FastMCP, pytest

## Global Constraints

- Patch review never repairs, applies, or changes the reviewed diff.
- Inputs are exactly one working-copy selection or one local ref range.
- Executor consent binds to the exact diff digest and authorizes validation only.
- Outcomes are `APPROVE`, `REQUEST_CHANGES`, or `INCONCLUSIVE`.
- Hosting-provider metadata and downloads remain outside the core.

---

### Task 1: Resolve and bound exact Git diffs

**Files:**
- Create: `src/repogent/diff_inputs.py`
- Test: `tests/unit/test_diff_inputs.py`

**Interfaces:**
- Produces: `WorkingCopyDiff`, `RefRangeDiff`, `PatchReviewSource`, `ResolvedDiff`
- Produces: `GitDiffResolver.resolve(root, source) -> ResolvedDiff`

- [ ] **Step 1: Write failing resolver tests**

```python
def test_ref_range_binds_commits_and_diff(git_repository: Path) -> None:
    result = GitDiffResolver().resolve(
        git_repository, RefRangeDiff(base="HEAD~1", head="HEAD")
    )
    assert result.target.kind is ValidationTargetKind.DIFF
    assert result.target.digest == sha256_text(result.diff)


def test_working_copy_includes_staged_unstaged_and_untracked(git_repository: Path) -> None:
    result = GitDiffResolver().resolve(git_repository, WorkingCopyDiff())
    assert {"staged.py", "unstaged.py", "new.py"} <= set(result.paths)
```

Add cases for missing/moving refs, identical refs, binary patches, symlinks,
protected paths, output truncation, timeout, and path-count limits.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_diff_inputs.py -q`

Expected: collection fails because `repogent.diff_inputs` is absent.

- [ ] **Step 3: Implement models and fixed Git resolution**

```python
class WorkingCopyDiff(VersionedModel):
    kind: Literal["working_copy"] = "working_copy"


class RefRangeDiff(VersionedModel):
    kind: Literal["ref_range"] = "ref_range"
    base: str = Field(min_length=1, max_length=256)
    head: str = Field(min_length=1, max_length=256)


PatchReviewSource = Annotated[WorkingCopyDiff | RefRangeDiff, Field(discriminator="kind")]


class ResolvedDiff(VersionedModel):
    diff: str = Field(min_length=1, max_length=256_000)
    paths: list[str] = Field(min_length=1, max_length=20)
    base_commit: str | None
    head_commit: str | None
    target: ValidationTarget
```

Use fixed `git diff --binary --no-ext-diff`, `git diff --cached`, `git
ls-files --others --exclude-standard`, and `git rev-parse --verify
<ref>^{commit}` arrays. Convert eligible untracked UTF-8 regular files to
bounded `/dev/null` diffs. Reuse `PatchPolicy.validate` without `PatchApplier`.

- [ ] **Step 4: Run resolver tests and verify GREEN**

Run: `.venv/bin/python -m pytest tests/unit/test_diff_inputs.py tests/unit/test_patching.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/repogent/diff_inputs.py tests/unit/test_diff_inputs.py
git commit -m "feat: resolve bounded review diffs"
```

---

### Task 2: Implement review workflow and result

**Files:**
- Create: `src/repogent/patch_review.py`
- Modify: `src/repogent/run_reports.py`
- Test: `tests/unit/test_patch_review.py`
- Test: `tests/unit/test_run_reports.py`

**Interfaces:**
- Produces: `FindingSeverity`, `ReviewFinding`, `PatchReviewResult`
- Produces: `PatchReviewWorkflow.run() -> RunManifest`
- Consumes: `ResolvedDiff`, `ValidationTarget`, `ExecutorSelector`, `Validator`

- [ ] **Step 1: Write failing workflow tests**

```python
def test_blocking_finding_requests_changes_without_mutation(review_workflow) -> None:
    manifest = review_workflow.run()
    assert manifest.outcome is WorkflowOutcome.REQUEST_CHANGES
    assert review_workflow.patch_applier_calls == 0
    assert manifest.checkout_state is CheckoutState.NOT_APPLIED


def test_static_review_can_finish_unvalidated(review_workflow) -> None:
    manifest = review_workflow.without_executor().run()
    assert manifest.outcome is WorkflowOutcome.APPROVE
    assert manifest.verification_status is VerificationStatus.UNVALIDATED
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_patch_review.py -q`

Expected: workflow types are absent.

- [ ] **Step 3: Implement typed findings and state machine**

```python
class ReviewFinding(VersionedModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    severity: Literal["error", "warning", "info"]
    path: str
    line: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1, max_length=1_024)
    evidence: str = Field(min_length=1, max_length=2_048)


class PatchReviewResult(VersionedModel):
    kind: Literal[WorkflowKind.PATCH_REVIEW] = WorkflowKind.PATCH_REVIEW
    target: ValidationTarget
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=100)
    merge_assessment: WorkflowOutcome
```

Change `CapabilityResult` to
`Annotated[VerifiedChangeResult | PatchReviewResult, Field(discriminator="kind")]`.

Store resolved diff, findings, optional validation, and typed/human reports.
Static checks cover malformed hunks, protected paths, binary changes,
executable-bit changes, oversized changes, and test deletion. Errors map to
`REQUEST_CHANGES`; resolver/validation uncertainty maps to `INCONCLUSIVE`.

- [ ] **Step 4: Add mutation tripwires and run tests**

Inject an `apply()` method that raises `AssertionError`; assert zero calls on
golden, failure, cancellation, timeout, and report-error paths.

Run: `.venv/bin/python -m pytest tests/unit/test_patch_review.py tests/unit/test_run_reports.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/repogent/patch_review.py src/repogent/run_reports.py tests/unit/test_patch_review.py tests/unit/test_run_reports.py
git commit -m "feat: add read-only patch review workflow"
```

---

### Task 3: Expose sessions and MCP

**Files:**
- Modify: `src/repogent/mcp_models.py`
- Modify: `src/repogent/run_sessions.py`
- Modify: `src/repogent/mcp_server.py`
- Test: `tests/unit/test_run_sessions.py`
- Test: `tests/unit/test_mcp_server.py`
- Create: `tests/integration/test_patch_review_end_to_end.py`

**Interfaces:**
- Produces: `PatchReviewStart`
- Produces: `SessionManager.start_patch_review(request) -> RunSnapshot`
- Produces: MCP `start_patch_review`

- [ ] **Step 1: Write failing tool and policy tests**

```python
def test_patch_review_tool_is_read_only(tool_map) -> None:
    tool = tool_map["start_patch_review"]
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False


def test_review_run_rejects_patch_approval(manager, review_request) -> None:
    snapshot = manager.start_patch_review(review_request)
    with pytest.raises(RepogentError) as caught:
        manager.decide(patch_decision(snapshot.run_id))
    assert caught.value.detail.code is ErrorCode.OPERATION_NOT_ALLOWED
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_mcp_server.py tests/unit/test_run_sessions.py -q`

Expected: start surface is missing.

- [ ] **Step 3: Add start model and register workflow**

```python
class PatchReviewStart(VersionedModel):
    repository: Path
    source: PatchReviewSource
    executor: Literal["none", "deferred"] = "none"
    output_dir: Path | None = None
```

Register its builder under `WorkflowKind.PATCH_REVIEW`. Annotate the start tool
read-only, non-destructive, non-idempotent, and closed-world.

- [ ] **Step 4: Add real-Git end-to-end coverage**

Start both static-only and deferred review, reconcile executor selection,
simulate uncertain executor-selection delivery before retry, fetch the report,
and compare the checkout fingerprint before and after. Submit `approve_patch`
and assert `operation_not_allowed` without calling a decision channel.

Run: `.venv/bin/python -m pytest tests/unit/test_mcp_server.py tests/unit/test_run_sessions.py tests/integration/test_patch_review_end_to_end.py -q --no-cov`

Expected: all tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/repogent/mcp_models.py src/repogent/run_sessions.py src/repogent/mcp_server.py tests/unit/test_mcp_server.py tests/unit/test_run_sessions.py tests/integration/test_patch_review_end_to_end.py
git commit -m "feat: expose typed patch review"
```

---

### Task 4: Ship review skill and docs

**Files:**
- Create: `plugins/repogent/skills/review-patch/SKILL.md`
- Create: `plugins/repogent/skills/review-patch/agents/openai.yaml`
- Modify: `plugins/repogent/.codex-plugin/plugin.json`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/security.md`
- Modify: `tests/plugin/evals.json`
- Modify: `tests/unit/test_plugin_package.py`

**Interfaces:**
- Consumes: start/get/select/cancel/report tools only
- Produces: `review-patch` skill with no approval or mutation tools

- [ ] **Step 1: Write failing package and pressure assertions**

```python
def test_review_skill_has_no_mutation_tools() -> None:
    skill = REVIEW_SKILL_PATH.read_text()
    assert "`start_patch_review`" in skill
    assert all(name not in skill for name in ("approve_plan", "approve_patch"))
```

Add pressure cases for “review and fix it”, ambiguous “continue”, and validation
timeout; require review-only, reconcile-first behavior and explicit trust.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_plugin_package.py -q`

Expected: skill is absent.

- [ ] **Step 3: Write skill, metadata, and documentation**

The skill displays the exact target, treats executor selection as validation
only, reports findings first, and refuses repair/application.

- [ ] **Step 4: Run gate and commit**

Run: `make verify PYTHON=.venv/bin/python`

Expected: all checks pass and package identity lists three skills.

```bash
git add plugins/repogent/skills/review-patch plugins/repogent/.codex-plugin/plugin.json README.md docs/architecture.md docs/security.md tests/plugin/evals.json tests/unit/test_plugin_package.py
git commit -m "docs: ship patch review capability"
```

---

## Plan Self-Review

- Spec coverage: both inputs, exact binding, static review, optional validation, read-only enforcement, outcomes, integration, and skill pressure tests are covered.
- Placeholder scan: all steps identify concrete models, commands, and expected results.
- Type consistency: `PatchReviewStart.source` uses `PatchReviewSource`; reports use P0 `ValidationTarget` and `PersistentRunReport`.
