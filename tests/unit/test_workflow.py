from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from repogent.agents import RoleSet
from repogent.approvals import FakeApprover
from repogent.artifacts import ArtifactStore
from repogent.candidates import CandidateEvaluator, CandidatePolicy, CandidateSelector
from repogent.domain import (
    ApprovalKind,
    Budget,
    CandidateEvidence,
    CandidateRecord,
    CheckoutState,
    CheckResult,
    CheckStatus,
    Decision,
    EventKind,
    FinalValidationStatus,
    ProviderCallEvidence,
    ProviderCallStatus,
    ProviderUsage,
    RiskLevel,
    RunManifest,
    RunStage,
    RunStatus,
    ValidationReport,
)
from repogent.events import EventSink
from repogent.localization import PythonLocalizer
from repogent.patching import PatchApplier, PatchPolicy
from repogent.provider_context import MAX_PROVIDER_PAYLOAD_CHARS
from repogent.providers import ProviderError, ProviderResult, ScriptedProvider
from repogent.repository import RepositoryInspector
from repogent.symbols import PythonSymbolGraphBuilder
from repogent.workflow import BudgetExceeded, IllegalTransition, Workflow, transition

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
    "diff": (
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
        " def value():\n-    return 1\n+    return 2\n"
    ),
    "acceptance_criteria_addressed": ["tests pass"],
    "focused_tests": ["pytest"],
}
ALTERNATIVE_PATCH_OUTPUT = {
    **VALID_PATCH_OUTPUT,
    "summary": "Change value alternatively",
    "diff": (
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
        " def value():\n-    return 1\n+    return 3\n"
    ),
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


class SequenceValidator:
    def __init__(self, statuses: list[CheckStatus]) -> None:
        self.statuses = statuses

    def run(
        self, root: Path, *, timeout_seconds: float | None = None
    ) -> ValidationReport:
        del root, timeout_seconds
        status = self.statuses.pop(0)
        return ValidationReport(
            checks=[
                CheckResult(
                    name="pytest",
                    argv=["pytest"],
                    status=status,
                    exit_code=0 if status is CheckStatus.PASSED else 1,
                )
            ]
        )


class FailingEventStore:
    def __init__(self) -> None:
        self.calls = 0

    def emit(self, event: object) -> None:
        del event
        self.calls += 1
        raise OSError("event evidence unavailable")


def make_phase2_workflow(
    tmp_path: Path,
    *,
    outputs: list[dict[str, object]],
    validation_statuses: list[CheckStatus],
    candidate_policy: CandidatePolicy | None = None,
    events: EventSink | None = None,
    budget: Budget | None = None,
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
        manifest=RunManifest(run_id="run-1", request="change value", events_file="events.jsonl"),
        roles=RoleSet.from_provider(ScriptedProvider(outputs)),
        approver=FakeApprover([Decision.APPROVED] * 4),
        patch_policy=patch_policy,
        patch_applier=patch_applier,
        validator=validator,
        artifacts=store,
        inspector=RepositoryInspector(),
        symbol_builder=PythonSymbolGraphBuilder(),
        localizer=PythonLocalizer(),
        candidate_evaluator=CandidateEvaluator(patch_policy, patch_applier, validator),
        candidate_policy=candidate_policy or CandidatePolicy(),
        candidate_selector=CandidateSelector(),
        events=events or store.event_store(),
        budget=budget or Budget(),
    )


def _events(workflow: Workflow) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (workflow.artifacts.root / "events.jsonl").read_text().splitlines()
    ]


def test_illegal_transition_is_rejected() -> None:
    with pytest.raises(IllegalTransition):
        transition(RunStage.CREATED, RunStage.PATCH_APPLIED)


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
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 2\n"
    assert [event["sequence"] for event in _events(workflow)] == list(
        range(1, len(_events(workflow)) + 1)
    )


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
        validation_statuses=[CheckStatus.FAILED, CheckStatus.PASSED, CheckStatus.PASSED],
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.COMPLETED
    assert manifest.candidate_ids == ["candidate-1", "candidate-2"]
    repair_input = json.loads(
        next(workflow.artifacts.root.glob("candidate-input-002.txt")).read_text()
    )
    assert repair_input["previous_failure"]["candidate_id"] == "candidate-1"
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 2\n"


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
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 1\n"
    report = (workflow.artifacts.root / "report.md").read_text()
    assert "## Candidate comparison" in report
    assert "candidate-1" in report
    assert "candidate-2" in report
    assert "## Selection" in report


