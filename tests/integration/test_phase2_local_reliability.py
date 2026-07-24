from __future__ import annotations

import difflib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repogent.agents import RoleSet
from repogent.approvals import FakeApprover
from repogent.artifacts import ArtifactStore
from repogent.candidates import CandidateEvaluator, CandidatePolicy, CandidateSelector
from repogent.cli import app
from repogent.domain import (
    ApprovalKind,
    Budget,
    Decision,
    EventKind,
    RunEvent,
    RunManifest,
    RunStage,
    RunStatus,
    ValidationReport,
)
from repogent.events import CompositeEventSink, ConsoleEventSink
from repogent.execution import LocalExecutor, ValidationPolicy
from repogent.localization import PythonLocalizer
from repogent.patching import PatchApplier, PatchPolicy, ValidatedPatch
from repogent.preflight import Preflight, configuration_fingerprint
from repogent.providers import ScriptedProvider
from repogent.repository import RepositoryInspector
from repogent.symbols import PythonSymbolGraphBuilder
from repogent.validation import ValidationPipeline
from repogent.workflow import Workflow

FIXTURES = Path(__file__).parents[1] / "fixtures"


@dataclass(frozen=True)
class FixtureCase:
    name: str
    source_path: str
    test_path: str
    function: str
    request: str
    before_source: str
    after_source: str
    before_test: str
    after_test: str


def _fixture_case(name: str) -> FixtureCase:
    root = FIXTURES / name
    if name == "python_library":
        return FixtureCase(
            name=name,
            source_path="src/example_math/__init__.py",
            test_path="tests/test_math.py",
            function="clamp",
            request="Reject inverted clamp bounds",
            before_source=(root / "src/example_math/__init__.py").read_text(),
            after_source=(
                "def clamp(value: int, lower: int, upper: int) -> int:\n"
                "    if lower > upper:\n"
                '        raise ValueError("lower must not exceed upper")\n'
                "    return min(max(value, lower), upper)\n"
            ),
            before_test=(root / "tests/test_math.py").read_text(),
            after_test=(
                "import pytest\n\n"
                "from example_math import clamp as limit\n\n\n"
                "def test_limits_values() -> None:\n"
                "    assert limit(9, 0, 5) == 5\n\n\n"
                "def test_rejects_inverted_bounds() -> None:\n"
                "    with pytest.raises(ValueError, match=\"lower\"):\n"
                "        limit(1, 5, 0)\n"
            ),
        )
    if name == "python_cli":
        return FixtureCase(
            name=name,
            source_path="src/example_cli/__main__.py",
            test_path="tests/test_cli.py",
            function="greeting",
            request="Trim whitespace from greeting names",
            before_source=(root / "src/example_cli/__main__.py").read_text(),
            after_source=(
                "def greeting(name: str) -> str:\n"
                "    return f\"Hello, {name.strip()}!\"\n\n\n"
                "if __name__ == \"__main__\":\n"
                "    print(greeting(\"world\"))\n"
            ),
            before_test=(root / "tests/test_cli.py").read_text(),
            after_test=(
                "from example_cli.__main__ import greeting\n\n\n"
                "def test_greeting() -> None:\n"
                "    assert greeting(\"Ada\") == \"Hello, Ada!\"\n\n\n"
                "def test_greeting_trims_name() -> None:\n"
                "    assert greeting(\" Ada \") == \"Hello, Ada!\"\n"
            ),
        )
    if name == "python_data":
        return FixtureCase(
            name=name,
            source_path="src/example_data/transform.py",
            test_path="tests/test_transform.py",
            function="normalize_rows",
            request="Make normalize_rows lowercase keys",
            before_source=(root / "src/example_data/transform.py").read_text(),
            after_source=(
                "def normalize_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:\n"
                "    return [\n"
                "        {key.strip().lower(): value.strip() for key, value in row.items()}\n"
                "        for row in rows\n"
                "    ]\n"
            ),
            before_test=(root / "tests/test_transform.py").read_text(),
            after_test=(
                "from example_data.transform import normalize_rows as clean\n\n\n"
                "def test_trims_cells() -> None:\n"
                "    assert clean([{\" name \": \" Ada \"}]) == [{\"name\": \"Ada\"}]\n\n\n"
                "def test_lowercases_keys() -> None:\n"
                "    assert clean([{\" Name \": \"Ada\"}]) == [{\"name\": \"Ada\"}]\n"
            ),
        )
    raise ValueError(f"unknown fixture: {name}")


