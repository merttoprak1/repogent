# Capability Foundation and Repository Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish Git-correct repository scope, typed workflow identity and outcomes, migrate the current change flow to `verified-change`, and ship a standalone read-only repository-readiness capability.

**Architecture:** Add a bounded Git path resolver in front of the existing race-resistant repository reader, while retaining the filesystem fallback for non-Git directories. Introduce workflow kind and outcome into shared domain/MCP snapshots, then rename the current generic start surface without changing its approval semantics. Readiness remains a synchronous, read-only service and reports the same bounded scope that later workflows will consume.

**Tech Stack:** Python 3.11+, Pydantic v2, FastMCP, Git CLI, pytest, Ruff, mypy

## Global Constraints

- Git scope is tracked files plus ignore-unmatched untracked files, with sensitive and no-follow filtering applied independently.
- Aggregate limits apply only to selected inventory paths.
- Readiness never runs target repository code, generates a patch, or mutates the checkout.
- `verified-change` preserves all three digest-bound content approvals and the separate executor decision.
- `start_run` and the generic `repogent` skill are removed in the same change that introduces their typed replacements.
- Workflow outcome and evidence trust remain separate fields.
- Every intermediate commit must leave the focused tests green.

---

### Task 1: Resolve a bounded Git repository scope

**Files:**
- Create: `src/repogent/repository_scope.py`
- Modify: `src/repogent/repository.py`
- Test: `tests/unit/test_repository_scope.py`
- Test: `tests/unit/test_repository.py`

**Interfaces:**
- Produces: `ScopeSource`, `RepositoryScope`, and `RepositoryScopeResolver.resolve(root: Path) -> RepositoryScope`
- Consumes: the existing sensitive-path and no-follow rules in `RepositoryInspector`
- Produces for Task 2: `RepositoryInspector.inspect(root, scope=...)`

- [ ] **Step 1: Write failing Git scope tests**

Create a real temporary Git repository and assert that tracked files and
non-ignored untracked files are selected, while ignored files, `.git`, linked
worktree metadata, and an ignored directory whose contents exceed a tiny test
budget are not selected.

```python
def test_git_scope_selects_tracked_and_nonignored_untracked(
    tmp_path: Path,
) -> None:
    repository = init_git_repository(tmp_path)
    (repository / "tracked.py").write_text("VALUE = 1\n")
    run_git(repository, "add", "tracked.py")
    run_git(repository, "commit", "-m", "initial")
    (repository / "new.py").write_text("VALUE = 2\n")
    (repository / ".gitignore").write_text(".worktrees/\n.superpowers/\n")
    (repository / ".worktrees").mkdir()
    (repository / ".worktrees" / "large.bin").write_bytes(b"x" * 128)

    scope = RepositoryScopeResolver(max_output_bytes=8_192).resolve(repository)

    assert scope.source is ScopeSource.GIT
    assert scope.paths == (Path(".gitignore"), Path("new.py"), Path("tracked.py"))
```

Add separate tests for NUL-containing/truncated Git output rejection, a Git
command failure falling back only when the directory is not a Git repository,
and unsupported absolute or escaping paths.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
pytest tests/unit/test_repository_scope.py -q
```

Expected: collection fails because `repogent.repository_scope` does not exist.

- [ ] **Step 3: Implement the minimal scope resolver**

Define these public types:

```python
class ScopeSource(StrEnum):
    GIT = "git"
    FILESYSTEM = "filesystem"


class RepositoryScope(VersionedModel):
    root: Path
    source: ScopeSource
    paths: tuple[Path, ...]
    skipped: tuple[str, ...] = ()


class RepositoryScopeResolver:
    def __init__(
        self,
        *,
        max_paths: int = 50_000,
        max_output_bytes: int = 4_000_000,
        timeout_seconds: float = 10.0,
    ) -> None: ...

    def resolve(self, root: Path) -> RepositoryScope: ...
```

For Git repositories, run the fixed argument vector:

```python
(
    "git",
    "-C",
    str(root),
    "ls-files",
    "-z",
    "--cached",
    "--others",
    "--exclude-standard",
)
```

Bound stdout, timeout, path count, and decoded path length. Reject absolute
paths, `..` components, duplicate canonical paths, malformed output, and a Git
repository whose listing cannot be trusted. Use the filesystem scope only when
`git rev-parse --is-inside-work-tree` proves the directory is not a Git
repository; do not silently fall back after an error inside a Git repository.

- [ ] **Step 4: Run scope tests and verify GREEN**

Run:

```bash
pytest tests/unit/test_repository_scope.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Write failing inventory integration tests**

