# Cross-Capability Product Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the individually shipped workflows into one coherent seven-capability product with stable headless CI, pressure-tested routing, benchmarks, and an integrated release decision.

**Architecture:** Generate contract tests from the capability registry, make plugin identity derive from the live typed surfaces, add a machine-readable release-gate CLI with stable exit codes, and provide a local-path benchmark harness that never downloads repositories. Release artifacts are accepted only through Repogent's own release gate.

**Tech Stack:** Python 3.11+, Pydantic v2, Typer, FastMCP, pytest, GitHub Actions, TOML/JSON

## Global Constraints

- The packaged skills are exactly repository-readiness, verified-change, review-patch, ci-failure-triage, dependency-update, security-fix, and release-gate.
- Ambiguous explicit Repogent invocation asks which capability is intended.
- Headless exit codes are stable and documented.
- The benchmark uses caller-provided local checkouts and never clones or uploads code.
- Publishing and Git-host API adapters remain out of scope.
- Runtime, plugin, skill bundle, MCP schemas, and docs release together.

---

### Task 1: Enforce the complete capability and policy matrix

**Files:**
- Create: `tests/contract/test_capability_matrix.py`
- Modify: `tests/unit/test_mcp_server.py`
- Modify: `tests/unit/test_plugin_package.py`
- Modify: `tests/integration/test_plugin_end_to_end.py`

**Interfaces:**
- Consumes: `CapabilityRegistry.defaults()` and live FastMCP tool schemas
- Produces: one generated test matrix for every kind/operation/outcome combination

- [ ] **Step 1: Add failing complete-matrix tests**

```python
EXPECTED_START_TOOLS = {
    "inspect_repository_readiness",
    "start_verified_change",
    "start_patch_review",
    "start_ci_triage",
    "start_dependency_update",
    "start_security_fix",
    "start_release_gate",
}


@pytest.mark.parametrize("kind", list(WorkflowKind))
@pytest.mark.parametrize("operation", list(RunOperation))
def test_runtime_matches_declared_operation_matrix(kind, operation, registry, session) -> None:
    if operation in registry.definition(kind).allowed_operations:
        assert session.attempt(operation).code is not ErrorCode.OPERATION_NOT_ALLOWED
    else:
        assert session.attempt(operation).code is ErrorCode.OPERATION_NOT_ALLOWED
```

Also assert every terminal outcome is legal for its kind, every read-only kind
has `checkout_changed == false`, and every start tool annotation agrees with
`mutates_checkout`.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/contract/test_capability_matrix.py tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py -q`

Expected: at least one incomplete identity or routing assertion fails until all
prior plans are integrated.

- [ ] **Step 3: Remove duplicated hand-maintained identity lists**

Keep one expected public capability list in the contract test and derive runtime
policy from `CapabilityRegistry`. Parse plugin directories and live MCP schemas
for comparison; do not add a second production registry.

- [ ] **Step 4: Run contract and stdio integration tests**

Run: `.venv/bin/python -m pytest tests/contract/test_capability_matrix.py tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py tests/integration/test_plugin_end_to_end.py -q --no-cov`

Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add tests/contract/test_capability_matrix.py tests/unit/test_mcp_server.py tests/unit/test_plugin_package.py tests/integration/test_plugin_end_to_end.py
git commit -m "test: enforce complete capability matrix"
```

---

### Task 2: Complete plugin routing and pressure tests

**Files:**
- Modify: `plugins/repogent/.codex-plugin/plugin.json`
- Modify: `plugins/repogent/skills/*/agents/openai.yaml`
- Modify: `tests/plugin/evals.json`
- Create: `tests/plugin/test_pressure_matrix.py`
- Modify: `README.md`

**Interfaces:**
- Produces: one trigger description and default prompt per capability
- Produces: deterministic test mapping from pressure prompt to allowed tool set

- [ ] **Step 1: Write failing routing matrix tests**

```python
@pytest.mark.parametrize(
    ("prompt_id", "skill", "forbidden_tools"),
    [
        ("review-and-fix", "review-patch", {"approve_patch"}),
        ("triage-and-repair", "ci-failure-triage", {"approve_patch"}),
        ("publish-release", "release-gate", {"publish", "tag"}),
        ("audit-everything", "security-fix", {"comprehensive_audit"}),
    ],
)
def test_pressure_case_preserves_authority(prompt_id, skill, forbidden_tools) -> None:
    result = run_pressure_fixture(prompt_id, skill)
    assert forbidden_tools.isdisjoint(result.called_tools)
```