def _unified_diff(path: str, before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    ) + "\n"


def _patch_output(case: FixtureCase, *, summary: str | None = None) -> dict[str, object]:
    return {
        "summary": summary or case.request,
        "diff": (
            _unified_diff(case.source_path, case.before_source, case.after_source)
            + _unified_diff(case.test_path, case.before_test, case.after_test)
        ),
        "acceptance_criteria_addressed": [
            f"{case.function} behavior matches the request",
            "Validation succeeds",
        ],
        "focused_tests": ["python -m pytest -q"],
    }


def _wrong_patch_output(case: FixtureCase, body: str, summary: str) -> dict[str, object]:
    """Return a policy-valid but behaviorally incorrect source-only patch."""
    before_line = "    return min(max(value, lower), upper)\n"
    if case.name != "python_library" or before_line not in case.before_source:
        raise ValueError("wrong-patch fixture is defined for the clamp library only")
    after_source = case.before_source.replace(before_line, f"    {body}\n")
    return {
        "summary": summary,
        "diff": _unified_diff(case.source_path, case.before_source, after_source),
        "acceptance_criteria_addressed": [
            f"{case.function} behavior matches the request",
            "Validation succeeds",
        ],
        "focused_tests": ["python -m pytest -q"],
    }


def _scripted_outputs(case: FixtureCase) -> list[dict[str, object]]:
    return [
        {
            "objective": case.request,
            "functional_requirements": [f"Update {case.function} safely"],
            "acceptance_criteria": [
                f"{case.function} behavior matches the request",
                "Validation succeeds",
            ],
            "risk_level": "low",
        },
        {
            "files_to_inspect": [case.source_path, case.test_path],
            "files_to_modify": [case.source_path, case.test_path],
            "steps": [
                {"id": "update_behavior", "description": f"Update {case.function}"},
                {
                    "id": "test_behavior",
                    "description": "Add focused regression coverage",
                    "depends_on": ["update_behavior"],
                },
            ],
            "tests": ["python -m pytest -q"],
            "security_considerations": ["Use no new dependencies or commands"],
            "regression_risks": ["Existing fixture behavior must remain covered"],
        },
        _patch_output(case),
        {
            "acceptance_criteria_coverage": 1,
            "test_quality_score": 1,
            "security_score": 1,
            "regression_risk": "low",
            "findings": [],
            "merge_recommendation": "approve",
        },
    ]


class RecordingApprover(FakeApprover):
    def __init__(self, root: Path, baseline: dict[str, bytes]) -> None:
        super().__init__([Decision.APPROVED] * 3)
        self.root = root
        self.baseline = baseline
        self.patch_approval_calls = 0

    def decide(self, kind: ApprovalKind, artifact: object) -> object:
        if kind is ApprovalKind.PATCH:
            self.patch_approval_calls += 1
            assert _tree_bytes(self.root) == self.baseline
        return super().decide(kind, artifact)  # type: ignore[arg-type]


class CountingPatchApplier(PatchApplier):
    def __init__(self, target_root: Path) -> None:
        self.target_root = target_root.resolve()
        self.target_apply_calls = 0

    def apply(self, root: Path, patch: ValidatedPatch) -> None:
        if root.resolve() == self.target_root:
            self.target_apply_calls += 1
        super().apply(root, patch)


class RecordingValidator:
    def __init__(self, pipeline: ValidationPipeline) -> None:
        self.pipeline = pipeline
        self.roots: list[Path] = []

    def run(self, root: Path, *, timeout_seconds: float | None = None) -> ValidationReport:
        self.roots.append(root.resolve())
        return self.pipeline.run(root, timeout_seconds=timeout_seconds)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }


def _tree_modes(root: Path) -> dict[str, int]:
    return {
        path.relative_to(root).as_posix(): path.stat().st_mode
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }


