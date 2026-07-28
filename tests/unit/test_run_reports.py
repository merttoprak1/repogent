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
from repogent.errors import ErrorCode
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
    assert report.errors == []


@pytest.mark.parametrize(
    ("reason", "checkout_state", "final_validation_status", "expected_code"),
    [
        (
            "provider request failed token=sk-proj-1234567890abcdef",
            CheckoutState.NOT_APPLIED,
            FinalValidationStatus.NOT_STARTED,
            ErrorCode.PROVIDER_UNAVAILABLE,
        ),
        (
            "required checks failed",
            CheckoutState.NOT_APPLIED,
            FinalValidationStatus.FAILED,
            ErrorCode.VALIDATION_FAILED,
        ),
        (
            "repository preflight failed",
            CheckoutState.NOT_APPLIED,
            FinalValidationStatus.NOT_STARTED,
            ErrorCode.POLICY,
        ),
        (
            "workflow timeout exceeded",
            CheckoutState.NOT_APPLIED,
            FinalValidationStatus.NOT_STARTED,
            ErrorCode.LIMIT_EXCEEDED,
        ),
        (
            "checkout recovery could not be proved",
            CheckoutState.RECOVERY_UNKNOWN,
            FinalValidationStatus.INTERRUPTED,
            ErrorCode.INTERNAL,
        ),
    ],
)
def test_terminal_failure_derives_a_safe_typed_error(
    reason: str,
    checkout_state: CheckoutState,
    final_validation_status: FinalValidationStatus,
    expected_code: ErrorCode,
) -> None:
    manifest = RunManifest(
        run_id="run-failure",
        request="change",
        status=RunStatus.HUMAN_INTERVENTION_REQUIRED,
        outcome=WorkflowOutcome.HUMAN_INTERVENTION_REQUIRED,
        reason=reason,
        checkout_state=checkout_state,
        final_validation_status=final_validation_status,
    )

    report = build_persistent_report(
        manifest,
        None,
        evidence_path="/bounded/evidence/run-failure",
    )

    assert len(report.errors) == 1
    assert report.errors[0].code is expected_code
    assert report.errors[0].run_id == manifest.run_id
    assert report.errors[0].run_kind is manifest.kind
    assert "sk-proj-1234567890abcdef" not in report.errors[0].message
    assert report.errors[0].remediation is not None


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
    assert report.errors == []
