# Repogent v0.2 Local Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first Phase 2 increment: explainable Python localization, event-backed evidence, safe preflight, and adaptive one-to-three patch candidates evaluated from an identical baseline.

**Architecture:** Keep the synchronous approval-gated workflow, provider boundary, Docker-first executor, artifact store, and transactional patch policy. Add focused modules for Python symbols, localization, events, preflight, and candidate evaluation; the workflow coordinates their typed results but does not absorb their algorithms.

**Tech Stack:** Python 3.11+, Pydantic 2, Typer, pytest, Ruff, mypy, Bandit, stdlib `ast`, existing unified-diff and execution infrastructure.

## Global Constraints

- Python remains `>=3.11`; do not add runtime dependencies for AST analysis, event storage, scoring, or candidate selection.
- Docker remains the default validator; restricted local execution is an explicit weaker fallback and is never selected silently.
- Models never execute commands, select arbitrary validation commands, apply patches, or override required failures.
- Generate one candidate by default and at most two alternatives only when objective policy triggers.
- Evaluate every candidate from the same repository baseline and retain rejected-candidate evidence.
- Evidence stays outside the target repository, is sanitized, bounded, versioned, and written atomically.
- Preserve requirements, plan, patch, and repair approval guarantees; no repository mutation occurs before patch approval except temporary evaluation that is verified restored.
- Required validation failures are hard blockers; unavailable optional checks are recorded as skipped, never passed.
- Use TDD for every task and keep the full existing quality gate: pytest with at least 85% coverage, Ruff, mypy, and Bandit.

---

## File Structure

### New production files

- `src/repogent/symbols.py`: typed Python symbol graph and deterministic AST builder.
- `src/repogent/localization.py`: named localization signals, bounded symbol-aware context, ambiguity calculation.
- `src/repogent/events.py`: versioned run events, append-only JSONL store, event sink protocol.
- `src/repogent/preflight.py`: repository, Git, executor, and validation-tool readiness checks.
- `src/repogent/candidates.py`: candidate generation policy, isolated evaluation, deduplication, eligibility, and selection.

### Modified production files

- `src/repogent/domain.py`: candidate, evidence, selection, preflight, and event schemas plus manifest fields.
- `src/repogent/repository.py`: keep secure inventory; remove responsibility for Phase 2 ranking after compatibility migration.
- `src/repogent/patching.py`: expose a reversible patch transaction used for isolated candidate evaluation.
- `src/repogent/execution.py`: expose executor readiness without running repository checks.
- `src/repogent/validation.py`: support focused command subsets and return evidence without changing policy semantics.
- `src/repogent/workflow.py`: consume localizer, candidate engine, event sink, and preflight results.
- `src/repogent/artifacts.py`: create the local JSONL event store and link it from the manifest.
- `src/repogent/cli.py`: run preflight before provider work and render concise events.
- `src/repogent/reporting.py`: report localization, alternatives, evidence, and selection reason.

### New test files

- `tests/unit/test_symbols.py`
- `tests/unit/test_localization.py`
- `tests/unit/test_events.py`
- `tests/unit/test_preflight.py`
- `tests/unit/test_candidates.py`
- `tests/integration/test_phase2_local_reliability.py`

### Modified test files

- `tests/unit/test_domain.py`
- `tests/unit/test_patching.py`
- `tests/unit/test_execution.py`
- `tests/unit/test_validation.py`
- `tests/unit/test_workflow.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_reporting.py`

---

### Task 1: Versioned Phase 2 Domain and Run Events

**Files:**
- Modify: `src/repogent/domain.py`
- Create: `src/repogent/events.py`
- Modify: `src/repogent/artifacts.py`
- Test: `tests/unit/test_domain.py`
- Create: `tests/unit/test_events.py`
- Modify: `tests/unit/test_artifacts.py`

**Interfaces:**
- Consumes: existing `VersionedModel`, `CheckResult`, `ValidationReport`, `RunManifest`, `ArtifactStore._atomic_write()`.
- Produces: `RunEvent`, `EventKind`, `EventSink.emit(event: RunEvent) -> None`, `JsonlEventStore`, enriched `PatchProposal`, `CandidateRecord`, `CandidateEvidence`, `CandidateSelection`, and new manifest fields `repository_fingerprint`, `configuration_fingerprint`, `candidate_ids`, `selected_candidate_id`, `events_file`.

- [ ] **Step 1: Write failing schema and append-only event tests**

```python
# tests/unit/test_events.py
import json
from pathlib import Path

import pytest

from repogent.domain import EventKind, RunEvent
from repogent.events import JsonlEventStore


def test_jsonl_event_store_appends_versioned_sanitized_events(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "events.jsonl", secrets=["sk-secret"])
    store.emit(
        RunEvent(
            run_id="run-1",
            sequence=1,
            kind=EventKind.WARNING,
            stage="preflight",
            message="credential sk-secret was not forwarded",
        )
    )
    payload = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
    assert payload["schema_version"] == "1"
    assert payload["sequence"] == 1
    assert "sk-secret" not in payload["message"]


def test_jsonl_event_store_rejects_non_monotonic_sequence(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "events.jsonl")
    event = RunEvent(run_id="run-1", sequence=1, kind=EventKind.STAGE, message="start")
    store.emit(event)
    with pytest.raises(ValueError, match="sequence"):
        store.emit(event)
```

Add domain tests asserting a `CandidateEvidence` with a failed required check is ineligible and that new `RunManifest` fields round-trip through JSON.

Also add `required: bool = True` to `CheckResult` and update `ValidationReport.passed` to require at least one check and require every `check.required` item to have `CheckStatus.PASSED`. Optional failed or skipped checks remain visible evidence but do not create a deterministic blocker. This default preserves existing callers while `ValidationPipeline` begins recording the originating `CommandSpec.required` value in Task 5.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/unit/test_domain.py tests/unit/test_events.py tests/unit/test_artifacts.py -q`

Expected: FAIL because `EventKind`, `RunEvent`, `CandidateEvidence`, and `JsonlEventStore` do not exist.

- [ ] **Step 3: Add the typed models**

Add these public shapes to `domain.py`; keep the existing `schema_version: Literal["1"]` behavior:

```python
class EventKind(StrEnum):
    STAGE = "stage"
    MODEL = "model"
    CANDIDATE = "candidate"
    VALIDATION = "validation"
    APPROVAL = "approval"
    WARNING = "warning"
    TERMINAL = "terminal"


class RunEvent(VersionedModel):
    run_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    kind: EventKind
    stage: str | None = None
    message: str = Field(min_length=1)
    data: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class CandidateRecord(VersionedModel):
    candidate_id: str = Field(pattern=r"^candidate-[1-3]$")
    proposal: PatchProposal
    parent_candidate_id: str | None = None
    generation_reason: str = Field(min_length=1)
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    usage: ProviderUsage