def test_candidate_evaluations_restore_baseline_before_approval(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )
    initial = (workflow.root / "app.py").read_text()
    original_decide = workflow.approver.decide

    def decide(kind: ApprovalKind, artifact: object) -> object:
        if kind is ApprovalKind.PATCH:
            assert (workflow.root / "app.py").read_text() == initial
        return original_decide(kind, artifact)  # type: ignore[arg-type]

    workflow.approver.decide = decide  # type: ignore[method-assign]
    manifest = workflow.run()
    assert manifest.status is RunStatus.COMPLETED


def test_failed_candidate_restoration_stops_before_another_provider_or_approval(
    tmp_path: Path,
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[],
    )

    def unrecovered_evaluation(*_args: object, **_kwargs: object) -> CandidateEvidence:
        return CandidateEvidence(
            candidate_id="candidate-1",
            validation=ValidationReport(
                checks=[CheckResult(name="restoration", argv=[], status=CheckStatus.FAILED)]
            ),
            acceptance_criteria_coverage=0,
            risk_level=RiskLevel.LOW,
            changed_files=1,
            changed_lines=2,
            duration_seconds=0,
            required_failures=["restoration"],
            restored_to_baseline=False,
        )

    evaluator = workflow.candidate_evaluator
    assert evaluator is not None
    evaluator.evaluate = unrecovered_evaluation  # type: ignore[method-assign]

    manifest = workflow.run()

    provider = workflow.roles.implementation.provider
    assert isinstance(provider, ScriptedProvider)
    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "candidate evaluation did not restore repository baseline"
    assert len(provider.calls) == 3
    assert [record.kind for record in workflow.approver.records] == [
        ApprovalKind.REQUIREMENTS,
        ApprovalKind.PLAN,
    ]


def test_workflow_rechecks_complete_baseline_before_patch_approval(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED],
    )
    evaluator = workflow.candidate_evaluator
    assert evaluator is not None
    original_evaluate = evaluator.evaluate

    def evaluate_then_mutate(*args: object, **kwargs: object) -> CandidateEvidence:
        evidence = original_evaluate(*args, **kwargs)  # type: ignore[arg-type]
        (workflow.root / "unapproved.py").write_text("side_effect = True\n")
        return evidence

    evaluator.evaluate = evaluate_then_mutate  # type: ignore[method-assign]

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "repository baseline changed before approval"
    assert (workflow.root / "unapproved.py").read_text() == "side_effect = True\n"
    assert [record.kind for record in workflow.approver.records] == [
        ApprovalKind.REQUIREMENTS,
        ApprovalKind.PLAN,
    ]


def test_outer_baseline_capture_honors_workflow_deadline_before_candidate_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT],
        validation_statuses=[],
        budget=Budget(timeout_seconds=1),
    )
    evaluator = workflow.candidate_evaluator
    assert evaluator is not None
    original_capture = evaluator.capture_baseline

    def expired_capture(root: Path, *, deadline: float | None = None) -> object:
        clock.now = 2.0
        return original_capture(root, deadline=deadline)

    monkeypatch.setattr("repogent.workflow.time.monotonic", clock.monotonic)
    evaluator.capture_baseline = expired_capture  # type: ignore[method-assign]

    manifest = workflow.run()

    provider = workflow.roles.implementation.provider
    assert isinstance(provider, ScriptedProvider)
    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "candidate evaluation timeout exceeded"
    assert len(provider.calls) == 2
    assert [record.kind for record in workflow.approver.records] == [
        ApprovalKind.REQUIREMENTS,
        ApprovalKind.PLAN,
    ]


def test_patch_approval_drift_stops_before_application_and_preserves_user_change(
    tmp_path: Path,
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED],
    )
    (workflow.root / "other.py").write_text("original\n")
    original_decide = workflow.approver.decide

    def decide(kind: ApprovalKind, artifact: object) -> object:
        record = original_decide(kind, artifact)  # type: ignore[arg-type]
        if kind is ApprovalKind.PATCH:
            (workflow.root / "other.py").write_text("concurrent edit\n")
        return record

    workflow.approver.decide = decide  # type: ignore[method-assign]

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "repository baseline changed after approval"
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 1\n"
    assert (workflow.root / "other.py").read_text() == "concurrent edit\n"


