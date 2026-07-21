from decimal import Decimal

import pytest
from pydantic import ValidationError

from repogent.domain import (
    Budget,
    CandidateEvidence,
    CheckoutState,
    CheckResult,
    CheckStatus,
    FinalValidationStatus,
    ImplementationPlan,
    PlanStep,
    RequirementsSpec,
    RiskLevel,
    RunEvent,
    RunManifest,
    RunStage,
    RunStatus,
    ValidationReport,
)


def test_requirements_reject_empty_objective() -> None:
    with pytest.raises(ValidationError):
        RequirementsSpec(objective="", functional_requirements=[], acceptance_criteria=[])


def test_plan_rejects_unknown_dependency() -> None:
    with pytest.raises(ValidationError, match="unknown dependency"):
        ImplementationPlan(
            files_to_modify=["app/main.py"],
            steps=[PlanStep(id="change", description="Change route", depends_on=["missing"])],
            tests=["pytest"],
        )


def test_validation_report_passes_when_required_checks_pass_and_optional_checks_do_not() -> None:
    report = ValidationReport(
        checks=[
            CheckResult(name="pytest", argv=["python", "-m", "pytest"], status=CheckStatus.PASSED),
            CheckResult(
                name="ruff",
                argv=["ruff", "check", "."],
                status=CheckStatus.SKIPPED,
                required=False,
            ),
        ]
    )
    assert report.passed is True


def test_candidate_evidence_with_failed_required_check_is_ineligible() -> None:
    validation = ValidationReport(
        checks=[
            CheckResult(
                name="pytest",
                argv=["python", "-m", "pytest"],
                status=CheckStatus.FAILED,
            )
        ]
    )

    evidence = CandidateEvidence(
        candidate_id="candidate-1",
        validation=validation,
        acceptance_criteria_coverage=1,
        risk_level=RiskLevel.LOW,
        changed_files=1,
        changed_lines=3,
        duration_seconds=1.5,
        restored_to_baseline=True,
    )

    assert evidence.eligible is False


def test_budget_defaults_to_two_repairs_and_positive_limits() -> None:
    budget = Budget()
    assert budget.max_repairs == 2
    assert budget.max_tokens > 0
    assert budget.max_cost_usd == Decimal("20.00")


def test_manifest_starts_in_created_state() -> None:
    manifest = RunManifest(run_id="run-123", request="Add a health route")
    assert manifest.status is RunStatus.RUNNING
    assert manifest.stage is RunStage.CREATED


def test_manifest_phase_two_fields_round_trip_through_json() -> None:
    manifest = RunManifest(
        run_id="run-123",
        request="Add a health route",
        repository_fingerprint="a" * 64,
        configuration_fingerprint="b" * 64,
        candidate_ids=["candidate-1", "candidate-2"],
        selected_candidate_id="candidate-1",
        events_file="events.jsonl",
        selected_patch_applied=True,
        applied_paths=["app.py"],
        final_validation_status=FinalValidationStatus.FAILED,
        recovery_guidance="Review app.py and revert the approved patch if unwanted.",
        generated_but_not_consumed=["qa-review"],
        checkout_state=CheckoutState.APPLIED,
    )

    restored = RunManifest.model_validate_json(manifest.model_dump_json())

    assert restored == manifest


def test_manifest_recovery_fields_default_for_existing_evidence() -> None:
    manifest = RunManifest.model_validate({"run_id": "old-run", "request": "change"})

    assert manifest.selected_patch_applied is False
    assert manifest.applied_paths == []
    assert manifest.final_validation_status is FinalValidationStatus.NOT_STARTED
    assert manifest.recovery_guidance is None
    assert manifest.generated_but_not_consumed == []
    assert manifest.checkout_state is CheckoutState.NOT_APPLIED

    legacy_applied = RunManifest.model_validate(
        {"run_id": "old-applied", "request": "change", "selected_patch_applied": True}
    )
    assert legacy_applied.checkout_state is CheckoutState.APPLIED


def test_run_event_message_is_limited_to_4096_characters() -> None:
    event = RunEvent(
        run_id="run-123",
        sequence=1,
        kind="stage",
        message="x" * 4096,
    )

    assert len(event.message) == 4096

    with pytest.raises(ValidationError, match="String should have at most 4096 characters"):
        RunEvent(
            run_id="run-123",
            sequence=1,
            kind="stage",
            message="x" * 4097,
        )
