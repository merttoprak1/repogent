from concurrent.futures import ThreadPoolExecutor

import pytest

from repogent.approval_gate import (
    ApprovalGateError,
    GateApprover,
    approval_digest,
    approval_payload,
)
from repogent.domain import ApprovalKind, Decision, RequirementsSpec


def requirements() -> RequirementsSpec:
    return RequirementsSpec(
        objective="Change health behavior",
        functional_requirements=["Return ok"],
        acceptance_criteria=["Tests pass"],
    )


def test_gate_waits_for_matching_digest() -> None:
    approver = GateApprover("run-1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approver.decide, ApprovalKind.REQUIREMENTS, requirements())
        generation, pending = approver.wait(after_generation=0, timeout_seconds=1)
        assert generation == 1
        assert pending.kind is ApprovalKind.REQUIREMENTS
        with pytest.raises(ApprovalGateError, match="digest"):
            approver.submit(
                ApprovalKind.REQUIREMENTS, "0" * 64, Decision.APPROVED, None
            )
        approver.submit(pending.kind, pending.digest, Decision.APPROVED, None)
        assert future.result(timeout=1).decision is Decision.APPROVED


def test_patch_digest_is_exact_diff_digest() -> None:
    artifact = '{"selected_candidate":{"proposal":{"diff":"patch-body"}}}'
    assert approval_digest(ApprovalKind.PATCH, artifact) == (
        "43b5f874219b5f838e5128ed38da54cbdff4540f144994cdffee2049d8f96ee2"
    )


def test_close_releases_waiting_decision_with_rejection() -> None:
    approver = GateApprover("run-1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approver.decide, ApprovalKind.PLAN, '{"steps": []}')
        _, pending = approver.wait(after_generation=0, timeout_seconds=1)
        assert pending is not None
        approver.close()
        record = future.result(timeout=1)

    assert record.decision is Decision.REJECTED
    assert record.feedback == "run session closed"


def test_gate_rejects_wrong_kind_and_duplicate_decision() -> None:
    approver = GateApprover("run-1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approver.decide, ApprovalKind.PLAN, '{"steps": []}')
        _, pending = approver.wait(after_generation=0, timeout_seconds=1)
        assert pending is not None
        with pytest.raises(ApprovalGateError, match="kind"):
            approver.submit(
                ApprovalKind.REQUIREMENTS, pending.digest, Decision.APPROVED, None
            )
        approver.submit(ApprovalKind.PLAN, pending.digest, Decision.APPROVED, None)
        with pytest.raises(ApprovalGateError, match="decision"):
            approver.submit(ApprovalKind.PLAN, pending.digest, Decision.REJECTED, None)
        assert future.result(timeout=1).decision is Decision.APPROVED


def test_non_patch_digests_are_canonical_across_key_ordering() -> None:
    first = '{"objective":"health","steps":["change"]}'
    second = '{"steps":["change"],"objective":"health"}'

    assert approval_digest(ApprovalKind.REQUIREMENTS, first) == approval_digest(
        ApprovalKind.REQUIREMENTS, second
    )
    assert approval_digest(ApprovalKind.PLAN, first) == approval_digest(ApprovalKind.PLAN, second)


def test_wait_after_generation_returns_only_a_new_gate() -> None:
    approver = GateApprover("run-1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(approver.decide, ApprovalKind.REQUIREMENTS, requirements())
        generation, pending = approver.wait(after_generation=0, timeout_seconds=1)
        assert pending is not None
        approver.submit(pending.kind, pending.digest, Decision.APPROVED, None)
        assert first.result(timeout=1).decision is Decision.APPROVED

        second = pool.submit(approver.decide, ApprovalKind.PLAN, '{"steps": []}')
        next_generation, next_pending = approver.wait(
            after_generation=generation, timeout_seconds=1
        )
        assert next_generation == generation + 1
        assert next_pending is not None
        assert next_pending.kind is ApprovalKind.PLAN
        approver.submit(next_pending.kind, next_pending.digest, Decision.APPROVED, None)
        assert second.result(timeout=1).decision is Decision.APPROVED


def test_patch_payload_retains_diff_and_redacts_validation_output() -> None:
    artifact = """{
        "selected_candidate": {"proposal": {"diff": "patch-body"}},
        "selection": {"selected_candidate_id": "candidate-1"},
        "candidates": [{
            "candidate": {"candidate_id": "candidate-1"},
            "evidence": {
                "eligible": true,
                "required_failures": [],
                "skipped_checks": [],
                "changed_files": 1,
                "changed_lines": 2,
                "acceptance_criteria_coverage": 1,
                "validation": {"checks": [{
                    "argv": ["pytest"], "stdout": "secret", "stderr": "error"
                }]}
            },
            "selected": true
        }]
    }"""

    payload = approval_payload(ApprovalKind.PATCH, artifact)

    assert payload["selected_candidate"]["proposal"]["diff"] == "patch-body"  # type: ignore[index]
    serialized = str(payload)
    assert "secret" not in serialized
    assert "error" not in serialized
    assert "pytest" not in serialized
