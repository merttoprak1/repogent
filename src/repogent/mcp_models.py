from pathlib import Path

from pydantic import Field

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


class RunStart(VersionedModel):
    repository: Path
    request: str = Field(min_length=1, max_length=10_000)
    provider: str = "codex-cli"
    model: str | None = None
    script: Path | None = None
    executor: str = "docker"
    output_dir: Path | None = None


class RunDecision(VersionedModel):
    run_id: str = Field(min_length=1)
    kind: ApprovalKind
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Decision
    feedback: str | None = Field(default=None, max_length=4_096)


class RunSnapshot(VersionedModel):
    run_id: str
    status: RunStatus
    stage: RunStage
    pending_approval: PendingApproval | None = None
    checkout_state: CheckoutState
    selected_patch_applied: bool
    applied_paths: list[str]
    final_validation_status: FinalValidationStatus
    reason: str | None = None
    evidence_path: str
    cancellation_requested: bool = False


class RunReport(VersionedModel):
    run_id: str
    status: RunStatus
    checkout_state: CheckoutState
    evidence_path: str
    report: str = Field(max_length=64_000)