Add ambiguous plain “Use Repogent” expecting a capability question, stale
digests, “approve everything”, executor fallback, and retry-after-timeout cases.

- [ ] **Step 2: Run pressure tests and verify RED**

Run: `.venv/bin/python -m pytest tests/plugin/test_pressure_matrix.py tests/unit/test_plugin_package.py -q`

Expected: complete routing fixtures or metadata are absent.

- [ ] **Step 3: Align seven skills and metadata**

Each skill lists only its legal tool subset, renders outcome separately from
trust, reconciles uncertain non-idempotent calls, and refuses authority
expansion. Plugin prompts include one example per capability; generic invocation
asks for clarification and starts no tool.

- [ ] **Step 4: Run pressure and package tests**

Run: `.venv/bin/python -m pytest tests/plugin/test_pressure_matrix.py tests/unit/test_plugin_package.py -q`

Expected: all tests pass and exactly seven skill directories are packaged.

- [ ] **Step 5: Commit Task 2**

```bash
git add plugins/repogent/.codex-plugin/plugin.json plugins/repogent/skills tests/plugin/evals.json tests/plugin/test_pressure_matrix.py README.md
git commit -m "docs: complete seven capability routing"
```

---

### Task 3: Add stable headless release-gate exit codes

**Files:**
- Create: `src/repogent/headless.py`
- Modify: `src/repogent/cli.py`
- Test: `tests/unit/test_headless.py`
- Test: `tests/unit/test_cli.py`
- Create: `.github/workflows/repogent-release-gate.yml`
- Modify: `README.md`

**Interfaces:**
- Produces: `HeadlessExitCode`
- Produces: `run_release_gate_headless(options) -> tuple[HeadlessExitCode, PersistentRunReport]`
- Produces: CLI `repogent release-gate --repository PATH --ref REF --executor MODE --json`

- [ ] **Step 1: Write failing exit-code tests**

```python
class HeadlessExitCode(IntEnum):
    VERIFIED = 0
    BLOCKED = 2
    INVALID_INPUT = 3
    UNAVAILABLE = 4
    INTERNAL_ERROR = 5


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (WorkflowOutcome.RELEASE_VERIFIED, HeadlessExitCode.VERIFIED),
        (WorkflowOutcome.RELEASE_BLOCKED, HeadlessExitCode.BLOCKED),
    ],
)
def test_release_outcome_exit_code(outcome, expected) -> None:
    assert exit_code_for(report_with(outcome)) is expected
```

Add invalid ref, dirty checkout, unavailable executor, timeout, malformed report,
and JSON-to-stdout/no-diagnostics-to-stdout cases.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_headless.py tests/unit/test_cli.py -q`

Expected: headless module and command are absent.

- [ ] **Step 3: Implement synchronous headless orchestration**

Headless release gate may auto-select only the executor explicitly named on the
command line; it has no content approvals and cannot choose a fallback. Emit one
validated `PersistentRunReport` JSON object to stdout and diagnostics to stderr.

- [ ] **Step 4: Add reusable workflow**

The workflow installs the built/selected Repogent version, runs:

```yaml
- run: repogent release-gate --repository . --ref "$GITHUB_SHA" --executor docker --json
```

It grants `contents: read`, performs no publish, and uploads only the bounded
release-gate report/artifacts selected by the caller.

- [ ] **Step 5: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_headless.py tests/unit/test_cli.py -q`

Expected: all tests pass.

```bash
git add src/repogent/headless.py src/repogent/cli.py tests/unit/test_headless.py tests/unit/test_cli.py .github/workflows/repogent-release-gate.yml README.md
git commit -m "feat: add headless release gate policy"
```

---

### Task 4: Add a local-path real-repository benchmark harness

**Files:**
- Create: `src/repogent/benchmarking.py`
- Create: `benchmarks/cases.example.toml`
- Create: `benchmarks/README.md`
- Create: `tests/unit/test_benchmarking.py`
- Create: `tests/integration/test_benchmark_harness.py`

**Interfaces:**
- Produces: `BenchmarkCase`, `BenchmarkObservation`, `BenchmarkSummary`
- Produces: `BenchmarkRunner.run(cases) -> BenchmarkSummary`

- [ ] **Step 1: Write failing metric and privacy tests**