def test_candidate_and_final_validation_run_only_in_disposable_roots(tmp_path: Path) -> None:
    class MutatingValidator:
        def __init__(self) -> None:
            self.roots: list[Path] = []

        def run(
            self, root: Path, *, timeout_seconds: float | None = None
        ) -> ValidationReport:
            del timeout_seconds
            self.roots.append(root)
            (root / "other.py").write_text("validator side effect\n")
            return ValidationReport(
                checks=[CheckResult(name="pytest", argv=["pytest"], status=CheckStatus.PASSED)]
            )

    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[],
    )
    (workflow.root / "other.py").write_text("original\n")
    validator = MutatingValidator()
    workflow.validator = validator
    workflow.candidate_evaluator = CandidateEvaluator(
        workflow.patch_policy, workflow.patch_applier, validator
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.COMPLETED
    assert len(validator.roots) == 2
    assert all(root != workflow.root for root in validator.roots)
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 2\n"
    assert (workflow.root / "other.py").read_text() == "original\n"


def test_patch_approval_contains_all_candidate_proposals_and_evidence(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[
            REQUIREMENTS_OUTPUT,
            PLAN_OUTPUT,
            INVALID_PATCH_OUTPUT,
            VALID_PATCH_OUTPUT,
            QA_OUTPUT,
        ],
        validation_statuses=[CheckStatus.FAILED, CheckStatus.PASSED, CheckStatus.PASSED],
    )
    original_decide = workflow.approver.decide
    approval_payload: dict[str, object] = {}

    def decide(kind: ApprovalKind, artifact: object) -> object:
        if kind is ApprovalKind.PATCH:
            assert isinstance(artifact, str)
            approval_payload.update(json.loads(artifact))
        return original_decide(kind, artifact)  # type: ignore[arg-type]

    workflow.approver.decide = decide  # type: ignore[method-assign]
    manifest = workflow.run()

    assert manifest.status is RunStatus.COMPLETED
    comparisons = approval_payload["candidates"]
    assert isinstance(comparisons, list)
    assert [item["candidate"]["candidate_id"] for item in comparisons] == [  # type: ignore[index]
        "candidate-1",
        "candidate-2",
    ]
    assert all("evidence" in item for item in comparisons if isinstance(item, dict))


def test_workflow_sets_default_events_filename(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[],
        validation_statuses=[],
    )
    workflow.manifest = workflow.manifest.model_copy(update={"events_file": None})

    workflow.__post_init__()

    assert workflow.manifest.events_file == "events.jsonl"


def test_event_store_failure_is_terminalized_without_recursive_emit(tmp_path: Path) -> None:
    events = FailingEventStore()
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[],
        validation_statuses=[],
        events=events,
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "event evidence unavailable"
    assert manifest.stage is RunStage.FINISHED
    assert events.calls == 1


def test_account_persists_usage_before_enforcing_token_and_cost_budget(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[],
        validation_statuses=[],
        budget=Budget(max_tokens=1, max_cost_usd=Decimal("0.01")),
    )

    with pytest.raises(BudgetExceeded, match="token budget"):
        workflow.account(
            ProviderUsage(
                model="test",
                input_tokens=1,
                output_tokens=1,
                estimated_cost_usd=Decimal("0.02"),
            )
        )

    persisted = json.loads((workflow.artifacts.root / "run.json").read_text())
    assert workflow.manifest.token_usage == 2
    assert persisted["token_usage"] == 2


def test_final_validation_evidence_mismatch_requires_human_intervention(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.FAILED],
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "changed validation evidence"
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 2\n"


def test_terminal_event_is_written_for_completed_run(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )
    workflow.run()
    assert _events(workflow)[-1]["kind"] == EventKind.TERMINAL.value
    assert _events(workflow)[-1]["stage"] == RunStage.FINISHED.value


def test_validation_events_include_concise_check_counts(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )

    workflow.run()

    validation_events = [event for event in _events(workflow) if event["kind"] == "validation"]
    assert validation_events[0]["data"] == {
        "candidate_id": "candidate-1",
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "cost_usd": "0",
        "restored_to_baseline": True,
    }
    assert validation_events[1]["data"] == {"passed": 1, "failed": 0, "skipped": 0}


def test_terminal_report_retains_candidate_evidence_after_unrecovered_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[],
    )

    def unrecovered_evaluation(
        _root: Path,
        candidate: CandidateRecord,
        _criteria: list[str],
        _timeout_seconds: float,
    ) -> CandidateEvidence:
        return CandidateEvidence(
            candidate_id=candidate.candidate_id,
            validation=ValidationReport(
                checks=[
                    CheckResult(
                        name="repository-drift",
                        argv=[],
                        status=CheckStatus.FAILED,
                        reason="evaluation copy was not restored",
                    )
                ]
            ),
            acceptance_criteria_coverage=0,
            risk_level=RiskLevel.HIGH,
            changed_files=1,
            changed_lines=1,
            duration_seconds=0,
            required_failures=["repository-drift"],
            restored_to_baseline=False,
        )

    assert workflow.candidate_evaluator is not None
    monkeypatch.setattr(workflow.candidate_evaluator, "evaluate", unrecovered_evaluation)

    manifest = workflow.run()
    report = (workflow.artifacts.root / "report.md").read_text()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "candidate evaluation did not restore repository baseline"
    assert "candidate-1" in report
    assert "repository-drift" in report
    assert "not restored" in report


