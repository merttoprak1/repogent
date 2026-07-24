from __future__ import annotations

import hashlib
import json
import threading
import time

from pydantic import BaseModel

from repogent.domain import (
    ApprovalKind,
    ApprovalRecord,
    Decision,
    ExecutionMode,
    IsolationLevel,
    PendingApproval,
    VerificationStatus,
)
from repogent.executor_selection import validate_executor_isolation
from repogent.sanitization import redact_text, sanitize_data

_CHECK_NAME_MAX_CHARS = 128
_CHECK_REASON_MAX_CHARS = 4_096


class ApprovalGateError(RuntimeError):
    pass


def approval_payload(
    kind: ApprovalKind, artifact: BaseModel | str
) -> dict[str, object] | str:
    return _approval_payload_from_snapshot(kind, _artifact_snapshot(artifact))


def _artifact_snapshot(artifact: BaseModel | str) -> dict[str, object] | str:
    if isinstance(artifact, BaseModel):
        payload = artifact.model_dump(mode="json")
        snapshot = json.loads(json.dumps(payload))
        if not isinstance(snapshot, dict):
            raise ApprovalGateError("approval artifact must serialize to an object")
        return snapshot
    try:
        parsed = json.loads(artifact)
    except json.JSONDecodeError:
        return artifact
    if not isinstance(parsed, dict):
        return artifact
    return parsed


def _approval_payload_from_snapshot(
    kind: ApprovalKind, payload: dict[str, object] | str
) -> dict[str, object] | str:
    if not isinstance(payload, dict):
        sanitized_text = sanitize_data(payload)
        if not isinstance(sanitized_text, str):
            raise ApprovalGateError("approval artifact sanitization failed")
        return sanitized_text
    if kind is not ApprovalKind.PATCH:
        sanitized_payload = sanitize_data(payload)
        if not isinstance(sanitized_payload, dict):
            raise ApprovalGateError("approval artifact sanitization failed")
        return sanitized_payload
    selected = payload.get("selected_candidate")
    selection = payload.get("selection")
    candidates = payload.get("candidates", [])
    if not isinstance(selected, dict) or not isinstance(candidates, list):
        raise ApprovalGateError("patch approval artifact is malformed")
    proposal = selected.get("proposal")
    exact_diff = proposal.get("diff") if isinstance(proposal, dict) else None
    if isinstance(exact_diff, str) and redact_text(exact_diff) != exact_diff:
        raise ApprovalGateError("approval artifact contains secret-like patch content")
    execution_evidence = _patch_execution_evidence(payload)
    summaries: list[dict[str, object]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        candidate = item.get("candidate")
        evidence = item.get("evidence")
        if not isinstance(candidate, dict) or not isinstance(evidence, dict):
            continue
        validation = evidence.get("validation")
        validation_checks = (
            validation.get("checks", []) if isinstance(validation, dict) else []
        )
        checks: list[dict[str, object]] = []
        skipped_checks: list[dict[str, str]] = []
        for check in validation_checks:
            if not isinstance(check, dict):
                continue
            name = _bounded_redacted(check.get("name"), _CHECK_NAME_MAX_CHARS)
            checks.append(
                {
                    "name": name,
                    "status": check.get("status"),
                    "required": check.get("required"),
                }
            )
            if check.get("status") == "skipped":
                skipped_checks.append(
                    {
                        "name": name,
                        "reason": _bounded_redacted(
                            check.get("reason"), _CHECK_REASON_MAX_CHARS
                        ),
                    }
                )
        summaries.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "eligible": evidence.get("eligible"),
                "checks": checks,
                "required_failures": [
                    _bounded_redacted(name, _CHECK_NAME_MAX_CHARS)
                    for name in evidence.get("required_failures", [])
                    if isinstance(name, str)
                ],
                "skipped_checks": skipped_checks,
                "changed_files": evidence.get("changed_files"),
                "changed_lines": evidence.get("changed_lines"),
                "acceptance_criteria_coverage": evidence.get(
                    "acceptance_criteria_coverage"
                ),
                "selected": item.get("selected", False),
            }
        )
    result = {
        **execution_evidence,
        "selected_candidate": selected,
        "selection": selection,
        "candidates": summaries,
    }
    sanitized_result = sanitize_data(result)
    if not isinstance(sanitized_result, dict):
        raise ApprovalGateError("approval artifact sanitization failed")
    sanitized_selected = sanitized_result.get("selected_candidate")
    sanitized_proposal = (
        sanitized_selected.get("proposal")
        if isinstance(sanitized_selected, dict)
        else None
    )
    sanitized_diff = (
        sanitized_proposal.get("diff")
        if isinstance(sanitized_proposal, dict)
        else None
    )
    if isinstance(exact_diff, str) and sanitized_diff != exact_diff:
        raise ApprovalGateError("approval artifact contains secret-like patch content")
    return sanitized_result


