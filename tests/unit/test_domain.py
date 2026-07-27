from decimal import Decimal

import pytest
from pydantic import ValidationError

from repogent.domain import (
    ApprovalKind,
    Budget,
    CandidateEvidence,
    CheckoutState,
    CheckResult,
    CheckStatus,
    Decision,
    ExecutionMode,
    FinalValidationStatus,
    ImplementationPlan,
    PendingApproval,
    PlanStep,
    ProviderCallEvidence,
    ProviderCallStatus,
    RequirementsSpec,
    RiskLevel,
    RunEvent,
    RunManifest,
    RunStage,
    RunStatus,
    ValidationReport,
    VerificationStatus,
)
from repogent.mcp_models import RunDecision, RunReport, RunSnapshot, RunStart


def test_pending_approval_requires_sha256_digest() -> None:
    pending = PendingApproval(
        run_id="run-1",
        kind=ApprovalKind.PLAN,
        digest="a" * 64,
        artifact={"steps": []},
    )
    assert pending.schema_version == "1"


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64])
def test_pending_approval_rejects_invalid_sha256_digests(digest: str) -> None:
    with pytest.raises(ValidationError, match="digest"):
        PendingApproval(
            run_id="run-1",
            kind=ApprovalKind.PLAN,
            digest=digest,
            artifact={"steps": []},
        )


def test_provider_call_evidence_requires_a_positive_invocation() -> None:
    evidence = ProviderCallEvidence(
        provider="codex-cli",
        model="default",
        role="requirements",
        invocation=1,
        status=ProviderCallStatus.AUTHENTICATION_FAILED,
    )

    assert evidence.structured_output_valid is False

    with pytest.raises(ValidationError):
        ProviderCallEvidence(**{**evidence.model_dump(), "invocation": 0})


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


def test_old_manifest_payload_receives_safe_execution_defaults() -> None:
    manifest = RunManifest.model_validate(
        {"run_id": "legacy-run", "request": "Apply a safe change"}
    )

    assert manifest.execution_mode is None
    assert manifest.isolation_level is None
    assert manifest.verification_status is VerificationStatus.UNVALIDATED
    assert manifest.preview_digest is None

    selected = RunManifest(
        run_id="run-123",
        request="Apply a safe change",
        execution_mode=ExecutionMode.LOCAL,
    )
    assert selected.execution_mode is ExecutionMode.LOCAL


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


def test_mcp_run_start_and_decision_are_versioned_and_typed(tmp_path) -> None:
    target = tmp_path / "target"
    script = tmp_path / "script.json"
    start = RunStart(
        repository=target,
        request="Add health endpoint",
        provider="scripted",
        script=script,
        executor="local",
        output_dir=tmp_path / "runs",
    )
    decision = RunDecision(
        run_id="run-1",
        kind=ApprovalKind.REQUIREMENTS,
        digest="a" * 64,
        decision=Decision.APPROVED,
    )

    assert start.repository == target
    assert start.schema_version == "1"
    assert decision.feedback is None

    assert RunStart(repository=target, request="Apply a safe change").executor == "docker"
    assert (
        RunStart(
            repository=target,
            request="Apply a safe change",
            executor="deferred",
        ).executor
        == "deferred"
    )
    with pytest.raises(ValidationError, match="executor"):
        RunStart(repository=target, request="Apply a safe change", executor="remote")


def test_mcp_models_enforce_input_and_output_bounds(tmp_path) -> None:
    with pytest.raises(ValidationError, match="at most 10000"):
        RunStart(repository=tmp_path, request="x" * 10_001)
    with pytest.raises(ValidationError, match="digest"):
        RunDecision(
            run_id="run-1",
            kind=ApprovalKind.PLAN,
            digest="A" * 64,
            decision="approved",
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RunSnapshot(
            run_id="run-1",
            status=RunStatus.RUNNING,
            stage=RunStage.CREATED,
            checkout_state=CheckoutState.NOT_APPLIED,
            selected_patch_applied=False,
            applied_paths=[],
            final_validation_status=FinalValidationStatus.NOT_STARTED,
            evidence_path="/evidence/run-1",
            validation_stdout="secret output",
        )


def _snapshot_payload() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "status": RunStatus.RUNNING,
        "stage": RunStage.CREATED,
        "checkout_state": CheckoutState.NOT_APPLIED,
        "selected_patch_applied": False,
        "applied_paths": [],
        "final_validation_status": FinalValidationStatus.NOT_STARTED,
        "evidence_path": "/evidence/run-1",
    }