def _workflow_for(
    root: Path,
    artifacts_root: Path,
    case: FixtureCase,
    *,
    outputs: list[dict[str, object]],
    approver: FakeApprover,
    validator: object,
    patch_applier: PatchApplier,
) -> Workflow:
    policy = ValidationPolicy()
    executor = LocalExecutor(
        allowed={command.name: command.argv for command in policy.commands(root)}
    )
    preflight = Preflight(executor, policy).run(root)
    assert preflight.passed
    store = ArtifactStore.create(artifacts_root, root, case.request, run_id=f"{case.name}-run")
    return Workflow(
        root=root,
        request=case.request,
        manifest=RunManifest(
            run_id=store.root.name,
            request=case.request,
            repository_fingerprint=preflight.repository_fingerprint,
            configuration_fingerprint=configuration_fingerprint(
                "scripted", "test", "local", policy.commands(root)
            ),
        ),
        roles=RoleSet.from_provider(ScriptedProvider(outputs)),
        approver=approver,
        patch_policy=PatchPolicy(),
        patch_applier=patch_applier,
        validator=validator,  # type: ignore[arg-type]
        artifacts=store,
        inspector=RepositoryInspector(),
        symbol_builder=PythonSymbolGraphBuilder(),
        localizer=PythonLocalizer(),
        candidate_evaluator=CandidateEvaluator(PatchPolicy(), patch_applier, validator),  # type: ignore[arg-type]
        candidate_policy=CandidatePolicy(),
        candidate_selector=CandidateSelector(),
        events=CompositeEventSink((store.event_store(), ConsoleEventSink(lambda _line: None))),
        budget=Budget(timeout_seconds=120),
    )