def test_terminal_report_retains_unevaluated_candidate_after_evaluator_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[],
    )

    def evaluation_error(
        _root: Path,
        _candidate: CandidateRecord,
        _criteria: list[str],
        _timeout_seconds: float,
    ) -> CandidateEvidence:
        raise RuntimeError("candidate evaluator unavailable")

    assert workflow.candidate_evaluator is not None
    monkeypatch.setattr(workflow.candidate_evaluator, "evaluate", evaluation_error)

    manifest = workflow.run()
    report = (workflow.artifacts.root / "report.md").read_text()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "candidate evaluator unavailable"
    assert "candidate-1" in report
    assert "not evaluated" in report
    assert "evaluation interrupted" in report


def test_terminal_report_pairing_ignores_duplicate_and_unknown_evidence(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )

    workflow.run()

    known_evidence = workflow.candidate_evidence[0]
    workflow.candidate_evidence.extend(
        [
            known_evidence,
            known_evidence.model_copy(update={"candidate_id": "candidate-2"}),
        ]
    )

    pairs = workflow._report_candidates()

    assert pairs == ((workflow.candidates[0], known_evidence),)


def test_final_manifest_persistence_failure_downgrades_and_persists_human_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )
    original_update = workflow.artifacts.update_manifest
    failed = False

    def fail_once_for_finished(manifest: RunManifest) -> Path:
        nonlocal failed
        if manifest.stage is RunStage.FINISHED and not failed:
            failed = True
            raise OSError("final manifest write failed")
        return original_update(manifest)

    monkeypatch.setattr(workflow.artifacts, "update_manifest", fail_once_for_finished)

    manifest = workflow.run()
    persisted = json.loads((workflow.artifacts.root / "run.json").read_text())

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "final manifest write failed"
    assert persisted["status"] == RunStatus.HUMAN_INTERVENTION_REQUIRED.value
    terminal = _events(workflow)[-1]
    assert terminal["kind"] == EventKind.TERMINAL.value
    assert terminal["data"]["status"] == RunStatus.HUMAN_INTERVENTION_REQUIRED.value


def test_report_persistence_failure_downgrades_before_terminal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )
    original_write_final = workflow.artifacts.write_final
    failed = False

    def fail_once_report(filename: str, content: str) -> Path:
        nonlocal failed
        if filename == "report.md" and not failed:
            failed = True
            raise OSError("final report write failed")
        return original_write_final(filename, content)

    monkeypatch.setattr(workflow.artifacts, "write_final", fail_once_report)

    manifest = workflow.run()
    persisted = json.loads((workflow.artifacts.root / "run.json").read_text())
    terminal_events = [event for event in _events(workflow) if event["kind"] == "terminal"]

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "final report write failed"
    assert persisted["status"] == RunStatus.HUMAN_INTERVENTION_REQUIRED.value
    assert (workflow.artifacts.root / "report.md").exists()
    assert len(terminal_events) == 1
    assert terminal_events[0]["data"]["status"] == RunStatus.HUMAN_INTERVENTION_REQUIRED.value