def _patch_execution_evidence(payload: dict[str, object]) -> dict[str, str]:
    mode_value = payload.get("execution_mode")
    isolation_value = payload.get("isolation_level")
    verification_value = payload.get("verification_status")
    if mode_value is None and isolation_value is None and verification_value is None:
        return {}
    if (
        not isinstance(mode_value, str)
        or not isinstance(isolation_value, str)
        or not isinstance(verification_value, str)
    ):
        raise ApprovalGateError("patch approval execution evidence is malformed")
    try:
        mode = ExecutionMode(mode_value)
        isolation = IsolationLevel(isolation_value)
        verification = VerificationStatus(verification_value)
    except ValueError as error:
        raise ApprovalGateError("patch approval execution evidence is malformed") from error
    try:
        validate_executor_isolation(mode, isolation)
    except ValueError as error:
        raise ApprovalGateError(str(error)) from error
    if verification is not VerificationStatus.PASSED:
        raise ApprovalGateError("patch approval execution evidence is not passed")
    return {
        "execution_mode": mode.value,
        "isolation_level": isolation.value,
        "verification_status": verification.value,
    }


def _bounded_redacted(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return redact_text(value)[:limit]


def approval_digest(kind: ApprovalKind, artifact: BaseModel | str) -> str:
    return _approval_digest_from_payload(kind, approval_payload(kind, artifact))


def _approval_digest_from_payload(
    kind: ApprovalKind, payload: dict[str, object] | str
) -> str:
    if kind is ApprovalKind.PATCH and isinstance(payload, dict):
        selected = payload.get("selected_candidate")
        if isinstance(selected, dict):
            proposal = selected.get("proposal")
            if isinstance(proposal, dict) and isinstance(proposal.get("diff"), str):
                return hashlib.sha256(proposal["diff"].encode()).hexdigest()
        raise ApprovalGateError("patch approval artifact does not contain an exact diff")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class GateApprover:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._condition = threading.Condition()
        self._generation = 0
        self._pending: PendingApproval | None = None
        self._decision: ApprovalRecord | None = None
        self._closed = False

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    def decide(self, kind: ApprovalKind, artifact: BaseModel | str) -> ApprovalRecord:
        snapshot = _artifact_snapshot(artifact)
        payload = _approval_payload_from_snapshot(kind, snapshot)
        pending = PendingApproval(
            run_id=self.run_id,
            kind=kind,
            digest=_approval_digest_from_payload(kind, payload),
            artifact=payload,
        )
        with self._condition:
            if self._closed:
                return ApprovalRecord(
                    kind=kind, decision=Decision.REJECTED, feedback="run session closed"
                )
            if self._pending is not None:
                raise ApprovalGateError("another approval is already pending")
            self._pending = pending
            self._decision = None
            self._generation += 1
            self._condition.notify_all()
            while self._decision is None and not self._closed:
                self._condition.wait()
            record = self._decision or ApprovalRecord(
                kind=kind, decision=Decision.REJECTED, feedback="run session closed"
            )
            self._pending = None
            self._decision = None
            self._condition.notify_all()
            return record

    def wait(
        self, *, after_generation: int, timeout_seconds: float
    ) -> tuple[int, PendingApproval | None]:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            if self._closed:
                return self._generation, None
            while self._generation <= after_generation and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._generation, None
                self._condition.wait(remaining)
            if self._closed:
                return self._generation, None
            return self._generation, self._pending

    def submit(
        self,
        kind: ApprovalKind,
        digest: str,
        decision: Decision,
        feedback: str | None,
    ) -> None:
        with self._condition:
            if self._closed:
                raise ApprovalGateError("approval gate is closed")
            pending = self._pending
            if pending is None:
                raise ApprovalGateError("no approval is pending")
            if self._decision is not None:
                raise ApprovalGateError("approval decision has already been submitted")
            if pending.kind is not kind:
                raise ApprovalGateError("approval kind does not match the pending gate")
            if pending.digest != digest:
                raise ApprovalGateError("approval digest does not match the displayed artifact")
            self._decision = ApprovalRecord(
                kind=kind, decision=decision, feedback=feedback
            )
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            if self._pending is not None:
                self._decision = ApprovalRecord(
                    kind=self._pending.kind,
                    decision=Decision.REJECTED,
                    feedback="run session closed",
                )
            self._condition.notify_all()