@pytest.mark.parametrize("model", ["decision", "snapshot", "report"])
def test_mcp_response_run_ids_are_limited_to_256_characters(model: str) -> None:
    run_id = "r" * 257
    with pytest.raises(ValidationError, match="at most 256"):
        if model == "decision":
            RunDecision(
                run_id=run_id,
                kind=ApprovalKind.PLAN,
                digest="a" * 64,
                decision=Decision.APPROVED,
            )
        elif model == "snapshot":
            RunSnapshot(**{**_snapshot_payload(), "run_id": run_id})
        else:
            RunReport(
                run_id=run_id,
                status=RunStatus.COMPLETED,
                checkout_state=CheckoutState.NOT_APPLIED,
                evidence_path="/evidence/run-1",
                report="done",
            )


@pytest.mark.parametrize("field", ["reason", "evidence_path"])
def test_run_snapshot_text_fields_are_limited_to_4096_characters(field: str) -> None:
    with pytest.raises(ValidationError, match="at most 4096"):
        RunSnapshot(**{**_snapshot_payload(), field: "x" * 4_097})


def test_run_report_evidence_path_is_limited_to_4096_characters() -> None:
    with pytest.raises(ValidationError, match="at most 4096"):
        RunReport(
            run_id="run-1",
            status=RunStatus.COMPLETED,
            checkout_state=CheckoutState.NOT_APPLIED,
            evidence_path="x" * 4_097,
            report="done",
        )


def test_run_snapshot_applied_paths_are_bounded_by_count_and_length() -> None:
    with pytest.raises(ValidationError, match="at most 20"):
        RunSnapshot(
            **{**_snapshot_payload(), "applied_paths": [f"file-{index}" for index in range(21)]}
        )
    with pytest.raises(ValidationError, match="at most 4096"):
        RunSnapshot(**{**_snapshot_payload(), "applied_paths": ["x" * 4_097]})


def test_run_snapshot_pending_artifact_accepts_256000_and_rejects_256001_chars() -> None:
    exact = PendingApproval(
        run_id="run-1",
        kind=ApprovalKind.REQUIREMENTS,
        digest="a" * 64,
        artifact="x" * 255_998,
    )
    oversized = PendingApproval(
        run_id="run-1",
        kind=ApprovalKind.REQUIREMENTS,
        digest="a" * 64,
        artifact="x" * 255_999,
    )

    accepted = RunSnapshot(**{**_snapshot_payload(), "pending_approval": exact})
    assert accepted.pending_approval == exact
    with pytest.raises(ValidationError, match="256,000"):
        RunSnapshot(**{**_snapshot_payload(), "pending_approval": oversized})


def test_run_snapshot_rejects_pending_approval_run_id_over_256_chars() -> None:
    pending = PendingApproval(
        run_id="r" * 257,
        kind=ApprovalKind.REQUIREMENTS,
        digest="a" * 64,
        artifact={"objective": "change"},
    )

    with pytest.raises(ValidationError, match="pending approval run ID"):
        RunSnapshot(**{**_snapshot_payload(), "pending_approval": pending})


def test_run_snapshot_rejects_pending_approval_for_another_run() -> None:
    pending = PendingApproval(
        run_id="another-run",
        kind=ApprovalKind.REQUIREMENTS,
        digest="a" * 64,
        artifact={"objective": "change"},
    )

    with pytest.raises(ValidationError, match="does not match"):
        RunSnapshot(**{**_snapshot_payload(), "pending_approval": pending})