class CandidateEvidence(VersionedModel):
    candidate_id: str
    validation: ValidationReport
    acceptance_criteria_coverage: float = Field(ge=0, le=1)
    risk_level: RiskLevel
    changed_files: int = Field(ge=0)
    changed_lines: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    required_failures: list[str] = Field(default_factory=list)
    skipped_checks: list[str] = Field(default_factory=list)
    restored_to_baseline: bool

    @computed_field  # type: ignore[prop-decorator]
    @property
    def eligible(self) -> bool:
        return (
            not self.required_failures
            and self.restored_to_baseline
            and self.validation.passed
        )


class CandidateSelection(VersionedModel):
    selected_candidate_id: str | None
    eligible_candidate_ids: list[str]
    ambiguous: bool = False
    reason: str = Field(min_length=1)
```

Extend `PatchProposal` with backward-compatible typed intent fields used by candidate comparison:

```python
acceptance_criteria_addressed: list[str] = Field(default_factory=list)
assumptions: list[str] = Field(default_factory=list)
risks: list[str] = Field(default_factory=list)
focused_tests: list[str] = Field(default_factory=list)
```

`acceptance_criteria_addressed` must contain exact strings from `RequirementsSpec.acceptance_criteria`; workflow validation rejects unknown values before candidate evaluation.

Extend `RunManifest` with defaulted fields so old evidence remains readable:

```python
repository_fingerprint: str | None = None
configuration_fingerprint: str | None = None
candidate_ids: list[str] = Field(default_factory=list)
selected_candidate_id: str | None = None
events_file: str | None = None
```

- [ ] **Step 4: Implement the append-only event store**

```python
# src/repogent/events.py
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from repogent.domain import RunEvent
from repogent.sanitization import sanitize_data


class EventSink(Protocol):
    def emit(self, event: RunEvent) -> None:
        raise NotImplementedError


