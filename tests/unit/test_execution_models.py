import pytest
from pydantic import ValidationError

import repogent.domain as domain
import repogent.mcp_models as mcp_models


def test_execution_contract_values_are_stable() -> None:
    assert [item.value for item in getattr(domain, "ExecutionMode", ())] == ["docker", "local"]
    assert [item.value for item in getattr(domain, "IsolationLevel", ())] == [
        "reduced_isolation",
        "isolated",
    ]
    assert [item.value for item in getattr(domain, "VerificationStatus", ())] == [
        "unvalidated",
        "validating",
        "passed",
        "failed",
    ]
    assert [item.value for item in getattr(domain, "TrustLabel", ())] == [
        "UNVALIDATED",
        "REDUCED ISOLATION",
        "ISOLATED VERIFIED",
    ]


def executor_option(index: int) -> object:
    option_model = getattr(mcp_models, "ExecutorOption", None)
    assert option_model is not None
    return option_model(
        mode=domain.ExecutionMode.DOCKER if index == 0 else domain.ExecutionMode.LOCAL,
        available=True,
        isolation_level=domain.IsolationLevel.ISOLATED,
        option_digest=f"{index}" * 64,
        message="Executor is available",
    )


def test_execution_decision_binds_mode_and_preview() -> None:
    decision_model = getattr(mcp_models, "ExecutionDecision", None)
    assert decision_model is not None
    decision = decision_model(
        run_id="run-1",
        preview_digest="a" * 64,
        mode=domain.ExecutionMode.LOCAL,
        option_digest="b" * 64,
        decision=domain.Decision.APPROVED,
    )
    assert decision.mode is domain.ExecutionMode.LOCAL


def test_pending_choice_rejects_unbounded_options() -> None:
    pending_choice_model = getattr(mcp_models, "PendingExecutionChoice", None)
    assert pending_choice_model is not None
    with pytest.raises(ValidationError):
        pending_choice_model(
            run_id="run-1",
            preview_digest="a" * 64,
            preview={"diff": "x"},
            options=[executor_option(index) for index in range(3)],
        )


def test_executor_availability_has_bounded_public_fields() -> None:
    availability_model = getattr(mcp_models, "ExecutorAvailability", None)
    assert availability_model is not None
    availability = availability_model(
        mode=domain.ExecutionMode.LOCAL,
        available=False,
        isolation_level=domain.IsolationLevel.REDUCED_ISOLATION,
        message="Local execution is unavailable",
        remediation="Install the required toolchain.",
        risk_statement="Local execution runs with reduced isolation.",
    )
    assert availability.schema_version == "1"

    with pytest.raises(ValidationError, match="at most 512"):
        availability_model(
            mode=domain.ExecutionMode.LOCAL,
            available=False,
            isolation_level=domain.IsolationLevel.REDUCED_ISOLATION,
            message="x" * 513,
        )


def snapshot_payload() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "status": domain.RunStatus.RUNNING,
        "stage": domain.RunStage.PATCH_PREVIEWED,
        "checkout_state": domain.CheckoutState.NOT_APPLIED,
        "selected_patch_applied": False,
        "applied_paths": [],
        "final_validation_status": domain.FinalValidationStatus.NOT_STARTED,
        "evidence_path": "/evidence/run-1",
    }


def test_snapshot_derives_trust_label_from_verified_isolation() -> None:
    expected_fields = {
        "pending_execution",
        "execution_mode",
        "isolation_level",
        "verification_status",
        "trust_label",
    }
    assert expected_fields <= mcp_models.RunSnapshot.model_fields.keys()

    isolated = mcp_models.RunSnapshot(
        **snapshot_payload(),
        execution_mode=domain.ExecutionMode.DOCKER,
        isolation_level=domain.IsolationLevel.ISOLATED,
        verification_status=domain.VerificationStatus.PASSED,
        trust_label=domain.TrustLabel.UNVALIDATED,
    )
    assert isolated.trust_label is domain.TrustLabel.ISOLATED_VERIFIED

    reduced = mcp_models.RunSnapshot(
        **snapshot_payload(),
        execution_mode=domain.ExecutionMode.LOCAL,
        isolation_level=domain.IsolationLevel.REDUCED_ISOLATION,
        verification_status=domain.VerificationStatus.PASSED,
    )
    assert reduced.trust_label is domain.TrustLabel.REDUCED_ISOLATION


def test_snapshot_local_mode_never_reports_isolated_verified() -> None:
    local = mcp_models.RunSnapshot(
        **snapshot_payload(),
        execution_mode=domain.ExecutionMode.LOCAL,
        isolation_level=domain.IsolationLevel.ISOLATED,
        verification_status=domain.VerificationStatus.PASSED,
    )

    assert local.trust_label is domain.TrustLabel.REDUCED_ISOLATION


def test_snapshot_bounds_pending_execution_preview_by_canonical_json_size() -> None:
    assert "pending_execution" in mcp_models.RunSnapshot.model_fields
    preview_model = mcp_models.PendingExecutionChoice
    exact = preview_model(
        run_id="run-1",
        preview_digest="a" * 64,
        preview={"diff": "x" * 255_989},
        options=[executor_option(index) for index in range(2)],
    )
    oversized = exact.model_copy(update={"preview": {"diff": "x" * 255_990}})

    accepted = mcp_models.RunSnapshot(**snapshot_payload(), pending_execution=exact)
    assert accepted.pending_execution == exact
    with pytest.raises(ValidationError, match="256,000"):
        mcp_models.RunSnapshot(**snapshot_payload(), pending_execution=oversized)
