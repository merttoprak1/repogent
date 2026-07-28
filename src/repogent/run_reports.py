from __future__ import annotations

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
from repogent.errors import ErrorDetail


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
    errors: list[ErrorDetail] = Field(default_factory=list)
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
        evidence_path=evidence_path,
        result=VerifiedChangeResult(
            selected_candidate_id=manifest.selected_candidate_id,
            applied_paths=manifest.applied_paths,
            final_validation_status=manifest.final_validation_status,
        ),
    )
