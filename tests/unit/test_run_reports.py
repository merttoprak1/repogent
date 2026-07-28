import pytest
from pydantic import ValidationError

from repogent.domain import (
    CheckoutState,
    CheckResult,
    CheckStatus,
    ExecutionMode,
    FinalValidationStatus,
    IsolationLevel,
    RunManifest,
    RunStatus,
    ValidationReport,
    ValidationTarget,
    ValidationTargetKind,
    VerificationStatus,
    WorkflowKind,
    WorkflowOutcome,
)
from repogent.run_reports import (
    PersistentRunReport,
    VerifiedChangeResult,
    build_persistent_report,
)


def _applied_manifest() -> RunManifest:
    return RunManifest(
        run_id="run-1",
        request="apply the verified change",
        kind=WorkflowKind.VERIFIED_CHANGE,
        outcome=WorkflowOutcome.APPLIED,
        status=RunStatus.COMPLETED,
        execution_mode=ExecutionMode.LOCAL,
        isolation_level=IsolationLevel.REDUCED_ISOLATION,
        verification_status=VerificationStatus.PASSED,
        evaluated_target=ValidationTarget(
            kind=ValidationTargetKind.PATCH,
            digest="a" * 64,
        ),
        selected_candidate_id="candidate-1",
        checkout_state=CheckoutState.APPLIED,
        applied_paths=["src/app.py"],
        final_validation_status=FinalValidationStatus.PASSED,
    )


def _validation_report() -> ValidationReport:
    return ValidationReport(
        checks=[
            CheckResult(
                name="pytest",
                argv=["pytest"],
                status=CheckStatus.PASSED,
                required=True,
            ),
            CheckResult(
                name="ruff",
                argv=["ruff", "check"],
                status=CheckStatus.SKIPPED,
                required=False,
            ),
        ]
    )


def test_verified_change_report_states_checkout_fact_and_trust() -> None:
    report = build_persistent_report(
        _applied_manifest(),
        _validation_report(),
        evidence_path="/bounded/evidence/run-1",
    )

    assert report.checkout_changed is True
    assert report.trust_label.value == "REDUCED ISOLATION"
    assert report.checks.required == ["pytest"]
    assert report.checks.passed == ["pytest"]
    assert report.checks.skipped == ["ruff"]
    assert isinstance(report.result, VerifiedChangeResult)
    assert report.result.applied_paths == ["src/app.py"]


def test_report_rejects_result_for_wrong_kind() -> None:
    result = VerifiedChangeResult(
        selected_candidate_id=None,
        final_validation_status=FinalValidationStatus.NOT_STARTED,
    )

    with pytest.raises(ValidationError, match="result kind"):
        PersistentRunReport(
            run_id="run-1",
            kind=WorkflowKind.PATCH_REVIEW,
            status=RunStatus.COMPLETED,
            outcome=WorkflowOutcome.APPROVE,
            evaluated_target=None,
            checkout_changed=False,
            checkout_state=CheckoutState.NOT_APPLIED,
            checks={},
            trust_label="UNVALIDATED",
            evidence_path="/bounded/evidence/run-1",
            result=result,
        )


def test_cancelled_report_preserves_absent_outcome() -> None:
    manifest = RunManifest(
        run_id="run-cancelled",
        request="cancel this run",
        status=RunStatus.CANCELLED,
    )

    report = build_persistent_report(
        manifest,
        None,
        evidence_path="/bounded/evidence/run-cancelled",
    )

    assert report.outcome is None
    assert report.checkout_changed is False
