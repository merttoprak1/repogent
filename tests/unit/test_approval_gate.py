import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel

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
                    "name": "pytest", "argv": ["pytest"],
                    "status": "passed", "required": true,
                    "stdout": "secret", "stderr": "error"
                }]}
            },
            "selected": true
        }]
    }"""

    payload = approval_payload(ApprovalKind.PATCH, artifact)

    assert payload["selected_candidate"]["proposal"]["diff"] == "patch-body"  # type: ignore[index]
    assert payload["candidates"][0]["checks"] == [  # type: ignore[index]
        {"name": "pytest", "status": "passed", "required": True}
    ]
    serialized = str(payload)
    assert "secret" not in serialized
    assert "error" not in serialized
    assert "argv" not in serialized


def test_non_patch_payload_is_recursively_redacted_before_digest_binding() -> None:
    artifact = """{
        "objective": "keep token=sk-proj-1234567890abcdef private",
        "nested": {"password": "correct-horse-battery-staple"}
    }"""
    approver = GateApprover("run-1")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approver.decide, ApprovalKind.REQUIREMENTS, artifact)
        _, pending = approver.wait(after_generation=0, timeout_seconds=1)
        assert pending is not None
        approver.close()
        assert future.result(timeout=1).decision is Decision.REJECTED

    assert "sk-proj-1234567890abcdef" not in str(pending.artifact)
    assert "correct-horse-battery-staple" not in str(pending.artifact)
    assert pending.digest == approval_digest(
        ApprovalKind.REQUIREMENTS,
        json.dumps(pending.artifact),
    )


def test_patch_payload_redacts_bounded_check_metadata_without_raw_process_data() -> None:
    safe_diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    sensitive_name = "lint token=sk-proj-1234567890abcdef " + "x" * 256
    sensitive_reason = "password=do-not-show " + "y" * 5_000
    artifact = {
        "selected_candidate": {
            "candidate_id": "candidate-1",
            "proposal": {"diff": safe_diff, "summary": "token=private-value"},
        },
        "selection": {"selected_candidate_id": "candidate-1"},
        "candidates": [
            {
                "candidate": {"candidate_id": "candidate-1"},
                "evidence": {
                    "eligible": True,
                    "required_failures": [],
                    "skipped_checks": [sensitive_name],
                    "changed_files": 1,
                    "changed_lines": 2,
                    "acceptance_criteria_coverage": 1,
                    "validation": {
                        "checks": [
                            {
                                "name": sensitive_name,
                                "argv": ["ruff", "--token", "raw-argv-secret"],
                                "status": "skipped",
                                "required": False,
                                "reason": sensitive_reason,
                                "stdout": "raw-stdout-secret",
                                "stderr": "raw-stderr-secret",
                            }
                        ]
                    },
                },
                "selected": True,
            }
        ],
    }

    payload = approval_payload(ApprovalKind.PATCH, json.dumps(artifact))

    assert isinstance(payload, dict)
    assert payload["selected_candidate"]["proposal"]["diff"] == safe_diff  # type: ignore[index]
    summary = payload["candidates"][0]  # type: ignore[index]
    assert set(summary["checks"][0]) == {"name", "status", "required"}  # type: ignore[index]
    assert len(summary["checks"][0]["name"]) <= 128  # type: ignore[index]
    assert summary["skipped_checks"] == [  # type: ignore[index]
        {
            "name": summary["checks"][0]["name"],  # type: ignore[index]
            "reason": "password=[REDACTED] " + "y" * (4_096 - 20),
        }
    ]
    serialized = str(payload)
    for forbidden in (
        "sk-proj-1234567890abcdef",
        "do-not-show",
        "raw-argv-secret",
        "raw-stdout-secret",
        "raw-stderr-secret",
    ):
        assert forbidden not in serialized
    assert approval_digest(ApprovalKind.PATCH, json.dumps(artifact)) == (
        hashlib.sha256(safe_diff.encode()).hexdigest()
    )


def test_patch_payload_fails_closed_when_redaction_would_change_exact_diff() -> None:
    unsafe_diff = (
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n"
        "+token=sk-proj-1234567890abcdef\n"
    )
    artifact = json.dumps(
        {
            "selected_candidate": {"proposal": {"diff": unsafe_diff}},
            "selection": {"selected_candidate_id": "candidate-1"},
            "candidates": [],
        }
    )

    with pytest.raises(ApprovalGateError, match="secret-like patch content"):
        approval_payload(ApprovalKind.PATCH, artifact)


def test_close_is_terminal_and_cannot_be_overridden_by_a_matching_submit() -> None:
    approver = GateApprover("run-1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approver.decide, ApprovalKind.PLAN, '{"steps": []}')
        _, pending = approver.wait(after_generation=0, timeout_seconds=1)
        assert pending is not None
        approver.close()
        with pytest.raises(ApprovalGateError, match="closed"):
            approver.submit(ApprovalKind.PLAN, pending.digest, Decision.APPROVED, None)
        assert future.result(timeout=1).decision is Decision.REJECTED


def test_closed_wait_does_not_replay_the_current_pending_gate() -> None:
    approver = GateApprover("run-1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approver.decide, ApprovalKind.PLAN, '{"steps": []}')
        generation, pending = approver.wait(after_generation=0, timeout_seconds=1)
        assert pending is not None
        approver.close()
        assert approver.wait(after_generation=generation, timeout_seconds=1) == (
            generation,
            None,
        )
        assert future.result(timeout=1).decision is Decision.REJECTED


class PatchArtifact(BaseModel):
    selected_candidate: dict[str, object]
    selection: dict[str, object]
    candidates: list[dict[str, object]]


def test_model_patch_payload_redacts_validation_output() -> None:
    artifact = PatchArtifact(
        selected_candidate={"proposal": {"diff": "patch-body"}},
        selection={"selected_candidate_id": "candidate-1"},
        candidates=[
            {
                "candidate": {"candidate_id": "candidate-1"},
                "evidence": {
                    "eligible": True,
                    "validation": {
                        "checks": [
                            {
                                "argv": ["pytest"],
                                "stdout": "secret",
                                "stderr": "error",
                            }
                        ]
                    },
                },
            }
        ],
    )

    serialized = str(approval_payload(ApprovalKind.PATCH, artifact))
    assert "secret" not in serialized
    assert "error" not in serialized
    assert "pytest" not in serialized


class DriftingArtifact(BaseModel):
    value: str = "before"

    def model_dump(self, **kwargs: object) -> dict[str, object]:
        payload = super().model_dump(**kwargs)
        self.value = "after"
        return payload


def test_gate_binds_digest_and_display_to_one_detached_snapshot() -> None:
    approver = GateApprover("run-1")
    artifact = DriftingArtifact()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(approver.decide, ApprovalKind.REQUIREMENTS, artifact)
        _, pending = approver.wait(after_generation=0, timeout_seconds=1)
        assert pending is not None
        approver.close()
        assert future.result(timeout=1).decision is Decision.REJECTED
        assert pending.artifact == {"value": "before"}
        assert pending.digest == approval_digest(
            ApprovalKind.REQUIREMENTS, '{"value":"before"}'
        )
