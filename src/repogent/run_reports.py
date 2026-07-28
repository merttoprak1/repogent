from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import Field, model_validator

from repogent.domain import (
    CheckoutState,
    CheckStatus,
    FinalValidationStatus,
    RunManifest,
    RunStatus,
    TrustLabel,
    ValidationReport,
    ValidationTarget,
    VersionedModel,
    WorkflowKind,
    WorkflowOutcome,
    compute_trust_label,
)
from repogent.errors import ErrorCode, ErrorDetail, RetryClass
from repogent.sanitization import redact_text


class CheckSummary(VersionedModel):
    required: list[str] = Field(default_factory=list)
    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class VerifiedChangeResult(VersionedModel):
    kind: Literal[WorkflowKind.VERIFIED_CHANGE] = WorkflowKind.VERIFIED_CHANGE
    selected_candidate_id: str | None
    applied_paths: list[str] = Field(default_factory=list, max_length=20)
    final_validation_status: FinalValidationStatus


CapabilityResult = VerifiedChangeResult


class PersistentRunReport(VersionedModel):
    run_id: str = Field(min_length=1, max_length=256)
    kind: WorkflowKind
    status: RunStatus
    outcome: WorkflowOutcome | None
    evaluated_target: ValidationTarget | None
    checkout_changed: bool
    checkout_state: CheckoutState
    checks: CheckSummary
    trust_label: TrustLabel
    errors: list[ErrorDetail] = Field(default_factory=list, max_length=10)
    evidence_path: str = Field(max_length=4_096)
    result: CapabilityResult

    @model_validator(mode="after")
    def validate_capability_result(self) -> PersistentRunReport:
        if self.result.kind is not self.kind:
            raise ValueError("result kind does not match report kind")
        expected_checkout_changed = self.checkout_state is not CheckoutState.NOT_APPLIED
        if self.checkout_changed is not expected_checkout_changed:
            raise ValueError("checkout_changed does not match durable checkout state")
        return self


def build_persistent_report(
    manifest: RunManifest,
    validation: ValidationReport | None,
    *,
    evidence_path: str,
    errors: Sequence[ErrorDetail] = (),
) -> PersistentRunReport:
    checks = tuple(validation.checks) if validation is not None else ()
    return PersistentRunReport(
        run_id=manifest.run_id,
        kind=manifest.kind,
        status=manifest.status,
        outcome=manifest.outcome,
        evaluated_target=manifest.evaluated_target,
        checkout_changed=manifest.checkout_state is not CheckoutState.NOT_APPLIED,
        checkout_state=manifest.checkout_state,
        checks=CheckSummary(
            required=[check.name for check in checks if check.required],
            passed=[check.name for check in checks if check.status is CheckStatus.PASSED],
            failed=[
                check.name
                for check in checks
                if check.status in {CheckStatus.FAILED, CheckStatus.TIMED_OUT}
            ],
            skipped=[check.name for check in checks if check.status is CheckStatus.SKIPPED],
        ),
        trust_label=compute_trust_label(
            manifest.execution_mode,
            manifest.isolation_level,
            manifest.verification_status,
        ),
        errors=_terminal_errors(manifest, validation, errors),
        evidence_path=evidence_path,
        result=VerifiedChangeResult(
            selected_candidate_id=manifest.selected_candidate_id,
            applied_paths=manifest.applied_paths,
            final_validation_status=manifest.final_validation_status,
        ),
    )


def provider_failure_error(manifest: RunManifest, *, retryable: bool) -> ErrorDetail:
    return ErrorDetail(
        code=ErrorCode.PROVIDER_UNAVAILABLE,
        message="The model provider could not complete the run.",
        remediation="Check provider readiness and retry the run when it is available.",
        retry=RetryClass.READ_ONLY if retryable else RetryClass.NON_RETRYABLE,
        run_id=manifest.run_id,
        run_kind=manifest.kind,
    )


def _terminal_errors(
    manifest: RunManifest,
    validation: ValidationReport | None,
    explicit_errors: Sequence[ErrorDetail],
) -> list[ErrorDetail]:
    if manifest.status is RunStatus.CANCELLED:
        return []
    if explicit_errors:
        return [_bind_error(error, manifest) for error in explicit_errors]
    detail = _derived_terminal_error(manifest, validation)
    return [_bind_error(detail, manifest)] if detail is not None else []


def _derived_terminal_error(
    manifest: RunManifest,
    validation: ValidationReport | None,
) -> ErrorDetail | None:
    if manifest.checkout_state is CheckoutState.RECOVERY_UNKNOWN:
        return ErrorDetail(
            code=ErrorCode.INTERNAL,
            message="Checkout recovery requires manual verification.",
            remediation="Inspect the affected paths and restore the checkout before retrying.",
            retry=RetryClass.RECONCILE_FIRST,
        )
    if manifest.final_validation_status is FinalValidationStatus.FAILED or (
        validation is not None
        and any(
            check.required and check.status in {CheckStatus.FAILED, CheckStatus.TIMED_OUT}
            for check in validation.checks
        )
    ):
        return ErrorDetail(
            code=ErrorCode.VALIDATION_FAILED,
            message="Required validation did not pass.",
            remediation="Review failed checks in the evidence and fix the patch before retrying.",
            retry=RetryClass.NON_RETRYABLE,
        )
    if manifest.status is not RunStatus.HUMAN_INTERVENTION_REQUIRED:
        return None
    reason = (manifest.reason or "").casefold()
    if "timeout" in reason or "budget" in reason:
        return ErrorDetail(
            code=ErrorCode.LIMIT_EXCEEDED,
            message="A configured execution limit was reached.",
            remediation="Review the run budget or timeout before retrying.",
            retry=RetryClass.NON_RETRYABLE,
        )
    if any(provider in reason for provider in ("provider", "openai", "codex", "scripted")):
        return provider_failure_error(manifest, retryable=True)
    if any(
        policy in reason
        for policy in (
            "preflight",
            "policy",
            "preview",
            "target",
            "baseline",
            "isolation",
            "candidate evidence",
        )
    ):
        return ErrorDetail(
            code=ErrorCode.POLICY,
            message="The run could not satisfy a required repository policy.",
            remediation=(
                "Review the recorded evidence and resolve the policy condition before retrying."
            ),
            retry=RetryClass.NON_RETRYABLE,
        )
    return ErrorDetail(
        code=ErrorCode.INTERNAL,
        message="The run could not be completed safely.",
        remediation="Review the evidence and reconcile the repository state before retrying.",
        retry=RetryClass.RECONCILE_FIRST,
    )


def _bind_error(error: ErrorDetail, manifest: RunManifest) -> ErrorDetail:
    return ErrorDetail(
        code=error.code,
        message=redact_text(error.message),
        remediation=redact_text(error.remediation) if error.remediation is not None else None,
        retry=error.retry,
        run_id=manifest.run_id,
        run_kind=manifest.kind,
    )