@pytest.mark.parametrize(
    ("cross_at", "artifact_name"),
    [(1, "requirements"), (2, "plan"), (3, "candidate"), (4, "qa-review")],
)
def test_generated_role_output_is_persisted_before_budget_enforcement(
    tmp_path: Path, cross_at: int, artifact_name: str
) -> None:
    class MeteredProvider(ScriptedProvider):
        def generate(self, **kwargs: object) -> ProviderResult[object]:  # type: ignore[override,type-var]
            result = super().generate(**kwargs)  # type: ignore[arg-type]
            usage = ProviderUsage(
                model="metered",
                output_tokens=10 if len(self.calls) == cross_at else 0,
            )
            return ProviderResult(output=result.output, usage=usage)

    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
        budget=Budget(max_tokens=5),
    )
    provider = MeteredProvider(
        [REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT]
    )
    workflow.roles = RoleSet.from_provider(provider)

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "token budget exceeded"
    assert artifact_name in manifest.generated_but_not_consumed
    assert list(workflow.artifacts.root.glob(f"{artifact_name}-*.json"))
    assert f"Generated but not consumed: {artifact_name}" in (
        workflow.artifacts.root / "report.md"
    ).read_text()
    assert len(provider.calls) == cross_at
    if cross_at == 3:
        assert manifest.candidate_ids == ["candidate-1"]
        assert workflow.candidate_evidence == []
        assert (workflow.root / "app.py").read_text() == "def value():\n    return 1\n"
    if cross_at == 4:
        assert manifest.selected_patch_applied is True
        assert manifest.final_validation_status is FinalValidationStatus.PASSED
        assert (workflow.root / "app.py").read_text() == "def value():\n    return 2\n"


def test_workflow_provider_payloads_are_bounded_and_requirements_exclude_contents(
    tmp_path: Path,
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )

    workflow.run()

    provider = workflow.roles.requirements.provider
    assert isinstance(provider, ScriptedProvider)
    assert all(
        len(json.dumps(call["payload"], sort_keys=True)) <= MAX_PROVIDER_PAYLOAD_CHARS
        for call in provider.calls
    )
    inventory = provider.calls[0]["payload"]["repository_inventory"]
    assert isinstance(inventory, dict)
    assert all("text" not in file for file in inventory["files"])


def test_keyboard_interrupt_is_durably_terminalized_as_cancellation(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(tmp_path, outputs=[], validation_statuses=[])

    def interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    workflow.roles.requirements.run = interrupt  # type: ignore[method-assign]

    manifest = workflow.run()

    assert manifest.status is RunStatus.CANCELLED
    assert manifest.reason == "workflow interrupted by user"
    assert json.loads((workflow.artifacts.root / "run.json").read_text())["status"] == "cancelled"
    assert (workflow.artifacts.root / "report.md").exists()
    assert _events(workflow)[-1]["kind"] == EventKind.TERMINAL.value


def test_requested_cancellation_terminalizes_as_cancelled(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(tmp_path, outputs=[], validation_statuses=[])
    workflow.cancel_requested = lambda: True

    manifest = workflow.run()

    assert manifest.status is RunStatus.CANCELLED
    assert manifest.reason == "workflow cancellation requested"
    assert manifest.checkout_state is CheckoutState.NOT_APPLIED


def test_cancellation_after_requirements_approval_stops_before_candidate_generation(
    tmp_path: Path,
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT],
        validation_statuses=[],
    )
    workflow.cancel_requested = lambda: bool(workflow.approver.records)

    manifest = workflow.run()

    provider = workflow.roles.requirements.provider
    assert isinstance(provider, ScriptedProvider)
    assert manifest.status is RunStatus.CANCELLED
    assert manifest.reason == "workflow cancellation requested"
    assert [record.kind for record in workflow.approver.records] == [ApprovalKind.REQUIREMENTS]
    assert len(provider.calls) == 1
    assert workflow.candidates == []


def test_cancellation_after_durable_apply_reports_applied_checkout(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED],
    )
    workflow.cancel_requested = (
        lambda: workflow.manifest.checkout_state is CheckoutState.APPLIED
    )

    manifest = workflow.run()
    report = (workflow.artifacts.root / "report.md").read_text()

    assert manifest.status is RunStatus.CANCELLED
    assert manifest.checkout_state is CheckoutState.APPLIED
    assert manifest.applied_paths == ["app.py"]
    assert manifest.final_validation_status is FinalValidationStatus.INTERRUPTED
    assert manifest.recovery_guidance is not None
    assert "run every required validation command" in manifest.recovery_guidance
    assert "revert the approved patch manually" in manifest.recovery_guidance
    assert "rolled back" not in manifest.recovery_guidance
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 2\n"
    assert "Real checkout patch: remains applied" in report
    assert "Real checkout patch: not applied" not in report


