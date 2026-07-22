import json
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator

from repogent.domain import (
    ApprovalKind,
    CheckoutState,
    Decision,
    FinalValidationStatus,
    PendingApproval,
    RunStage,
    RunStatus,
    VersionedModel,
)

BoundedPath = Annotated[str, Field(max_length=4_096)]


class RunStart(VersionedModel):
    repository: Path
    request: str = Field(min_length=1, max_length=10_000)
    provider: str = "codex-cli"
    model: str | None = None
    script: Path | None = None
    executor: str = "docker"
    output_dir: Path | None = None


class RunDecision(VersionedModel):
    run_id: str = Field(min_length=1, max_length=256)
    kind: ApprovalKind
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Decision
    feedback: str | None = Field(default=None, max_length=4_096)


class RunSnapshot(VersionedModel):
    run_id: str = Field(min_length=1, max_length=256)
    status: RunStatus
    stage: RunStage
    pending_approval: PendingApproval | None = None
    checkout_state: CheckoutState
    selected_patch_applied: bool
    applied_paths: list[BoundedPath] = Field(max_length=20)
    final_validation_status: FinalValidationStatus
    reason: str | None = Field(default=None, max_length=4_096)
    evidence_path: str = Field(max_length=4_096)
    cancellation_requested: bool = False

    @field_validator("pending_approval")
    @classmethod
    def bound_pending_artifact(
        cls, pending: PendingApproval | None
    ) -> PendingApproval | None:
        if pending is None:
            return None
        try:
            serialized = json.dumps(
                pending.artifact,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("pending approval artifact is not serializable") from error
        if len(serialized) > 256_000:
            raise ValueError(
                "pending approval artifact exceeds 256,000 serialized characters"
            )
        return pending


class RunReport(VersionedModel):
    run_id: str = Field(min_length=1, max_length=256)
    status: RunStatus
    checkout_state: CheckoutState
    evidence_path: str = Field(max_length=4_096)
    report: str = Field(max_length=64_000)