Add tests demonstrating that:

```python
scope = RepositoryScopeResolver().resolve(repository)
inventory = RepositoryInspector(max_total_bytes=32).inspect(repository, scope=scope)
assert [record.path for record in inventory.files] == [".gitignore", "app.py"]
```

An ignored 128-byte file must not trigger the 32-byte aggregate limit. A
selected 33-byte file must still raise `RepositoryLimitError`. Sensitive files
and symlinks selected by a malicious or fake scope remain skipped.

- [ ] **Step 6: Adapt `RepositoryInspector` to inspect selected paths securely**

Add:

```python
def inspect(
    self,
    root: Path,
    *,
    scope: RepositoryScope | None = None,
    deadline: float | None = None,
) -> RepositoryInventory:
```

Retain the current descriptor-relative, no-follow reads. For a Git scope, walk
only the parent directories required by `scope.paths`; never call `Path.read_*`
on a repository path. For a filesystem scope or `scope=None`, preserve the
existing traversal behavior. Include `scope_source` in
`RepositoryInventory`, defaulting old callers to `filesystem`.

- [ ] **Step 7: Run repository security and regression tests**

Run:

```bash
pytest tests/unit/test_repository_scope.py tests/unit/test_repository.py -q
```

Expected: all tests pass, including existing race, symlink, sensitive-file, and
limit coverage.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/repogent/repository_scope.py src/repogent/repository.py tests/unit/test_repository_scope.py tests/unit/test_repository.py
git commit -m "feat: scope repository inspection to Git project files"
```

---

### Task 2: Use one scope in preflight and workflow construction

**Files:**
- Modify: `src/repogent/preflight.py`
- Modify: `src/repogent/run_builder.py`
- Modify: `src/repogent/doctor.py`
- Test: `tests/unit/test_preflight.py`
- Test: `tests/unit/test_run_builder.py`
- Test: `tests/unit/test_doctor.py`

**Interfaces:**
- Consumes: `RepositoryScopeResolver.resolve`
- Produces: a single `RepositoryScope` retained by `PreparedRun`
- Guarantees: readiness and workflow inventory describe the same selected paths

- [ ] **Step 1: Write failing scope consistency tests**

Inject a recording resolver and inspector. Assert that `build_run` resolves the
scope once, passes the same scope to preflight and inspection, and fails closed
when resolution fails.

```python
assert prepared.scope is resolved_scope
assert inspector.received_scope is resolved_scope
assert preflight.received_scope is resolved_scope
```

Add a doctor test whose repository contains an ignored oversized
`.superpowers/competitor-research` tree and assert readiness remains successful
with `scope_source == "git"`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
pytest tests/unit/test_preflight.py tests/unit/test_run_builder.py tests/unit/test_doctor.py -q
```

Expected: failures because scope is neither injected nor reported.

- [ ] **Step 3: Thread scope through shared construction**

Add optional resolver dependencies to `build_run` and `DoctorService`. Extend:

```python
@dataclass(frozen=True)
class PreparedRun:
    workflow: Workflow
    store: ArtifactStore
    manifest: RunManifest
    scope: RepositoryScope
```

Update repository preflight to accept a resolved scope for Git metadata and
pytest discovery. Configuration files must be considered only when present in
the selected scope, while recognized tracked configuration retains the existing
regular-file/no-follow checks.

- [ ] **Step 4: Expose bounded scope facts in readiness**

Extend the readiness report with:

```python
class RepositoryScopeSummary(VersionedModel):
    source: ScopeSource
    selected_files: int = Field(ge=0)
    aggregate_bytes: int = Field(ge=0)
    skipped_paths: int = Field(ge=0)


class RepositoryReadinessReport(VersionedModel):
    ready: bool
    repository: str
    provider: str
    scope: RepositoryScopeSummary | None
    checks: list[ReadinessCheck]
    executors: list[ExecutorAvailability]
```