def test_final_validation_failure_reports_real_patch_as_applied(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.FAILED],
    )

    manifest = workflow.run()
    report = (workflow.artifacts.root / "report.md").read_text()

    assert manifest.selected_patch_applied is True
    assert manifest.applied_paths == ["app.py"]
    assert manifest.final_validation_status is FinalValidationStatus.FAILED
    assert "Real checkout patch: remains applied" in report
    assert "run the required validation commands" in report


def test_post_apply_artifact_failure_preserves_truthful_recovery_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED],
    )
    original_write = workflow.artifacts.write_model

    def fail_patch_artifact(name: str, model: object) -> Path:
        if name == "patch-applied":
            raise OSError("patch artifact unavailable")
        return original_write(name, model)  # type: ignore[arg-type]

    monkeypatch.setattr(workflow.artifacts, "write_model", fail_patch_artifact)

    manifest = workflow.run()

    assert manifest.reason == "patch artifact unavailable"
    assert manifest.selected_patch_applied is True
    assert manifest.applied_paths == ["app.py"]
    assert manifest.final_validation_status is FinalValidationStatus.INTERRUPTED
    assert "Real checkout patch: remains applied" in (
        workflow.artifacts.root / "report.md"
    ).read_text()


def test_post_apply_qa_interrupt_keeps_applied_patch_and_final_validation_state(
    tmp_path: Path,
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )

    def interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    workflow.roles.qa.run = interrupt  # type: ignore[method-assign]

    manifest = workflow.run()

    assert manifest.status is RunStatus.CANCELLED
    assert manifest.selected_patch_applied is True
    assert manifest.final_validation_status is FinalValidationStatus.PASSED
    assert manifest.recovery_guidance is not None


def test_post_apply_qa_provider_failure_keeps_applied_patch_and_guidance(
    tmp_path: Path,
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )

    def fail_qa(*_args: object, **_kwargs: object) -> object:
        raise ProviderError("QA provider unavailable")

    workflow.roles.qa.run = fail_qa  # type: ignore[method-assign]

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "QA provider unavailable"
    assert manifest.selected_patch_applied is True
    assert manifest.final_validation_status is FinalValidationStatus.PASSED
    assert "Real checkout patch: remains applied" in (
        workflow.artifacts.root / "report.md"
    ).read_text()


def test_non_retryable_provider_failure_writes_evidence_and_requires_human(
    tmp_path: Path,
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[],
        validation_statuses=[],
    )
    evidence = ProviderCallEvidence(
        provider="codex-cli",
        model="default",
        role="requirements",
        invocation=1,
        status=ProviderCallStatus.AUTHENTICATION_FAILED,
        structured_output_valid=False,
    )
    error = ProviderError(
        "Codex CLI is not authenticated", retryable=False, evidence=evidence
    )

    class FailingProvider:
        def generate(self, **_kwargs: object) -> object:
            raise error

    workflow.roles = RoleSet.from_provider(FailingProvider())  # type: ignore[arg-type]

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    failure = json.loads((workflow.artifacts.root / "provider-failure-001.json").read_text())
    assert failure == evidence.model_dump(mode="json")


def test_retryable_provider_failure_writes_final_attempt_evidence(
    tmp_path: Path,
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[],
        validation_statuses=[],
    )

    class FailingProvider:
        calls = 0

        def generate(self, **_kwargs: object) -> object:
            self.calls += 1
            evidence = ProviderCallEvidence(
                provider="codex-cli",
                model="default",
                role="requirements",
                invocation=self.calls,
                status=ProviderCallStatus.EXECUTION_FAILED,
                structured_output_valid=False,
            )
            raise ProviderError("Codex CLI execution failed", evidence=evidence)

    provider = FailingProvider()
    workflow.roles = RoleSet.from_provider(provider)  # type: ignore[arg-type]

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert provider.calls == 2
    failure = json.loads((workflow.artifacts.root / "provider-failure-001.json").read_text())
    assert failure["invocation"] == 2