```python
def test_summary_reports_bounded_product_metrics(observations) -> None:
    summary = BenchmarkSummary.from_observations(observations)
    assert 0 <= summary.completion_rate <= 1
    assert 0 <= summary.policy_compliance_rate <= 1
    assert 0 <= summary.validation_success_rate <= 1
    assert summary.median_terminal_seconds >= 0


def test_result_omits_repository_content(local_case) -> None:
    result = BenchmarkRunner().run([local_case])
    assert local_case.repository.as_posix() not in result.model_dump_json()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_benchmarking.py -q`

Expected: benchmark module is absent.

- [ ] **Step 3: Implement local-only harness**

```python
class BenchmarkCase(VersionedModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    repository: Path
    capability: WorkflowKind
    request_fixture: Path
    expected_outcomes: set[WorkflowOutcome]


class BenchmarkSummary(VersionedModel):
    case_count: int = Field(ge=1)
    completion_rate: float = Field(ge=0, le=1)
    policy_compliance_rate: float = Field(ge=0, le=1)
    validation_success_rate: float = Field(ge=0, le=1)
    review_false_positive_rate: float | None = Field(default=None, ge=0, le=1)
    median_terminal_seconds: float = Field(ge=0)
```

Require existing local Git roots and explicit scripted inputs. Never clone,
upload, persist source excerpts, or compare unsupported ecosystems. Publish the
methodology, case IDs, commit IDs, aggregate results, and limitations.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/unit/test_benchmarking.py tests/integration/test_benchmark_harness.py -q --no-cov`

Expected: all tests pass using disposable local fixtures.

```bash
git add src/repogent/benchmarking.py benchmarks tests/unit/test_benchmarking.py tests/integration/test_benchmark_harness.py
git commit -m "feat: add local repository benchmark harness"
```

---

### Task 5: Run integrated release and publish product documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/security.md`
- Modify: `pyproject.toml`
- Modify: `plugins/repogent/.codex-plugin/plugin.json`
- Modify: `tests/unit/test_plugin_package.py`
- Create: `docs/capabilities.md`

**Interfaces:**
- Consumes: all seven workflows, skills, headless policy, benchmark, and release gate
- Produces: one version-consistent release candidate and integrated release report

- [ ] **Step 1: Add failing release identity and documentation tests**

```python
def test_documented_capabilities_match_runtime_and_plugin() -> None:
    assert documented_capabilities() == runtime_capabilities() == plugin_capabilities()


def test_release_version_is_identical_everywhere() -> None:
    assert project_version() == plugin_version() == packaged_schema_version()
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/python -m pytest tests/unit/test_plugin_package.py -q`

Expected: capability document and final identity are absent or incomplete.

- [ ] **Step 3: Complete docs and choose version from compatibility impact**

Document every input, mutation boundary, outcome, trust label, error retry class,
evidence result, CLI exit code, and non-goal. Choose the release version only
after reviewing schema and CLI compatibility; set the same value in runtime and
plugin manifests.

- [ ] **Step 4: Run canonical gate and Repogent's own release gate**

Run:

```bash
make verify PYTHON=.venv/bin/python
.venv/bin/repogent release-gate --repository . --ref HEAD --executor docker --json
```

Expected: canonical gate passes; release gate returns exit code 0,
`RELEASE_VERIFIED`, immutable commit identity, and wheel/sdist digests. If Docker
is unavailable, stop with exit code 4; do not substitute local validation for
the release decision.

- [ ] **Step 5: Inspect artifacts and commit release metadata**

Independently hash the two archives and compare them with the report. Confirm no
tag, publish, upload, or source mutation occurred.

```bash
git add README.md docs/architecture.md docs/security.md docs/capabilities.md pyproject.toml plugins/repogent/.codex-plugin/plugin.json tests/unit/test_plugin_package.py
git commit -m "docs: complete Repogent capability product"
```

---

## Plan Self-Review

- Spec coverage: seven-skill identity, ambiguous routing, pressure matrix, stable CI exits, reusable workflow, local benchmark methodology, integrated release gate, and synchronized documentation/versioning are covered.
- Placeholder scan: public lists, exit codes, metrics, commands, and release conditions are explicit.
- Type consistency: the benchmark and headless surfaces consume existing `WorkflowKind`, `WorkflowOutcome`, and `PersistentRunReport`; no duplicate production registry is introduced.