class JsonlEventStore:
    def __init__(self, path: Path, secrets: list[str] | None = None) -> None:
        self.path = path
        self.secrets = secrets or []
        self._last_sequence = 0

    def emit(self, event: RunEvent) -> None:
        if event.sequence <= self._last_sequence:
            raise ValueError("event sequence must increase monotonically")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = sanitize_data(event.model_dump(mode="json"), self.secrets)
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8", closefd=False) as handle:
                handle.write(json.dumps(payload) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        self._last_sequence = event.sequence
```

Add `ArtifactStore.event_store()` returning `JsonlEventStore(self._path_in_root("events.jsonl"), self.secrets)`.

- [ ] **Step 5: Run tests, then quality checks**

Run: `pytest tests/unit/test_domain.py tests/unit/test_events.py tests/unit/test_artifacts.py -q`

Expected: PASS.

Run: `ruff check src/repogent/domain.py src/repogent/events.py src/repogent/artifacts.py tests/unit/test_events.py && mypy src/repogent/domain.py src/repogent/events.py src/repogent/artifacts.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/repogent/domain.py src/repogent/events.py src/repogent/artifacts.py tests/unit/test_domain.py tests/unit/test_events.py tests/unit/test_artifacts.py
git commit -m "feat: add versioned run events and candidate evidence"
```

---

### Task 2: Python Symbol Graph

**Files:**
- Create: `src/repogent/symbols.py`
- Create: `tests/unit/test_symbols.py`

**Interfaces:**
- Consumes: `RepositoryInventory` and `FileRecord` from `repogent.repository`.
- Produces: `SymbolKind`, `SymbolNode`, `SymbolEdge`, `PythonSymbolGraph`, and `PythonSymbolGraphBuilder.build(inventory: RepositoryInventory) -> PythonSymbolGraph`.

- [ ] **Step 1: Write failing graph tests**

```python
from pathlib import Path

from repogent.repository import RepositoryInspector
from repogent.symbols import PythonSymbolGraphBuilder, SymbolKind


def test_builder_records_qualified_symbols_imports_and_calls(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        "from helpers import normalize\n"
        "class UserService:\n"
        "    def save(self, name: str) -> str:\n"
        "        return normalize(name)\n"
    )
    graph = PythonSymbolGraphBuilder().build(RepositoryInspector().inspect(tmp_path))
    symbols = {node.qualified_name: node for node in graph.nodes}
    assert symbols["service.UserService"].kind is SymbolKind.CLASS
    assert symbols["service.UserService.save"].start_line == 3
    assert any(edge.kind == "imports" and edge.target == "helpers.normalize" for edge in graph.edges)
    assert any(edge.kind == "calls" and edge.target == "normalize" for edge in graph.edges)


def test_builder_reports_one_parse_error_without_losing_valid_files(tmp_path: Path) -> None:
    (tmp_path / "good.py").write_text("def valid():\n    return 1\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")
    graph = PythonSymbolGraphBuilder().build(RepositoryInspector().inspect(tmp_path))
    assert [node.qualified_name for node in graph.nodes] == ["good.valid"]
    assert graph.parse_errors == {"bad.py": "invalid syntax"}
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/unit/test_symbols.py -q`

Expected: FAIL with `ModuleNotFoundError: repogent.symbols`.

- [ ] **Step 3: Implement typed graph records and builder**

Use Pydantic models so graph artifacts serialize consistently:

```python
class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


class SymbolNode(VersionedModel):
    symbol_id: str
    qualified_name: str
    name: str
    kind: SymbolKind
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    parent_id: str | None = None
    decorators: list[str] = Field(default_factory=list)


class SymbolEdge(VersionedModel):
    source: str
    target: str
    kind: Literal["contains", "imports", "calls"]


class PythonSymbolGraph(VersionedModel):
    nodes: list[SymbolNode]
    edges: list[SymbolEdge]
    parse_errors: dict[str, str] = Field(default_factory=dict)
```

Implement `PythonSymbolGraphBuilder` with an `ast.NodeVisitor` that maintains a qualified-name stack, uses `lineno` and `end_lineno`, records imports with aliases, emits containment edges, and records statically named `ast.Call` targets. Sort nodes by `(path, start_line, qualified_name)` and edges by `(source, kind, target)` for deterministic artifacts. Catch `SyntaxError` per file and normalize its message with `error.msg`.

- [ ] **Step 4: Run focused and existing repository tests**

Run: `pytest tests/unit/test_symbols.py tests/unit/test_repository.py -q`

Expected: PASS with existing inventory behavior unchanged.

Run: `ruff check src/repogent/symbols.py tests/unit/test_symbols.py && mypy src/repogent/symbols.py`

Expected: both commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/repogent/symbols.py tests/unit/test_symbols.py
git commit -m "feat: build deterministic Python symbol graph"
```

---

### Task 3: Explainable Hybrid Localization

**Files:**
- Create: `src/repogent/localization.py`
- Create: `tests/unit/test_localization.py`
- Modify: `src/repogent/cli.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `RepositoryInventory`, `PythonSymbolGraph`, request text, and optional `ValidationReport`.
- Produces: `LocalizationSignal`, `LocalizedSymbol`, `LocalizationReport`, and `PythonLocalizer.localize(inventory: RepositoryInventory, graph: PythonSymbolGraph, request: str, acceptance_criteria: Sequence[str] = (), failure_evidence: ValidationReport | None = None) -> LocalizationReport`.

- [ ] **Step 1: Write failing ranking, bounding, and ambiguity tests**

```python
def test_localizer_combines_lexical_symbol_import_and_test_signals(tmp_path: Path) -> None:
    inventory, graph = build_fixture(
        tmp_path,
        {
            "auth.py": "from limits import throttle\ndef login():\n    return throttle()\n",
            "tests/test_auth.py": "from auth import login\ndef test_login():\n    assert login()\n",
            "billing.py": "def invoice():\n    return 1\n",
        },
    )
    report = PythonLocalizer(max_snippets=4, max_total_chars=4_000).localize(
        inventory, graph, "fix login throttling"
    )
    assert report.snippets[0].path == "auth.py"
    assert {signal.name for signal in report.locations[0].signals} >= {
        "lexical",
        "symbol",
    }
    assert sum(len(snippet.text) for snippet in report.snippets) <= 4_000


def test_localizer_marks_diffuse_results_ambiguous(tmp_path: Path) -> None:
    inventory, graph = build_fixture(
        tmp_path,
        {"a.py": "def value(): return 1\n", "b.py": "def value(): return 2\n"},
    )
    report = PythonLocalizer().localize(inventory, graph, "change value")
    assert report.ambiguous is True
    assert "top locations are not concentrated" in report.ambiguity_reason
```

Use this complete test helper:

```python
def build_fixture(
    root: Path, files: dict[str, str]
) -> tuple[RepositoryInventory, PythonSymbolGraph]:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    inventory = RepositoryInspector().inspect(root)
    return inventory, PythonSymbolGraphBuilder().build(inventory)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/unit/test_localization.py -q`

Expected: FAIL with `ModuleNotFoundError: repogent.localization`.

- [ ] **Step 3: Implement named signals and bounded context**

```python
class LocalizationSignal(VersionedModel):
    name: Literal["lexical", "symbol", "import", "call", "test", "failure"]
    score: float = Field(gt=0)
    reason: str


class LocalizedSymbol(VersionedModel):
    symbol_id: str
    path: str
    start_line: int
    end_line: int
    score: float = Field(ge=0)
    signals: list[LocalizationSignal]


class LocalizationReport(VersionedModel):
    locations: list[LocalizedSymbol]
    snippets: list[ContextSnippet]
    ambiguous: bool
    ambiguity_reason: str | None = None
```

`PythonLocalizer.localize()` must tokenize request and acceptance text with the existing identifier tokenizer rules, add named weights (`lexical=1.0`, `symbol=1.5`, `import=0.5`, `call=0.5`, `test=0.75`, `failure=2.0`), and normalize each score by the sum of its signal weights. Build snippets from symbol line ranges with at most 20 lines of surrounding context. Enforce both `max_snippets` and `max_total_chars` while preserving rank order.

Set `ambiguous=True` when there are no locations or when the top score is below `0.35` or less than `1.20` times the second score. Store the exact triggering explanation.

- [ ] **Step 4: Update `analyze` to emit graph and localization report**

Replace the CLI's direct `LexicalRetriever` call with:

```python
inventory = RepositoryInspector().inspect(repository)
graph = PythonSymbolGraphBuilder().build(inventory)
localization = PythonLocalizer().localize(inventory, graph, request) if request else None
payload = {
    "inventory": inventory.model_dump(),
    "symbol_graph": graph.model_dump(),
    "localization": localization.model_dump() if localization else None,
}
typer.echo(json.dumps(payload, indent=2))
```

Update the CLI test to assert `payload["localization"]["snippets"][0]["path"] == "auth.py"` and that `symbol_graph.nodes` contains `auth.login`.

- [ ] **Step 5: Run focused tests and static checks**

Run: `pytest tests/unit/test_localization.py tests/unit/test_cli.py::test_analyze_prints_inventory_and_ranked_context -q`

Expected: PASS.

Run: `ruff check src/repogent/localization.py src/repogent/cli.py tests/unit/test_localization.py tests/unit/test_cli.py && mypy src/repogent/localization.py src/repogent/cli.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/repogent/localization.py src/repogent/cli.py tests/unit/test_localization.py tests/unit/test_cli.py
git commit -m "feat: add explainable Python localization"
```

---

### Task 4: Safe Preflight Before Model Spending

**Files:**
- Create: `src/repogent/preflight.py`
- Modify: `src/repogent/execution.py`
- Create: `tests/unit/test_preflight.py`
- Modify: `tests/unit/test_execution.py`
- Modify: `src/repogent/cli.py`
- Modify: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: repository path, executor, and `ValidationPolicy`.
- Produces: `ReadinessStatus`, `PreflightCheck`, `PreflightReport`, `Executor.readiness() -> tuple[bool, str | None]`, `Preflight.run(root: Path) -> PreflightReport`, and `configuration_fingerprint(provider: str, model: str, executor: str, commands: Sequence[CommandSpec]) -> str`.

- [ ] **Step 1: Write failing preflight tests**

```python
def test_preflight_reports_commit_dirty_state_and_executor(tmp_path: Path) -> None:
    repository = initialize_git_repository(tmp_path)
    (repository / "tracked.py").write_text("value = 2\n")
    report = Preflight(FakeExecutor(ready=True), ValidationPolicy()).run(repository)
    assert report.passed is True
    assert report.git_commit is not None
    assert report.dirty is True
    assert report.repository_fingerprint


def test_preflight_blocks_unavailable_docker_before_provider_creation(tmp_path: Path) -> None:
    repository = initialize_git_repository(tmp_path)
    report = Preflight(FakeExecutor(ready=False), ValidationPolicy()).run(repository)
    assert report.passed is False
    assert report.checks[-1].name == "executor"
    assert report.checks[-1].required is True
```

Use these exact test helpers:

```python
class FakeExecutor:
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready

    def readiness(self) -> tuple[bool, str | None]:
        return (self.ready, None if self.ready else "validator image unavailable")

    def available(self, command: CommandSpec) -> bool:
        del command
        return True

    def run(self, command: CommandSpec, root: Path) -> CheckResult:
        raise AssertionError(f"preflight must not run {command.name} in {root}")


def initialize_git_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    (repository / "tracked.py").write_text("value = 1\n")
    commands = [
        ("git", "init", "-q"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test User"),
        ("git", "add", "tracked.py"),
        ("git", "commit", "-qm", "initial"),
    ]
    for command in commands:
        subprocess.run(command, cwd=repository, check=True)  # noqa: S603,S607
    return repository
```

Add a CLI test that monkeypatches `OpenAIProvider` to fail if constructed, configures preflight to fail, and asserts the command exits 2 without constructing the provider.

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/unit/test_preflight.py tests/unit/test_execution.py tests/unit/test_cli.py -q`

Expected: FAIL because the readiness and preflight APIs do not exist.

- [ ] **Step 3: Add readiness without running project commands**

Extend `Executor` with `readiness() -> tuple[bool, str | None]`. `LocalExecutor.readiness()` returns `(True, "restricted local execution provides weaker isolation")`. `DockerExecutor.readiness()` checks the executable and configured image using `_inspect_image()` and returns an actionable reason for missing Docker, timeout, or missing image.

Do not make `available(command)` responsible for global readiness; it continues to answer command-specific availability.

- [ ] **Step 4: Implement preflight and repository fingerprinting**

```python
class ReadinessStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"


class PreflightCheck(VersionedModel):
    name: str
    status: ReadinessStatus
    required: bool
    reason: str | None = None


class PreflightReport(VersionedModel):
    checks: list[PreflightCheck]
    git_commit: str | None
    dirty: bool
    repository_fingerprint: str

    @computed_field
    @property
    def passed(self) -> bool:
        return all(not check.required or check.status is ReadinessStatus.PASSED for check in self.checks)
```

`Preflight.run()` resolves the repository, rejects filesystem root, uses fixed-argv `git rev-parse HEAD` and `git status --porcelain` with 5-second timeouts, calls executor readiness, and hashes the resolved root, commit or `no-commit`, dirty-state output, and sorted validation command names with SHA-256. Git absence is a warning, not a blocker; executor unavailability is required and blocks.

Add `configuration_fingerprint(provider: str, model: str, executor: str, commands: Sequence[CommandSpec]) -> str`. Serialize those values and sorted command fields with canonical `json.dumps(payload, sort_keys=True, separators=(",", ":"))`, then return its SHA-256. This function uses CLI option values and requires no provider construction.

- [ ] **Step 5: Invoke preflight before evidence/provider initialization**

In `run_command`, construct the validation policy and executor, create the external artifact store, run preflight, and persist `preflight` as the first model artifact. Print each failed or warning check and, when `not report.passed`, write a terminal manifest and report before exiting 2. Construct no provider on this path, so failed preflight spends no model budget while still producing durable evidence. On success, copy the repository fingerprint plus `configuration_fingerprint(provider, model, executor, policy.commands(repository))` into `RunManifest`, then construct the provider.

- [ ] **Step 6: Run focused tests and static checks**

Run: `pytest tests/unit/test_preflight.py tests/unit/test_execution.py tests/unit/test_cli.py -q`

Expected: PASS.

Run: `ruff check src/repogent/preflight.py src/repogent/execution.py src/repogent/cli.py tests/unit/test_preflight.py && mypy src/repogent/preflight.py src/repogent/execution.py src/repogent/cli.py`

Expected: both commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/repogent/preflight.py src/repogent/execution.py src/repogent/cli.py tests/unit/test_preflight.py tests/unit/test_execution.py tests/unit/test_cli.py
git commit -m "feat: add executor and repository preflight"
```

---

### Task 5: Reversible Patch Transactions and Candidate Evaluation

**Files:**
- Modify: `src/repogent/patching.py`
- Modify: `src/repogent/validation.py`
- Create: `src/repogent/candidates.py`
- Modify: `tests/unit/test_patching.py`
- Modify: `tests/unit/test_validation.py`
- Create: `tests/unit/test_candidates.py`

**Interfaces:**
- Consumes: `ValidatedPatch`, `PatchApplier`, `Validator`, `CandidateRecord`.
- Produces: `PatchTransaction`, `PatchApplier.transaction(root, patch)`, `CandidateEvaluator.evaluate(root: Path, candidate: CandidateRecord, acceptance_criteria: Sequence[str], timeout_seconds: float) -> CandidateEvidence`.

- [ ] **Step 1: Write failing restoration and equal-baseline tests**

```python
def test_patch_transaction_restores_successful_patch_without_commit(tmp_path: Path) -> None:
    root = repository_with_value(tmp_path, 1)
    validated = PatchPolicy().validate(root, proposal_changing_value(1, 2))
    with PatchApplier().transaction(root, validated):
        assert (root / "app.py").read_text() == "value = 2\n"
    assert (root / "app.py").read_text() == "value = 1\n"


def test_candidate_evaluator_uses_same_baseline_for_each_candidate(tmp_path: Path) -> None:
    root = repository_with_value(tmp_path, 1)
    evaluator = CandidateEvaluator(PatchPolicy(), PatchApplier(), RecordingValidator())
    first = evaluator.evaluate(root, candidate("candidate-1", 1, 2), ["value changes"], 30)
    second = evaluator.evaluate(root, candidate("candidate-2", 1, 3), ["value changes"], 30)
    assert first.restored_to_baseline is True
    assert second.restored_to_baseline is True
    assert (root / "app.py").read_text() == "value = 1\n"
```

Use these complete helpers in `test_candidates.py`:

```python
class RecordingValidator:
    def run(self, root: Path, *, timeout_seconds: float | None = None) -> ValidationReport:
        del timeout_seconds
        assert (root / "app.py").exists()
        return ValidationReport(
            checks=[
                CheckResult(
                    name="pytest",
                    argv=["python", "-m", "pytest", "-q"],
                    status=CheckStatus.PASSED,
                    exit_code=0,
                )
            ]
        )


def repository_with_value(root: Path, value: int) -> Path:
    repository = root / "repository"
    repository.mkdir()
    (repository / "app.py").write_text(f"value = {value}\n")
    return repository


def proposal_changing_value(old: int, new: int) -> PatchProposal:
    return PatchProposal(
        summary="Change value",
        diff=f"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = {old}\n+value = {new}\n",
        acceptance_criteria_addressed=["value changes"],
        focused_tests=["pytest"],
    )


def candidate(candidate_id: str, old: int, new: int) -> CandidateRecord:
    proposal = proposal_changing_value(old, new)
    return CandidateRecord(
        candidate_id=candidate_id,
        proposal=proposal,
        generation_reason="initial candidate" if candidate_id == "candidate-1" else "validation failed",
        diff_sha256=hashlib.sha256(proposal.diff.encode()).hexdigest(),
        usage=ProviderUsage(model="scripted"),
    )
```

Add tests for restoration after validator exception and for a restoration verification mismatch returning ineligible evidence rather than continuing.

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/unit/test_patching.py tests/unit/test_validation.py tests/unit/test_candidates.py -q`

Expected: FAIL because `transaction()` and `CandidateEvaluator` do not exist.

- [ ] **Step 3: Refactor snapshots into a reversible transaction**

Add a public context manager whose implementation reuses the existing fd-relative, `O_NOFOLLOW` snapshot and restore functions:

```python
class PatchTransaction:
    def __init__(self, applier: PatchApplier, root: Path, patch: ValidatedPatch) -> None:
        self.applier = applier
        self.root = root.resolve(strict=True)
        self.patch = patch
        self._snapshots: dict[Path, Snapshot] = {}
        self._missing_directories: set[Path] = set()
        self._committed = False

    def __enter__(self) -> PatchTransaction:
        self._snapshots, self._missing_directories = self.applier.snapshot(self.root, self.patch)
        self.applier.apply(self.root, self.patch)
        return self

    def commit(self) -> None:
        self._committed = True

    def __exit__(self, *_error: object) -> None:
        if not self._committed:
            self.applier.restore(self.root, self._snapshots, self._missing_directories)
```

Expose typed `snapshot()` and `restore()` wrappers. `restore()` joins concrete restoration exception messages and raises `RuntimeError(f"patch restoration failed: {details}")` on any error. Keep `apply()` behavior backward compatible.

- [ ] **Step 4: Implement candidate evaluation**

`CandidateEvaluator.evaluate()` validates the proposal, rejects any `acceptance_criteria_addressed` value not present in the supplied requirements, hashes every touched path before the transaction, runs validation inside it, exits the transaction, hashes the paths again, and sets `restored_to_baseline` only when all hashes and existence states match. Convert patch-policy or acceptance-mapping failure into a `CandidateEvidence` with a synthetic required failure named `patch-policy` or `acceptance-mapping`; do not mutate the repository.

Update `ValidationPipeline` so every executor result is copied with `required=command.required`; synthetic unavailable results also carry that value. Use `time.monotonic()` for duration, copy `changed_files` and `changed_lines` from `ValidatedPatch`, list checks where `check.required and check.status is not CheckStatus.PASSED` as required failures, and list all skipped checks. When required validation passes, calculate acceptance coverage as the number of distinct exact criteria addressed divided by the number required; use `1.0` when both sets are empty and `0.0` when validation fails. QA later reviews the claimed mapping but cannot raise candidate eligibility.

Calculate candidate risk deterministically: high when more than 5 files or 500 changed lines are touched; medium when more than 2 files or 100 changed lines are touched; low otherwise. If any touched path is `pyproject.toml`, a dependency lockfile, `.github/`, or a public package `__init__.py`, raise the result by one level, capped at high.

- [ ] **Step 5: Run focused tests and static checks**

Run: `pytest tests/unit/test_patching.py tests/unit/test_validation.py tests/unit/test_candidates.py -q`

Expected: PASS.

Run: `ruff check src/repogent/patching.py src/repogent/validation.py src/repogent/candidates.py tests/unit/test_candidates.py && mypy src/repogent/patching.py src/repogent/validation.py src/repogent/candidates.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/repogent/patching.py src/repogent/validation.py src/repogent/candidates.py tests/unit/test_patching.py tests/unit/test_validation.py tests/unit/test_candidates.py
git commit -m "feat: evaluate patches with reversible transactions"
```

---

### Task 6: Adaptive Candidate Policy and Explainable Selection

**Files:**
- Modify: `src/repogent/candidates.py`
- Modify: `tests/unit/test_candidates.py`

**Interfaces:**
- Consumes: `LocalizationReport`, `CandidateRecord`, and `CandidateEvidence`.
- Produces: `ExpansionReason`, `CandidatePolicy.should_expand(localization: LocalizationReport, evidence: CandidateEvidence, candidate_count: int) -> ExpansionReason | None`, `CandidateSelector.select(candidates: Sequence[CandidateRecord], evidence: Sequence[CandidateEvidence]) -> CandidateSelection`.

- [ ] **Step 1: Write the candidate-policy decision table as tests**

```python
@pytest.mark.parametrize(
    ("ambiguous", "eligible", "risk", "changed_lines", "coverage", "expected"),
    [
        (False, True, RiskLevel.LOW, 4, 1.0, None),
        (True, True, RiskLevel.LOW, 4, 1.0, ExpansionReason.AMBIGUOUS_LOCALIZATION),
        (False, False, RiskLevel.LOW, 4, 0.0, ExpansionReason.VALIDATION_FAILED),
        (False, True, RiskLevel.HIGH, 4, 1.0, ExpansionReason.HIGH_RISK),
        (False, True, RiskLevel.LOW, 501, 1.0, ExpansionReason.BROAD_PATCH),
        (False, True, RiskLevel.LOW, 4, 0.5, ExpansionReason.INCOMPLETE_ACCEPTANCE),
    ],
)
def test_candidate_policy_expands_only_for_objective_reasons(
    ambiguous: bool,
    eligible: bool,
    risk: RiskLevel,
    changed_lines: int,
    coverage: float,
    expected: ExpansionReason | None,
) -> None:
    localization = localization_report(ambiguous=ambiguous)
    evidence = candidate_evidence(
        eligible=eligible,
        risk=risk,
        changed_lines=changed_lines,
        coverage=coverage,
    )
    policy = CandidatePolicy(max_candidates=3, broad_patch_lines=500)
    assert policy.should_expand(localization, evidence, candidate_count=1) is expected
```

Use these helpers below the imports:

```python
def localization_report(*, ambiguous: bool) -> LocalizationReport:
    return LocalizationReport(
        locations=[],
        snippets=[],
        ambiguous=ambiguous,
        ambiguity_reason="top locations are not concentrated" if ambiguous else None,
    )


def candidate_evidence(
    *, eligible: bool, risk: RiskLevel, changed_lines: int, coverage: float
) -> CandidateEvidence:
    status = CheckStatus.PASSED if eligible else CheckStatus.FAILED
    return CandidateEvidence(
        candidate_id="candidate-1",
        validation=ValidationReport(
            checks=[
                CheckResult(
                    name="pytest",
                    argv=["pytest"],
                    status=status,
                    exit_code=0 if eligible else 1,
                )
            ]
        ),
        acceptance_criteria_coverage=coverage,
        risk_level=risk,
        changed_files=1,
        changed_lines=changed_lines,
        duration_seconds=1,
        required_failures=[] if eligible else ["pytest"],
        restored_to_baseline=True,
    )
```

Add selector tests proving: ineligible candidates never win; identical diffs deduplicate by hash; passing more required checks ranks before diff size; equal evidence returns `ambiguous=True`; and at most three candidate IDs are accepted.

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/unit/test_candidates.py -q`

Expected: FAIL because the policy and selector do not exist.

- [ ] **Step 3: Implement expansion reasons and hard candidate cap**

```python
class ExpansionReason(StrEnum):
    AMBIGUOUS_LOCALIZATION = "ambiguous_localization"
    VALIDATION_FAILED = "validation_failed"
    HIGH_RISK = "high_risk"
    BROAD_PATCH = "broad_patch"
    INCOMPLETE_ACCEPTANCE = "incomplete_acceptance"
```

`CandidatePolicy.should_expand()` checks in this order: maximum reached, validation eligibility, localization ambiguity, high risk, changed-line threshold, acceptance coverage. Return the first objective reason or `None`. Reject `max_candidates` outside `1..3`.

- [ ] **Step 4: Implement deterministic eligibility, deduplication, and comparison**

`CandidateSelector.select()` first validates one-to-one candidate/evidence IDs, filters `evidence.eligible`, and deduplicates by `diff_sha256`. Rank survivors with this tuple:

```python
(
    -len(evidence.required_failures),
    evidence.acceptance_criteria_coverage,
    -len(evidence.skipped_checks),
    -evidence.changed_files,
    -evidence.changed_lines,
    -float(candidate.usage.estimated_cost_usd),
)
```

If no candidate survives, select `None` with reason `no candidate passed required validation`. If the first two rank tuples are equal, return `ambiguous=True` and no selected ID. Otherwise select the highest tuple and render a reason naming the decisive evidence fields.

- [ ] **Step 5: Run tests and static checks**

Run: `pytest tests/unit/test_candidates.py -q`

Expected: PASS.

Run: `ruff check src/repogent/candidates.py tests/unit/test_candidates.py && mypy src/repogent/candidates.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/repogent/candidates.py tests/unit/test_candidates.py
git commit -m "feat: add adaptive candidate selection policy"
```

---

### Task 7: Integrate Localization, Candidates, and Events into the Workflow

**Files:**
- Modify: `src/repogent/workflow.py`
- Modify: `src/repogent/agents.py`
- Modify: `tests/unit/test_workflow.py`
- Modify: `tests/unit/test_agents.py`

**Interfaces:**
- Consumes: `PythonSymbolGraphBuilder`, `PythonLocalizer`, `CandidateEvaluator`, `CandidatePolicy`, `CandidateSelector`, `EventSink`, and existing `RoleSet`.
- Produces: workflow artifacts `symbol-graph`, `localization`, `candidate`, `candidate-evidence`, and `candidate-selection`; emits monotonic run events; applies only the approved selected candidate.

- [ ] **Step 1: Replace repair-loop expectations with failing adaptive-flow tests**

Add workflow tests for these complete behaviors:

```python
def test_valid_first_candidate_is_only_candidate_and_is_applied(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )
    manifest = workflow.run()
    assert manifest.status is RunStatus.COMPLETED
    assert manifest.candidate_ids == ["candidate-1"]
    assert manifest.selected_candidate_id == "candidate-1"
    assert len(list(workflow.artifacts.root.glob("candidate-[0-9][0-9][0-9].json"))) == 1


def test_failed_candidate_triggers_one_evidence_informed_alternative(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[
            REQUIREMENTS_OUTPUT,
            PLAN_OUTPUT,
            INVALID_PATCH_OUTPUT,
            VALID_PATCH_OUTPUT,
            QA_OUTPUT,
        ],
        validation_statuses=[
            CheckStatus.FAILED,
            CheckStatus.PASSED,
            CheckStatus.PASSED,
        ],
    )
    manifest = workflow.run()
    assert manifest.candidate_ids == ["candidate-1", "candidate-2"]
    repair_input = json.loads(next(workflow.artifacts.root.glob("candidate-input-002.txt")).read_text())
    assert repair_input["previous_failure"]["candidate_id"] == "candidate-1"


def test_ambiguous_selection_requires_human_without_applying(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[
            REQUIREMENTS_OUTPUT,
            PLAN_OUTPUT,
            VALID_PATCH_OUTPUT,
            ALTERNATIVE_PATCH_OUTPUT,
        ],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
        candidate_policy=CandidatePolicy(max_candidates=2, broad_patch_lines=1),
    )
    manifest = workflow.run()
    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "candidate evidence is ambiguous"
    assert (workflow.root / "app.py").read_text() == "value = 1\n"
```

Define the fixture data and replace the existing `make_workflow()` helper with this Phase 2 constructor:

```python
REQUIREMENTS_OUTPUT = {
    "objective": "Change value",
    "functional_requirements": ["value is 2"],
    "acceptance_criteria": ["tests pass"],
}
PLAN_OUTPUT = {
    "files_to_modify": ["app.py"],
    "steps": [{"id": "change", "description": "Change value"}],
    "tests": ["pytest"],
}
VALID_PATCH_OUTPUT = {
    "summary": "Change value",
    "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n",
    "acceptance_criteria_addressed": ["tests pass"],
    "focused_tests": ["pytest"],
}
ALTERNATIVE_PATCH_OUTPUT = {
    "summary": "Change value alternatively",
    "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 3\n",
    "acceptance_criteria_addressed": ["tests pass"],
    "focused_tests": ["pytest"],
}
INVALID_PATCH_OUTPUT = {
    **VALID_PATCH_OUTPUT,
    "summary": "First candidate fails its focused test",
}
QA_OUTPUT = {
    "acceptance_criteria_coverage": 1,
    "test_quality_score": 1,
    "security_score": 1,
    "regression_risk": "low",
    "merge_recommendation": "approve",
}


def make_phase2_workflow(
    tmp_path: Path,
    *,
    outputs: list[dict[str, object]],
    validation_statuses: list[CheckStatus],
    candidate_policy: CandidatePolicy | None = None,
) -> Workflow:
    target = tmp_path / "target"
    target.mkdir()
    (target / "app.py").write_text("def value():\n    return 1\n")
    store = ArtifactStore.create(tmp_path / "runs", target, "change", run_id="run-1")
    validator = SequenceValidator(validation_statuses)
    patch_policy = PatchPolicy()
    patch_applier = PatchApplier()
    return Workflow(
        root=target,
        request="change value",
        manifest=RunManifest(
            run_id="run-1",
            request="change value",
            events_file="events.jsonl",
        ),
        roles=RoleSet.from_provider(ScriptedProvider(outputs)),
        approver=FakeApprover([Decision.APPROVED] * 4),
        patch_policy=patch_policy,
        patch_applier=patch_applier,
        validator=validator,
        artifacts=store,
        inspector=RepositoryInspector(),
        symbol_builder=PythonSymbolGraphBuilder(),
        localizer=PythonLocalizer(),
        candidate_evaluator=CandidateEvaluator(
            patch_policy,
            patch_applier,
            validator,
        ),
        candidate_policy=candidate_policy or CandidatePolicy(),
        candidate_selector=CandidateSelector(),
        events=store.event_store(),
        budget=Budget(),
    )
```

Update the existing `SequenceValidator.run()` test double to accept `timeout_seconds: float | None = None` and discard it before returning the next configured result. This makes candidate evaluation and final validation use the same explicit validator contract.

Also assert events have consecutive sequences and that every candidate evaluation leaves the target at baseline before approval.

- [ ] **Step 2: Run workflow tests and verify failure**

Run: `pytest tests/unit/test_workflow.py tests/unit/test_agents.py -q`

Expected: FAIL because `Workflow` still uses one applied patch plus repairs.

- [ ] **Step 3: Add an event emitter and Phase 2 dependencies**

Add fields `symbol_builder`, `localizer`, `candidate_evaluator`, `candidate_policy`, `candidate_selector`, and `events` to `Workflow`. Add `_sequence: int = field(default=0, init=False)` and:

```python
def emit(self, kind: EventKind, message: str, **data: object) -> None:
    self._sequence += 1
    self.events.emit(
        RunEvent(
            run_id=self.manifest.run_id,
            sequence=self._sequence,
            kind=kind,
            stage=self.manifest.stage.value,
            message=message,
            data=data,
        )
    )
```

Emit at preflight import, stage changes, model completion, candidate creation, validation completion, approval decision, warnings, and terminal status. Event writes are part of evidence integrity: an event-store error ends the run as `human_intervention_required`.

- [ ] **Step 4: Replace lexical context with graph localization**

After inspection, build and persist the graph, localize with the request plus requirements acceptance criteria after requirements generation, and persist the localization report. If the report has no locations or is ambiguous, perform exactly one broader deterministic pass with doubled snippet and character budgets plus available failing-test evidence, then persist that second report separately. If the broadened report still has no locations, finish with `human_intervention_required`. If it remains ambiguous, continue to planning but force the candidate policy to consider an alternative. Never add an unbounded third pass.

Pass `localization.snippets` to planning and implementation payloads and include the full localization report separately so the model sees selection reasons.

- [ ] **Step 5: Replace applied-first repair with evaluate-select-approve-apply**

Create candidate IDs sequentially. Candidate 1 uses `roles.implementation`; alternatives use `roles.repair` and include the previous candidate plus objective evidence failure. For each proposal:

1. account for provider usage;
2. construct and persist `CandidateRecord` with SHA-256 of the exact diff;
3. evaluate it transactionally;
4. persist `CandidateEvidence`;
5. ask `CandidatePolicy.should_expand()` and stop when it returns `None` or three candidates exist.

Run `CandidateSelector.select()`. If no candidate is selected, finish as `human_intervention_required` with the selector reason. Present the selected proposal plus comparison to `ApprovalKind.PATCH`. On approval, validate the patch policy again against the current baseline, apply it once, run final validation, and assert its required results match the candidate evidence. A mismatch ends as `human_intervention_required` and is reported as changed validation evidence.

Remove the old applied-first repair loop and `repair_attempts` orchestration from the active path, but retain the manifest field for schema compatibility and set it to `candidate_count - 1`.

- [ ] **Step 6: Feed acceptance coverage and selection evidence to QA**

The QA payload must include all acceptance criteria, selected candidate, selection reason, final validation, and diff. Use the QA result only after deterministic validation passes. QA cannot change candidate eligibility or override failed checks.

- [ ] **Step 7: Run workflow tests and the end-to-end test**

Run: `pytest tests/unit/test_workflow.py tests/unit/test_agents.py tests/integration/test_end_to_end.py -q`

Expected: PASS. Update scripted fixtures so their output order is requirements, plan, candidate(s), and QA.

Run: `ruff check src/repogent/workflow.py src/repogent/agents.py tests/unit/test_workflow.py && mypy src/repogent/workflow.py src/repogent/agents.py`

Expected: both commands exit 0.

- [ ] **Step 8: Commit**

```bash
git add src/repogent/workflow.py src/repogent/agents.py tests/unit/test_workflow.py tests/unit/test_agents.py tests/integration/test_end_to_end.py examples/scripted_run.json
git commit -m "feat: orchestrate adaptive evidence-backed candidates"
```

---

### Task 8: Live CLI Timeline and Complete Evidence Report

**Files:**
- Modify: `src/repogent/events.py`
- Modify: `src/repogent/cli.py`
- Modify: `src/repogent/reporting.py`
- Modify: `tests/unit/test_events.py`
- Modify: `tests/unit/test_cli.py`
- Modify: `tests/unit/test_reporting.py`

**Interfaces:**
- Consumes: `RunEvent`, `JsonlEventStore`, `CandidateSelection`, candidate artifacts.
- Produces: `CompositeEventSink`, `ConsoleEventSink`, concise default timeline, stable Markdown candidate comparison.

- [ ] **Step 1: Write failing console and report tests**

```python
def test_console_sink_renders_concise_timeline() -> None:
    output: list[str] = []
    sink = ConsoleEventSink(output.append)
    sink.emit(RunEvent(run_id="r", sequence=1, kind=EventKind.STAGE, message="Localizing"))
    sink.emit(
        RunEvent(
            run_id="r",
            sequence=2,
            kind=EventKind.VALIDATION,
            message="candidate-1 passed",
            data={"passed": 4, "failed": 0, "skipped": 1, "cost_usd": "0.18"},
        )
    )
    assert output == ["[stage] Localizing", "[validation] candidate-1 passed (4 passed, 0 failed, 1 skipped, $0.18)"]
```

Add a report test asserting sections `Localization`, `Candidate comparison`, `Selection`, `Deterministic validation`, `Cost and duration`, and `Recovery` appear and name rejected candidates.

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/unit/test_events.py tests/unit/test_cli.py tests/unit/test_reporting.py -q`

Expected: FAIL because console/composite sinks and Phase 2 report inputs do not exist.

- [ ] **Step 3: Implement console and composite sinks**

```python
class CompositeEventSink:
    def __init__(self, sinks: Sequence[EventSink]) -> None:
        self.sinks = tuple(sinks)

    def emit(self, event: RunEvent) -> None:
        for sink in self.sinks:
            sink.emit(event)


class ConsoleEventSink:
    def __init__(self, write: Callable[[str], object]) -> None:
        self.write = write

    def emit(self, event: RunEvent) -> None:
        label = event.kind.value
        suffix = ""
        if event.kind is EventKind.VALIDATION:
            suffix = (
                f" ({event.data.get('passed', 0)} passed, "
                f"{event.data.get('failed', 0)} failed, "
                f"{event.data.get('skipped', 0)} skipped"
                f"{', $' + str(event.data['cost_usd']) if 'cost_usd' in event.data else ''})"
            )
        self.write(f"[{label}] {event.message}{suffix}")
```

Use `CompositeEventSink([store.event_store(), ConsoleEventSink(typer.echo)])` in the CLI. Keep raw command output in artifacts, not the default terminal timeline.

- [ ] **Step 4: Extend report inputs and rendering**

Change `render_report()` to accept `localization: LocalizationReport | None`, `candidates: Sequence[tuple[CandidateRecord, CandidateEvidence]]`, and `selection: CandidateSelection | None`. Render a compact Markdown table with candidate, eligible, required failures, skipped checks, changed files/lines, coverage, cost, and selected marker. Include the localization ambiguity reason and explicit recovery state.

- [ ] **Step 5: Run tests and static checks**

Run: `pytest tests/unit/test_events.py tests/unit/test_cli.py tests/unit/test_reporting.py -q`

Expected: PASS.

Run: `ruff check src/repogent/events.py src/repogent/cli.py src/repogent/reporting.py tests/unit/test_events.py tests/unit/test_reporting.py && mypy src/repogent/events.py src/repogent/cli.py src/repogent/reporting.py`

Expected: both commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/repogent/events.py src/repogent/cli.py src/repogent/reporting.py tests/unit/test_events.py tests/unit/test_cli.py tests/unit/test_reporting.py
git commit -m "feat: show live runs and candidate evidence"
```

---

### Task 9: Broad-Python Integration Fixtures and v0.2 Local Reliability Gate

**Files:**
- Create: `tests/fixtures/python_library/src/example_math/__init__.py`
- Create: `tests/fixtures/python_library/tests/test_math.py`
- Create: `tests/fixtures/python_library/pyproject.toml`
- Create: `tests/fixtures/python_cli/src/example_cli/__init__.py`
- Create: `tests/fixtures/python_cli/src/example_cli/__main__.py`
- Create: `tests/fixtures/python_cli/tests/test_cli.py`
- Create: `tests/fixtures/python_cli/pyproject.toml`
- Create: `tests/fixtures/python_data/src/example_data/transform.py`
- Create: `tests/fixtures/python_data/tests/test_transform.py`
- Create: `tests/fixtures/python_data/pyproject.toml`
- Create: `tests/integration/test_phase2_local_reliability.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/security.md`

**Interfaces:**
- Consumes: the full Phase 2 local engine through public CLI and workflow constructors.
- Produces: reproducible library, CLI, and data-package coverage plus documented local workflow and limitations.

- [ ] **Step 1: Add three minimal repository fixtures**

Each fixture must be dependency-light and execute with the existing validator environment. Use these behaviors:

```python
# python_library/src/example_math/__init__.py
def clamp(value: int, lower: int, upper: int) -> int:
    return min(max(value, lower), upper)

# python_cli/src/example_cli/__main__.py
def greeting(name: str) -> str:
    return f"Hello, {name}!"

# python_data/src/example_data/transform.py
def normalize_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{key.strip(): value.strip() for key, value in row.items()} for row in rows]
```

Give each fixture a focused pytest suite and minimal PEP 621 `pyproject.toml`. Do not add third-party fixture dependencies.

- [ ] **Step 2: Write failing end-to-end Phase 2 tests**

Parameterize over the three fixtures. For each, copy to `tmp_path`, use a scripted provider, restricted local executor, automatic fake approvals, and assert:

- symbol-aware localization identifies the intended function;
- a valid first candidate produces exactly one candidate;
- the candidate workspace restores to the baseline before approval;
- the approved patch applies once;
- final tests pass;
- `events.jsonl`, candidate artifacts, `run.json`, and `report.md` exist;
- the manifest includes repository/configuration fingerprints and the selected candidate.

Add one failure scenario where all three candidates fail required validation and assert unchanged target files plus `human_intervention_required`.

- [ ] **Step 3: Run integration tests and verify failure before fixture scripts are aligned**

Run: `pytest tests/integration/test_phase2_local_reliability.py -q`

Expected: FAIL until the exact scripted outputs and workflow constructor use the completed Phase 2 interfaces.

- [ ] **Step 4: Complete scripted outputs and integration wiring**

Create the exact typed requirements, plan, patch candidates, and QA outputs inside test helpers. Keep them local to the integration test so benchmark artifacts are not implied. Use the same `CompositeEventSink` and artifact store as the CLI.

- [ ] **Step 5: Update user-facing documentation**

Document:

- supported broad-Python scope and known static-analysis limitations;
- preflight and explicit Docker/local behavior;
- five workflow stages;
- when alternatives are generated;
- candidate evidence and ambiguity behavior;
- artifact filenames and schema version;
- recovery and terminal statuses;
- that benchmark, headless CI, and GitHub integration are subsequent Phase 2 increments.

Include a tested local example command and a sample concise timeline. Do not advertise unreleased benchmark or GitHub capabilities.

- [ ] **Step 6: Run the complete verification gate**

Run: `pytest`

Expected: PASS with coverage at or above 85%.

Run: `ruff check .`

Expected: exit 0.

Run: `mypy src/repogent`

Expected: exit 0 with no errors.

Run: `bandit -q -r src/repogent`

Expected: exit 0 with no findings.

Run when Docker is available: `pytest -m docker tests/integration/test_docker_execution.py -q`

Expected: PASS; otherwise record that Docker verification remains outstanding and do not claim it passed.

- [ ] **Step 7: Commit**

```bash
git add tests/fixtures tests/integration/test_phase2_local_reliability.py README.md docs/architecture.md docs/security.md
git commit -m "test: verify v0.2 across Python repository shapes"
```

---

## Completion Review

Before calling this increment complete:

- Confirm a successful low-risk run creates only `candidate-1`.
- Confirm each objective trigger can expand to `candidate-2` and never beyond `candidate-3`.
- Confirm all candidate evaluations restore byte-for-byte baseline state before approval.
- Confirm no candidate with a required failure can be selected.
- Confirm equal evidence produces ambiguity rather than an arbitrary winner.
- Confirm failed preflight constructs no provider and spends no model budget.
- Confirm JSONL events are monotonic, sanitized, and match the manifest link.
- Confirm reports retain rejected candidates and selection reasons.
- Confirm Docker remains the default and local fallback remains explicit.
- Confirm library, CLI, web-service MVP fixture, and data-package integration tests pass.
- Confirm the full quality gate and, when available, Docker integration test have fresh passing output.

## Subsequent Plans

After this plan is implemented and reviewed, create separate plans in this order:

1. real-repository benchmark harness and published metrics;
2. public v0.2 packaging, installation verification, docs, and release automation;
3. headless CI policy, stable exit codes, and reusable workflow;
4. GitHub issue and pull-request adapter.

Each subsequent plan must consume the interfaces stabilized here instead of adding a parallel workflow engine.