Do not expose the full path list in the MCP response. Store the bounded full
inventory only in evidence for persistent workflows.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
pytest tests/unit/test_preflight.py tests/unit/test_run_builder.py tests/unit/test_doctor.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/repogent/preflight.py src/repogent/run_builder.py src/repogent/doctor.py tests/unit/test_preflight.py tests/unit/test_run_builder.py tests/unit/test_doctor.py
git commit -m "feat: bind readiness and runs to one repository scope"
```

---

### Task 3: Add typed workflow identity and independent outcomes

**Files:**
- Modify: `src/repogent/domain.py`
- Modify: `src/repogent/mcp_models.py`
- Modify: `src/repogent/run_builder.py`
- Modify: `src/repogent/run_sessions.py`
- Modify: `src/repogent/reporting.py`
- Test: `tests/unit/test_domain.py`
- Test: `tests/unit/test_run_builder.py`
- Test: `tests/unit/test_run_sessions.py`
- Test: `tests/unit/test_reporting.py`

**Interfaces:**
- Produces: `WorkflowKind`, `WorkflowOutcome`
- Extends: `RunManifest`, `RunSnapshot`, and `RunReport`
- Preserves: `TrustLabel` derivation solely from executor/isolation/verification

- [ ] **Step 1: Write failing model and report tests**

Specify:

```python
class WorkflowKind(StrEnum):
    VERIFIED_CHANGE = "verified_change"
    PATCH_REVIEW = "patch_review"
    CI_TRIAGE = "ci_triage"
    DEPENDENCY_UPDATE = "dependency_update"
    SECURITY_FIX = "security_fix"
    RELEASE_GATE = "release_gate"


class WorkflowOutcome(StrEnum):
    PATCH_READY = "patch_ready"
    APPLIED = "applied"
    HUMAN_INTERVENTION_REQUIRED = "human_intervention_required"
```

Assert a newly built existing workflow has
`kind=VERIFIED_CHANGE`, its terminal applied manifest has `outcome=APPLIED`,
and a report renders `Outcome:` separately from `Verification:`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
pytest tests/unit/test_domain.py tests/unit/test_run_sessions.py tests/unit/test_reporting.py -q
```

Expected: imports or assertions fail because the enums and fields are absent.

- [ ] **Step 3: Implement typed identity and outcome**

Add `kind` and `outcome` to `RunManifest` with explicit construction rather than
implicit inference from `RunStatus`. Add the same fields to `RunSnapshot` and
`RunReport`. A running workflow has `outcome=None`; terminalization sets exactly
one capability-valid outcome.

Keep `compute_trust_label()` unchanged. Add a validator ensuring the current
verified-change workflow can emit only its three allowed outcomes.

- [ ] **Step 4: Update terminal paths**

Map current workflow exits:

- approved, applied, and final validation passed → `APPLIED`;
- validated and awaiting exact patch approval → no terminal outcome;
- limit/provider/validation/recovery interruption → `HUMAN_INTERVENTION_REQUIRED`;
- a future non-applied successful terminal path → `PATCH_READY`.

Ensure cancellations retain `outcome=None` rather than claiming success or
failure.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
pytest tests/unit/test_domain.py tests/unit/test_run_builder.py tests/unit/test_run_sessions.py tests/unit/test_reporting.py -q
```

Expected: all tests pass and trust-label tests remain unchanged.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/repogent/domain.py src/repogent/mcp_models.py src/repogent/run_builder.py src/repogent/run_sessions.py src/repogent/reporting.py tests/unit/test_domain.py tests/unit/test_run_builder.py tests/unit/test_run_sessions.py tests/unit/test_reporting.py
git commit -m "feat: type workflow identity and outcomes"
```

---

### Task 4: Replace the generic MCP start surface with verified change

**Files:**
- Modify: `src/repogent/mcp_models.py`
- Modify: `src/repogent/mcp_server.py`
- Modify: `src/repogent/run_sessions.py`
- Modify: `tests/unit/test_mcp_server.py`
- Modify: `tests/unit/test_run_sessions.py`
- Modify: `tests/integration/test_plugin_end_to_end.py`

**Interfaces:**
- Rename: `RunStart` → `VerifiedChangeStart`
- Rename: MCP `start_run` → `start_verified_change`
- Preserve: run ID, digest approvals, executor selection, cancellation, report access

- [ ] **Step 1: Write failing MCP contract tests**

Assert the registered tool names are exactly:

```python
{
    "inspect_repository_readiness",
    "start_verified_change",
    "get_run",
    "approve_requirements",
    "approve_plan",
    "select_executor",
    "approve_patch",
    "cancel_run",
    "get_report",
}
```

Assert `start_run` and `repogent_doctor` are absent. Verify
`start_verified_change` accepts only `VerifiedChangeStart` and creates a
`VERIFIED_CHANGE` snapshot.

- [ ] **Step 2: Run MCP tests and verify RED**

Run:

```bash
pytest tests/unit/test_mcp_server.py tests/unit/test_run_sessions.py -q
```

Expected: tool-name and model assertions fail.

- [ ] **Step 3: Rename the typed start model and manager method**

Implement:

```python
class VerifiedChangeStart(VersionedModel):
    repository: Path
    request: str = Field(min_length=1, max_length=10_000)
    provider: Literal["openai", "codex-cli", "scripted"] = "codex-cli"
    model: str | None = Field(default=None, max_length=256)
    script: Path | None = None
    executor: Literal["docker", "local", "deferred"] = "deferred"
    output_dir: Path | None = None
```

Rename `SessionManager.start` to `start_verified_change`; keep internal
`RunSession.start` because it starts an already typed session. Remove public
imports and tests for `RunStart`.

- [ ] **Step 4: Rename MCP tools and annotations**

Register `inspect_repository_readiness` as read-only/idempotent and
`start_verified_change` as non-read-only/non-idempotent. Remove both legacy tool
names rather than retaining aliases.

- [ ] **Step 5: Migrate stdio integration tests**

Update every plugin test call and schema assertion. Preserve tests proving:

- three fresh digest-bound content approvals;
- ambiguous approval rejection;
- deferred preview without Docker;
- stale executor digest rejection;
- one exact patch application; and
- trust-label downgrade after final-validation failure.

- [ ] **Step 6: Run MCP and plugin integration tests**

Run:

```bash
pytest tests/unit/test_mcp_server.py tests/unit/test_run_sessions.py tests/integration/test_plugin_end_to_end.py -q
```

Expected: all tests pass with no legacy tool names in registered schemas.

- [ ] **Step 7: Commit Task 4**

```bash
git add src/repogent/mcp_models.py src/repogent/mcp_server.py src/repogent/run_sessions.py tests/unit/test_mcp_server.py tests/unit/test_run_sessions.py tests/integration/test_plugin_end_to_end.py
git commit -m "feat: expose typed verified-change MCP workflow"
```

---

### Task 5: Ship the standalone repository-readiness skill

**Files:**
- Delete: `plugins/repogent/skills/repogent/SKILL.md`
- Delete: `plugins/repogent/skills/repogent/agents/openai.yaml`
- Create: `plugins/repogent/skills/verified-change/SKILL.md`
- Create: `plugins/repogent/skills/verified-change/agents/openai.yaml`
- Create: `plugins/repogent/skills/repository-readiness/SKILL.md`
- Create: `plugins/repogent/skills/repository-readiness/agents/openai.yaml`
- Modify: `tests/plugin/evals.json`
- Modify: `tests/unit/test_plugin_package.py`
- Modify: `tests/unit/test_package_data.py`

**Interfaces:**
- Produces: two discoverable plugin skills with non-overlapping triggers
- Consumes: `inspect_repository_readiness` and `start_verified_change`
- Guarantees: readiness never calls a start, approval, executor, or patch tool

- [ ] **Step 1: Record baseline skill pressure failures**

Before writing the new skills, run isolated agent scenarios against the current
generic skill and record in the test fixture that it:

- treats “is this repository ready?” as a change run;
- cannot distinguish readiness remediation from terminal run evidence; and
- exposes one generic trigger for both diagnostic and mutating intent.

Add those prompts to `tests/plugin/evals.json` with the expected new skill/tool
selection so the current bundle fails.

- [ ] **Step 2: Run plugin package tests and verify RED**

Run:

```bash
pytest tests/unit/test_plugin_package.py tests/unit/test_package_data.py -q
```

Expected: failures because the two skill directories and metadata are absent.

- [ ] **Step 3: Write `repository-readiness`**

Use frontmatter:

```yaml
---
name: repository-readiness
description: Use when diagnosing whether a Python repository, provider, validation commands, or executor options are ready for a Repogent workflow without changing files.
---
```

The body must require `inspect_repository_readiness`, forbid all mutation and
target-code execution, render scope source and selected-file/byte counts, and
report typed remediation. It must explicitly treat `BLOCKED` as a diagnostic
result, not permission to install software or edit configuration.

- [ ] **Step 4: Move the current workflow to `verified-change`**

Use frontmatter:

```yaml
---
name: verified-change
description: Use when a Python repository change must be independently validated, evidence-backed, and shown as an exact patch before approval and application.
---
```

Retain the current safety and approval content, replace tool names with the typed
API, and require readiness through `inspect_repository_readiness`. Remove generic
`@Repogent/$repogent` trigger language that would swallow review, triage, or
release requests.

- [ ] **Step 5: Add metadata and package assertions**

Each `openai.yaml` uses the matching `$skill-name` in its default prompt. Assert
the package contains exactly these two skill directories at this stage and that
their frontmatter names match their paths.

- [ ] **Step 6: Re-run pressure scenarios with the skills present**

Verify:

- readiness prompts select only `repository-readiness`;
- bounded change prompts select only `verified-change`;
- ambiguous generic Repogent prompts ask which capability is intended; and
- readiness never calls an executor-selection or patch tool.

Record the passing outcomes in the eval fixture or its documented runner; do not
replace behavioral scenarios with string-only assertions.

- [ ] **Step 7: Run plugin tests**

Run:

```bash
pytest tests/unit/test_plugin_package.py tests/unit/test_package_data.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit Task 5**

```bash
git add plugins/repogent/skills tests/plugin/evals.json tests/unit/test_plugin_package.py tests/unit/test_package_data.py
git commit -m "feat: split readiness and verified-change skills"
```

---

### Task 6: Document and validate the foundation release

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/security.md`
- Modify: `plugins/repogent/.codex-plugin/plugin.json`
- Modify: `pyproject.toml`
- Test: `tests/unit/test_plugin_package.py`
- Test: `tests/integration/test_plugin_end_to_end.py`

**Interfaces:**
- Documents: Git-selected scope, the two capabilities, typed MCP names, and no legacy aliases
- Locks: plugin/runtime version equality

- [ ] **Step 1: Write failing version and documentation assertions**

Extend package tests to assert:

```python
assert plugin_manifest["version"] == importlib.metadata.version("repogent")
assert "start_run" not in documented_mcp_tools
assert "repogent_doctor" not in documented_mcp_tools
```

Add a test that packaged skills are exactly those declared in documentation.

- [ ] **Step 2: Run package tests and verify RED**

Run:

```bash
pytest tests/unit/test_plugin_package.py tests/unit/test_package_data.py -q
```

Expected: documentation/version assertions fail.

- [ ] **Step 3: Update product and security documentation**

Explain that repository scope follows tracked plus non-ignored untracked Git
paths and that limits apply after selection. Document the non-Git fallback,
sensitive-path override, two skill triggers, exact MCP tool names, and the
absence of compatibility aliases.

- [ ] **Step 4: Bump plugin and runtime together**

Set both versions to `0.3.0`. Because plugin contents change, never update only
`pyproject.toml` or only `plugin.json`.

- [ ] **Step 5: Run the complete quality gate**

Run:

```bash
pytest -q
ruff check .
ruff format --check .
mypy src
bandit -q -r src
python -m build
pytest tests/unit/test_plugin_package.py tests/integration/test_plugin_end_to_end.py -q --no-cov
python -c "import glob, zipfile; wheels = glob.glob('dist/*.whl'); assert len(wheels) == 1; names = zipfile.ZipFile(wheels[0]).namelist(); assert 'repogent/mcp_server.py' in names; assert 'repogent/py.typed' in names; assert any(name.endswith('.dist-info/licenses/LICENSE') for name in names); assert not any('.superpowers' in name for name in names)"
```

Expected: every required check passes; skipped checks retain their documented
reasons.

- [ ] **Step 6: Inspect the packaged plugin and wheel**

Run:

```bash
python -c "import json, pathlib, tomllib; root = pathlib.Path('.'); manifest = json.loads((root / 'plugins/repogent/.codex-plugin/plugin.json').read_text()); project = tomllib.loads((root / 'pyproject.toml').read_text())['project']; skills = sorted(path.name for path in (root / 'plugins/repogent/skills').iterdir() if path.is_dir()); assert manifest['version'] == project['version'] == '0.3.0'; assert skills == ['repository-readiness', 'verified-change']"
```

Expected: exit code 0. The preceding wheel command proves the wheel contains
`repogent/py.typed`, the typed MCP server, and no private workflow state.

- [ ] **Step 7: Commit Task 6**

```bash
git add README.md docs/architecture.md docs/security.md plugins/repogent/.codex-plugin/plugin.json pyproject.toml tests/unit/test_plugin_package.py tests/integration/test_plugin_end_to_end.py
git commit -m "docs: release typed capability foundation"
```

---

## Plan Self-Review

- Spec coverage: this plan covers delivery stages 1 and 2 and intentionally
  leaves patch review, CI triage, release gate, dependency update, and security
  fix for separate plans after the shared interfaces exist.
- Placeholder scan: no implementation step defers a decision, relies on implicit
  error handling, or references an undefined neighboring task.
- Type consistency: `RepositoryScope`, `WorkflowKind`, `WorkflowOutcome`,
  `VerifiedChangeStart`, and `RepositoryReadinessReport` are introduced before
  their consumers.
- Release continuity: the generic start tool and skill are removed only in the
  tasks that add and test their typed replacements.