def test_provider_failure_evidence_persistence_error_is_terminal_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[],
        validation_statuses=[],
    )
    evidence = ProviderCallEvidence(
        provider="codex-cli",
        model="default",
        role="requirements",
        invocation=1,
        status=ProviderCallStatus.AUTHENTICATION_FAILED,
        structured_output_valid=False,
    )

    class FailingProvider:
        def generate(self, **_kwargs: object) -> object:
            raise ProviderError(
                "Codex CLI is not authenticated", retryable=False, evidence=evidence
            )

    original_write_model = workflow.artifacts.write_model

    def fail_provider_failure(name: str, model: object) -> Path:
        if name == "provider-failure":
            raise OSError("provider failure evidence unavailable")
        return original_write_model(name, model)  # type: ignore[arg-type]

    monkeypatch.setattr(workflow.artifacts, "write_model", fail_provider_failure)
    workflow.roles = RoleSet.from_provider(FailingProvider())  # type: ignore[arg-type]

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "provider failure evidence unavailable"


def test_post_apply_event_failure_keeps_real_checkout_state_in_manifest_and_report(
    tmp_path: Path,
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )
    delegate = workflow.artifacts.event_store()

    class FailFinalValidationEvent:
        def emit(self, event: object) -> None:
            if getattr(event, "message", "") == "final validation completed":
                raise OSError("final validation event unavailable")
            delegate.emit(event)  # type: ignore[arg-type]

    workflow.events = FailFinalValidationEvent()

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "final validation event unavailable"
    assert manifest.selected_patch_applied is True
    assert manifest.final_validation_status is FinalValidationStatus.PASSED
    persisted = json.loads((workflow.artifacts.root / "run.json").read_text())
    assert persisted["selected_patch_applied"] is True
    assert "Real checkout patch: remains applied" in (
        workflow.artifacts.root / "report.md"
    ).read_text()


def test_terminal_event_keyboard_interrupt_is_durably_recorded_without_recursion(
    tmp_path: Path,
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )
    delegate = workflow.artifacts.event_store()

    class InterruptTerminalEvent:
        def emit(self, event: object) -> None:
            if getattr(event, "kind", None) is EventKind.TERMINAL:
                raise KeyboardInterrupt
            delegate.emit(event)  # type: ignore[arg-type]

    workflow.events = InterruptTerminalEvent()

    manifest = workflow.run()

    assert manifest.status is RunStatus.CANCELLED
    assert manifest.reason == "workflow interrupted during terminalization"
    persisted = json.loads((workflow.artifacts.root / "run.json").read_text())
    assert persisted["status"] == RunStatus.CANCELLED.value
    assert (workflow.artifacts.root / "report.md").exists()


def test_real_apply_interrupt_restores_checkout_and_cancels_as_not_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED],
    )
    original_apply = PatchApplier._git_apply

    def interrupt_real_apply(root: Path, diff: str, *, check: bool) -> None:
        original_apply(root, diff, check=check)
        if root.resolve() == workflow.root.resolve() and not check:
            raise KeyboardInterrupt

    monkeypatch.setattr(PatchApplier, "_git_apply", staticmethod(interrupt_real_apply))

    manifest = workflow.run()

    assert manifest.status is RunStatus.CANCELLED
    assert manifest.checkout_state is CheckoutState.NOT_APPLIED
    assert manifest.selected_patch_applied is False
    assert manifest.applied_paths == []
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 1\n"
    assert "Real checkout patch: not applied" in (
        workflow.artifacts.root / "report.md"
    ).read_text()


def test_real_apply_interrupt_with_failed_restore_reports_recovery_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED],
    )
    original_apply = PatchApplier._git_apply
    original_restore = workflow.patch_applier.restore

    def interrupt_real_apply(root: Path, diff: str, *, check: bool) -> None:
        original_apply(root, diff, check=check)
        if root.resolve() == workflow.root.resolve() and not check:
            raise KeyboardInterrupt

    def fail_real_restore(
        root: Path, snapshots: object, missing_directories: object
    ) -> None:
        if root.resolve() == workflow.root.resolve():
            raise RuntimeError("real checkout restore failed")
        original_restore(root, snapshots, missing_directories)  # type: ignore[arg-type]

    monkeypatch.setattr(PatchApplier, "_git_apply", staticmethod(interrupt_real_apply))
    monkeypatch.setattr(workflow.patch_applier, "restore", fail_real_restore)

    manifest = workflow.run()
    report = (workflow.artifacts.root / "report.md").read_text()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.checkout_state is CheckoutState.RECOVERY_UNKNOWN
    assert manifest.selected_patch_applied is False
    assert manifest.applied_paths == ["app.py"]
    assert manifest.final_validation_status is FinalValidationStatus.INTERRUPTED
    assert manifest.recovery_guidance is not None
    assert "manually inspect and restore app.py" in manifest.recovery_guidance
    assert "Real checkout patch: recovery unknown" in report
    assert "Real checkout patch: not applied" not in report
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 2\n"


