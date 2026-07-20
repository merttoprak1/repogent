from __future__ import annotations

from collections import deque
from typing import Protocol

import typer
from pydantic import BaseModel

from repogent.domain import ApprovalKind, ApprovalRecord, Decision


class Approver(Protocol):
    def decide(self, kind: ApprovalKind, artifact: BaseModel | str) -> ApprovalRecord: ...


def render_artifact(artifact: BaseModel | str) -> str:
    return artifact if isinstance(artifact, str) else artifact.model_dump_json(indent=2)


class CliApprover:
    def decide(self, kind: ApprovalKind, artifact: BaseModel | str) -> ApprovalRecord:
        typer.echo(f"\n--- {kind.value} approval ---\n{render_artifact(artifact)}\n")
        approved = typer.confirm(f"Approve {kind.value}?", default=False)
        feedback = (
            None
            if approved
            else typer.prompt("Reason for rejection", default="Rejected by user")
        )
        return ApprovalRecord(
            kind=kind,
            decision=Decision.APPROVED if approved else Decision.REJECTED,
            feedback=feedback,
        )


class FakeApprover:
    def __init__(self, decisions: list[Decision]) -> None:
        self._decisions = deque(decisions)
        self.records: list[ApprovalRecord] = []

    def decide(self, kind: ApprovalKind, artifact: BaseModel | str) -> ApprovalRecord:
        del artifact
        if not self._decisions:
            raise RuntimeError("fake approver has no decision remaining")
        record = ApprovalRecord(kind=kind, decision=self._decisions.popleft())
        self.records.append(record)
        return record