@pytest.mark.parametrize("name", ["python_library", "python_cli", "python_data"])
def test_phase2_workflow_verifies_a_low_risk_patch_across_python_shapes(
    tmp_path: Path, name: str
) -> None:
    case = _fixture_case(name)
    target = tmp_path / case.name
    shutil.copytree(FIXTURES / case.name, target)
    baseline = _tree_bytes(target)
    policy = ValidationPolicy()
    executor = LocalExecutor(
        allowed={command.name: command.argv for command in policy.commands(target)}
    )
    validator = RecordingValidator(ValidationPipeline(executor, policy))
    approver = RecordingApprover(target, baseline)
    patch_applier = CountingPatchApplier(target)
    workflow = _workflow_for(
        target,
        tmp_path / "runs",
        case,
        outputs=_scripted_outputs(case),
        approver=approver,
        validator=validator,
        patch_applier=patch_applier,
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.COMPLETED
    assert workflow.localization is not None
    assert workflow.localization.locations[0].symbol_id.endswith(f".{case.function}")
    assert manifest.candidate_ids == ["candidate-1"]
    assert manifest.selected_candidate_id == "candidate-1"
    assert approver.patch_approval_calls == 1
    assert patch_applier.target_apply_calls == 1
    assert _tree_bytes(target)[case.source_path] == case.after_source.encode()
    assert _tree_bytes(target)[case.test_path] == case.after_test.encode()
    assert len(validator.roots) == 2
    assert all(root != target.resolve() for root in validator.roots)
    assert workflow.validation is not None and workflow.validation.passed

    evidence = workflow.artifacts.root
    assert (evidence / "events.jsonl").exists()
    assert (evidence / "candidate-001.json").exists()
    assert (evidence / "candidate-evidence-001.json").exists()
    assert (evidence / "run.json").exists()
    assert (evidence / "report.md").exists()
    selection = json.loads(
        next(evidence.glob("candidate-selection-*.json")).read_text()
    )
    assert selection["selected_candidate_id"] == "candidate-1"
    persisted = json.loads((evidence / "run.json").read_text())
    assert persisted["repository_fingerprint"]
    assert persisted["configuration_fingerprint"]
    assert persisted["selected_candidate_id"] == "candidate-1"
    assert persisted["events_file"] == "events.jsonl"
    assert persisted["execution_mode"] == "local"
    assert persisted["verification_status"] == "passed"
    assert persisted["preview_digest"]
    events = [
        RunEvent.model_validate(json.loads(line))
        for line in (evidence / "events.jsonl").read_text().splitlines()
    ]
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert {event.run_id for event in events} == {manifest.run_id}
    terminal = events[-1]
    assert terminal.kind is EventKind.TERMINAL
    assert terminal.stage == RunStage.FINISHED.value
    assert terminal.data["status"] == manifest.status.value
    report = (evidence / "report.md").read_text()
    assert "candidate-1" in report
    assert "| selected |" in report
    assert "Verification: REDUCED ISOLATION" in report
    assert "Execution mode: local" in report
    assert f"Preview digest: {persisted['preview_digest']}" in report


@pytest.mark.parametrize("name", ["python_library", "python_cli", "python_data"])
def test_analyze_cli_exposes_the_intended_python_symbol(tmp_path: Path, name: str) -> None:
    case = _fixture_case(name)
    target = tmp_path / case.name
    shutil.copytree(FIXTURES / case.name, target)

    result = CliRunner().invoke(app, ["analyze", str(target), "--request", case.request])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    symbols = payload["localization"]["locations"]
    assert symbols[0]["symbol_id"].endswith(f".{case.function}")


def test_all_failed_candidates_leave_the_real_repository_unchanged(tmp_path: Path) -> None:
    case = _fixture_case("python_library")
    target = tmp_path / case.name
    shutil.copytree(FIXTURES / case.name, target)
    baseline = _tree_bytes(target)
    baseline_modes = _tree_modes(target)
    patch_applier = CountingPatchApplier(target)
    policy = ValidationPolicy()
    executor = LocalExecutor(
        allowed={command.name: command.argv for command in policy.commands(target)}
    )
    validator = RecordingValidator(ValidationPipeline(executor, policy))
    approver = FakeApprover([Decision.APPROVED, Decision.APPROVED])
    workflow = _workflow_for(
        target,
        tmp_path / "runs",
        case,
        outputs=[
            *_scripted_outputs(case)[:2],
            _wrong_patch_output(case, "return lower", "First failed candidate"),
            _wrong_patch_output(case, "return value", "Second failed candidate"),
            _wrong_patch_output(case, "return upper - 1", "Third failed candidate"),
        ],
        approver=approver,
        validator=validator,
        patch_applier=patch_applier,
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "no candidate passed required validation"
    assert manifest.candidate_ids == ["candidate-1", "candidate-2", "candidate-3"]
    assert manifest.selected_candidate_id is None
    assert _tree_bytes(target) == baseline
    assert _tree_modes(target) == baseline_modes
    assert patch_applier.target_apply_calls == 0
    assert [record.kind for record in approver.records] == [
        ApprovalKind.REQUIREMENTS,
        ApprovalKind.PLAN,
    ]
    candidate_evidence = [
        json.loads(path.read_text())
        for path in sorted(workflow.artifacts.root.glob("candidate-evidence-*.json"))
    ]
    assert len(candidate_evidence) == 3
    assert all("pytest" in item["required_failures"] for item in candidate_evidence)
    assert all(
        any(
            check["name"] == "pytest"
            and check["required"]
            and check["status"] == "failed"
            and check["exit_code"] != 0
            for check in item["validation"]["checks"]
        )
        for item in candidate_evidence
    )
    assert len(validator.roots) == 3
    assert len(set(validator.roots)) == 3
    assert all(root != target.resolve() for root in validator.roots)
    report = (workflow.artifacts.root / "report.md").read_text()
    assert all(candidate_id in report for candidate_id in manifest.candidate_ids)
    assert "rejected" in report
    assert "Verification: REDUCED ISOLATION" in report
    assert "Execution mode: local" in report


def test_nested_suffix_test_failure_makes_candidates_ineligible_and_unselected(
    tmp_path: Path,
) -> None:
    case = _fixture_case("python_library")
    target = tmp_path / "nested-tests"
    shutil.copytree(FIXTURES / case.name, target)
    nested_test = target / "quality" / "regression" / "math_test.py"
    nested_test.parent.mkdir(parents=True)
    (target / case.test_path).replace(nested_test)
    pyproject = target / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            'testpaths = ["tests"]', 'testpaths = ["quality/regression"]'
        )
    )
    baseline = _tree_bytes(target)
    policy = ValidationPolicy()
    assert policy.commands(target)[0].required is True
    executor = LocalExecutor(
        allowed={command.name: command.argv for command in policy.commands(target)}
    )
    validator = RecordingValidator(ValidationPipeline(executor, policy))
    patch_applier = CountingPatchApplier(target)
    workflow = _workflow_for(
        target,
        tmp_path / "runs",
        case,
        outputs=[
            *_scripted_outputs(case)[:2],
            _wrong_patch_output(case, "return lower", "First failed candidate"),
            _wrong_patch_output(case, "return value", "Second failed candidate"),
            _wrong_patch_output(case, "return upper - 1", "Third failed candidate"),
        ],
        approver=FakeApprover([Decision.APPROVED, Decision.APPROVED]),
        validator=validator,
        patch_applier=patch_applier,
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.selected_candidate_id is None
    assert len(workflow.candidate_evidence) == 3
    assert all(not evidence.eligible for evidence in workflow.candidate_evidence)
    assert all("pytest" in evidence.required_failures for evidence in workflow.candidate_evidence)
    assert _tree_bytes(target) == baseline
