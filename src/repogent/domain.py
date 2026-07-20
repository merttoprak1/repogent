from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class VersionedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RunStatus(StrEnum):
    RUNNING = "running"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    COMPLETED_WITH_FINDINGS = "completed_with_findings"
    CHANGES_REQUESTED = "changes_requested"
    HUMAN_INTERVENTION_REQUIRED = "human_intervention_required"


class RunStage(StrEnum):
    CREATED = "created"
    ANALYZED = "analyzed"
    REQUIREMENTS = "requirements"
    REQUIREMENTS_APPROVED = "requirements_approved"
    PLANNED = "planned"
    PLAN_APPROVED = "plan_approved"
    PATCH_PROPOSED = "patch_proposed"
    PATCH_APPROVED = "patch_approved"
    PATCH_APPLIED = "patch_applied"
    VALIDATED = "validated"
    REPAIRING = "repairing"
    REVIEWED = "reviewed"
    FINISHED = "finished"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"


class ApprovalKind(StrEnum):
    REQUIREMENTS = "requirements"
    PLAN = "plan"
    PATCH = "patch"
    REPAIR_PATCH = "repair_patch"


class Decision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class MergeRecommendation(StrEnum):
    APPROVE = "approve"
    APPROVE_WITH_FINDINGS = "approve_with_findings"
    CHANGES_REQUESTED = "changes_requested"


class RequirementsSpec(VersionedModel):
    objective: str = Field(min_length=1)
    functional_requirements: list[str]
    non_functional_requirements: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str]
    technical_constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.MEDIUM


class PlanStep(VersionedModel):
    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class ImplementationPlan(VersionedModel):
    files_to_inspect: list[str] = Field(default_factory=list)
    files_to_modify: list[str]
    steps: list[PlanStep]
    tests: list[str]
    security_considerations: list[str] = Field(default_factory=list)
    regression_risks: list[str] = Field(default_factory=list)
    rollback: str = "Restore the recorded pre-patch snapshot."

    @model_validator(mode="after")
    def dependencies_exist(self) -> ImplementationPlan:
        ids = {step.id for step in self.steps}
        for step in self.steps:
            unknown = set(step.depends_on) - ids
            if unknown:
                raise ValueError(f"unknown dependency for {step.id}: {sorted(unknown)}")
        return self


class ContextSnippet(VersionedModel):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str
    score: float = Field(ge=0)
    reason: str


class PatchProposal(VersionedModel):
    summary: str = Field(min_length=1)
    diff: str = Field(min_length=1)

    @field_validator("diff")
    @classmethod
    def is_unified_diff(cls, value: str) -> str:
        if "--- " not in value or "+++ " not in value or "@@" not in value:
            raise ValueError("patch must be a unified diff")
        return value


class CheckResult(VersionedModel):
    name: str
    argv: list[str]
    status: CheckStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(default=0, ge=0)
    reason: str | None = None


class ValidationReport(VersionedModel):
    checks: list[CheckResult]

    @computed_field
    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(
            check.status in {CheckStatus.PASSED, CheckStatus.SKIPPED} for check in self.checks
        )


class Finding(VersionedModel):
    severity: RiskLevel
    description: str
    evidence: str


class QAReview(VersionedModel):
    acceptance_criteria_coverage: float = Field(ge=0, le=1)
    test_quality_score: float = Field(ge=0, le=1)
    security_score: float = Field(ge=0, le=1)
    regression_risk: RiskLevel
    findings: list[Finding] = Field(default_factory=list)
    merge_recommendation: MergeRecommendation


class ApprovalRecord(VersionedModel):
    kind: ApprovalKind
    decision: Decision
    feedback: str | None = None
    decided_at: datetime = Field(default_factory=utc_now)


class ProviderUsage(VersionedModel):
    model: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    request_id: str | None = None
    latency_seconds: float = Field(default=0, ge=0)


class Budget(VersionedModel):
    max_repairs: int = Field(default=2, ge=0, le=2)
    max_tokens: int = Field(default=200_000, gt=0)
    max_cost_usd: Decimal = Field(default=Decimal("20.00"), gt=0)
    timeout_seconds: int = Field(default=1800, gt=0)


class RunManifest(VersionedModel):
    run_id: str
    request: str
    status: RunStatus = RunStatus.RUNNING
    stage: RunStage = RunStage.CREATED
    repair_attempts: int = Field(default=0, ge=0, le=2)
    token_usage: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    reason: str | None = None