def test_interrupt_after_real_apply_keeps_durable_write_ahead_recovery_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED],
    )
    original_apply = workflow.patch_applier.apply

    def apply_then_interrupt(root: Path, patch: object) -> None:
        original_apply(root, patch)  # type: ignore[arg-type]
        if root.resolve() == workflow.root.resolve():
            raise KeyboardInterrupt

    monkeypatch.setattr(workflow.patch_applier, "apply", apply_then_interrupt)

    manifest = workflow.run()
    persisted = json.loads((workflow.artifacts.root / "run.json").read_text())
    report = (workflow.artifacts.root / "report.md").read_text()

    assert manifest.status is RunStatus.CANCELLED
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 2\n"
    assert manifest.checkout_state is CheckoutState.RECOVERY_UNKNOWN
    assert manifest.applied_paths == ["app.py"]
    assert manifest.recovery_guidance is not None
    assert persisted["checkout_state"] == CheckoutState.RECOVERY_UNKNOWN.value
    assert persisted["applied_paths"] == ["app.py"]
    assert "Real checkout patch: recovery unknown" in report
    assert "Real checkout patch: not applied" not in report


def test_failed_write_ahead_intent_persistence_prevents_real_checkout_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED],
    )
    original_apply = workflow.patch_applier.apply
    original_update = workflow.artifacts.update_manifest
    real_apply_called = False

    def record_real_apply(root: Path, patch: object) -> None:
        nonlocal real_apply_called
        if root.resolve() == workflow.root.resolve():
            real_apply_called = True
        original_apply(root, patch)  # type: ignore[arg-type]

    def fail_write_ahead_intent(manifest: object) -> Path:
        if getattr(manifest, "checkout_state", None) is CheckoutState.RECOVERY_UNKNOWN:
            raise OSError("write-ahead intent unavailable")
        return original_update(manifest)  # type: ignore[arg-type]

    monkeypatch.setattr(workflow.patch_applier, "apply", record_real_apply)
    monkeypatch.setattr(workflow.artifacts, "update_manifest", fail_write_ahead_intent)

    manifest = workflow.run()
    persisted = json.loads((workflow.artifacts.root / "run.json").read_text())

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "write-ahead intent unavailable"
    assert real_apply_called is False
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 1\n"
    assert manifest.checkout_state is CheckoutState.NOT_APPLIED
    assert persisted["checkout_state"] == CheckoutState.NOT_APPLIED.value


def test_failed_applied_state_persistence_retains_durable_recovery_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT],
        validation_statuses=[CheckStatus.PASSED],
    )
    original_update = workflow.artifacts.update_manifest
    failed = False

    def fail_applied_state_once(manifest: RunManifest) -> Path:
        nonlocal failed
        if manifest.checkout_state is CheckoutState.APPLIED and not failed:
            failed = True
            raise OSError("applied state unavailable")
        return original_update(manifest)

    monkeypatch.setattr(workflow.artifacts, "update_manifest", fail_applied_state_once)

    manifest = workflow.run()
    persisted = json.loads((workflow.artifacts.root / "run.json").read_text())

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.reason == "applied state unavailable"
    assert (workflow.root / "app.py").read_text() == "def value():\n    return 2\n"
    assert manifest.checkout_state is CheckoutState.RECOVERY_UNKNOWN
    assert persisted["checkout_state"] == CheckoutState.RECOVERY_UNKNOWN.value
    assert persisted["applied_paths"] == ["app.py"]


def test_successful_real_apply_persists_applied_checkout_state(tmp_path: Path) -> None:
    workflow = make_phase2_workflow(
        tmp_path,
        outputs=[REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT, QA_OUTPUT],
        validation_statuses=[CheckStatus.PASSED, CheckStatus.PASSED],
    )

    manifest = workflow.run()
    persisted = json.loads((workflow.artifacts.root / "run.json").read_text())

    assert manifest.status is RunStatus.COMPLETED
    assert manifest.checkout_state is CheckoutState.APPLIED
    assert manifest.applied_paths == ["app.py"]
    assert persisted["checkout_state"] == CheckoutState.APPLIED.value
    assert persisted["applied_paths"] == ["app.py"]
