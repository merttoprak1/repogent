from __future__ import annotations

import hashlib
import json
import threading
import time

from pydantic import BaseModel

from repogent.domain import ApprovalKind, ApprovalRecord, Decision, PendingApproval


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
        return payload
    if kind is not ApprovalKind.PATCH:
        return payload
    selected = payload.get("selected_candidate")
    selection = payload.get("selection")
    candidates = payload.get("candidates", [])
    if not isinstance(selected, dict) or not isinstance(candidates, list):
        raise ApprovalGateError("patch approval artifact is malformed")
    summaries: list[dict[str, object]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        candidate = item.get("candidate")
        evidence = item.get("evidence")
        if not isinstance(candidate, dict) or not isinstance(evidence, dict):
            continue
        summaries.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "eligible": evidence.get("eligible"),
                "required_failures": evidence.get("required_failures", []),
                "skipped_checks": evidence.get("skipped_checks", []),
                "changed_files": evidence.get("changed_files"),
                "changed_lines": evidence.get("changed_lines"),
                "acceptance_criteria_coverage": evidence.get(
                    "acceptance_criteria_coverage"
                ),
                "selected": item.get("selected", False),
            }
        )
    return {
        "selected_candidate": selected,
        "selection": selection,
        "candidates": summaries,
    }


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
