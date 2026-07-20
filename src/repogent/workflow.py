from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from repogent.agents import RoleSet
from repogent.approvals import Approver
from repogent.artifacts import ArtifactStore
from repogent.domain import (
    ApprovalKind,
    Budget,
    Decision,
    ImplementationPlan,
    MergeRecommendation,
    ProviderUsage,
    QAReview,
    RequirementsSpec,
    RunManifest,
    RunStage,
    RunStatus,
    ValidationReport,
    utc_now,
)
from repogent.patching import PatchApplier, PatchPolicy
from repogent.reporting import render_report
from repogent.repository import LexicalRetriever, RepositoryInspector


class IllegalTransition(ValueError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class Validator(Protocol):
    def run(self, root: Path) -> ValidationReport: ...


LEGAL_TRANSITIONS: dict[RunStage, set[RunStage]] = {
    RunStage.CREATED: {RunStage.ANALYZED},
    RunStage.ANALYZED: {RunStage.REQUIREMENTS},
    RunStage.REQUIREMENTS: {RunStage.REQUIREMENTS_APPROVED},
    RunStage.REQUIREMENTS_APPROVED: {RunStage.PLANNED},
    RunStage.PLANNED: {RunStage.PLAN_APPROVED},
    RunStage.PLAN_APPROVED: {RunStage.PATCH_PROPOSED},
    RunStage.PATCH_PROPOSED: {RunStage.PATCH_APPROVED},
    RunStage.PATCH_APPROVED: {RunStage.PATCH_APPLIED},
    RunStage.PATCH_APPLIED: {RunStage.VALIDATED},
    RunStage.VALIDATED: {RunStage.REPAIRING, RunStage.REVIEWED},
    RunStage.REPAIRING: {RunStage.PATCH_PROPOSED},
    RunStage.REVIEWED: {RunStage.FINISHED},
}


def transition(current: RunStage, requested: RunStage) -> RunStage:
    if requested is RunStage.FINISHED and current is not RunStage.FINISHED:
        return requested
    if requested not in LEGAL_TRANSITIONS.get(current, set()):
        raise IllegalTransition(f"illegal transition: {current.value} -> {requested.value}")
    return requested


@dataclass
class Workflow:
    root: Path
    request: str
    manifest: RunManifest
    roles: RoleSet
    approver: Approver
    patch_policy: PatchPolicy
    patch_applier: PatchApplier
    validator: Validator
    artifacts: ArtifactStore
    inspector: RepositoryInspector
    retriever: LexicalRetriever
    budget: Budget
    requirements: RequirementsSpec | None = field(default=None, init=False)
    plan: ImplementationPlan | None = field(default=None, init=False)
    validation: ValidationReport | None = field(default=None, init=False)
    review: QAReview | None = field(default=None, init=False)
    started_at: float = field(default=0, init=False)
    elapsed_seconds: float = field(default=0, init=False)

    def run(self) -> RunManifest:
        self.started_at = time.monotonic()
        try:
            try:
                self.artifacts.update_manifest(self.manifest)
                status, reason = self._execute()
                if status in {
                    RunStatus.COMPLETED,
                    RunStatus.COMPLETED_WITH_FINDINGS,
                    RunStatus.CHANGES_REQUESTED,
                }:
                    self.ensure_time()
            except Exception as error:
                status = RunStatus.HUMAN_INTERVENTION_REQUIRED
                reason = str(error)
            return self.finish(status, reason)
        finally:
            self.elapsed_seconds = time.monotonic() - self.started_at

    def _execute(self) -> tuple[RunStatus, str | None]:
        inventory = self.inspector.inspect(self.root)
        self.write("inventory", inventory)
        self.advance(RunStage.ANALYZED)
        context = self.retriever.retrieve(inventory, self.request)
        context_payload = [item.model_dump() for item in context]
        self.artifacts.write_text("context", json.dumps(context_payload, indent=2))

        self.ensure_time()
        requirements_payload = {
            "request": self.request,
            "repository_context": context_payload,
        }
        self.artifacts.write_text(
            "requirements-input", json.dumps(requirements_payload, indent=2)
        )
        requirements_result = self.roles.requirements.run(requirements_payload)
        self.requirements = requirements_result.output
        self.account(requirements_result.usage)
        self.ensure_time()
        self.write("requirements", self.requirements)
        self.advance(RunStage.REQUIREMENTS)
        requirements_approved = self.approve(ApprovalKind.REQUIREMENTS, self.requirements)
        self.ensure_time()
        if not requirements_approved:
            return RunStatus.CANCELLED, "requirements rejected"
        self.advance(RunStage.REQUIREMENTS_APPROVED)

        self.ensure_time()
        plan_payload = {
            "requirements": self.requirements.model_dump(),
            "repository_context": context_payload,
        }
        self.artifacts.write_text("planning-input", json.dumps(plan_payload, indent=2))
        plan_result = self.roles.planning.run(plan_payload)
        self.plan = plan_result.output
        self.account(plan_result.usage)
        self.ensure_time()
        self.write("plan", self.plan)
        self.advance(RunStage.PLANNED)
        plan_approved = self.approve(ApprovalKind.PLAN, self.plan)
        self.ensure_time()
        if not plan_approved:
            return RunStatus.CANCELLED, "implementation plan rejected"
        self.advance(RunStage.PLAN_APPROVED)

        self.ensure_time()
        implementation_payload = {
            "requirements": self.requirements.model_dump(),
            "plan": self.plan.model_dump(),
            "repository_context": context_payload,
        }
        self.artifacts.write_text(
            "implementation-input", json.dumps(implementation_payload, indent=2)
        )
        patch_result = self.roles.implementation.run(implementation_payload)
        self.account(patch_result.usage)
        self.ensure_time()
        current_patch = self.patch_policy.validate(self.root, patch_result.output)
        self.write("patch-proposal", patch_result.output)
        self.advance(RunStage.PATCH_PROPOSED)
        patch_approved = self.approve(ApprovalKind.PATCH, patch_result.output)
        self.ensure_time()
        if not patch_approved:
            return RunStatus.CANCELLED, "patch rejected"
        self.write("patch-approved", patch_result.output)
        self.advance(RunStage.PATCH_APPROVED)
        self.ensure_time()
        self.patch_applier.apply(self.root, current_patch)
        applied_diffs = [current_patch.proposal.diff]
        self.write("patch-applied", patch_result.output)
        self.advance(RunStage.PATCH_APPLIED)

        self.ensure_time()
        self.validation = self.validator.run(self.root)
        self.ensure_time()
        self.write("validation", self.validation)
        self.advance(RunStage.VALIDATED)
        while (
            not self.validation.passed
            and self.manifest.repair_attempts < self.budget.max_repairs
        ):
            self.manifest = self.manifest.model_copy(
                update={"repair_attempts": self.manifest.repair_attempts + 1}
            )
            self.advance(RunStage.REPAIRING)
            self.ensure_time()
            repair_payload = {
                "requirements": self.requirements.model_dump(),
                "plan": self.plan.model_dump(),
                "failed_validation": self.validation.model_dump(),
            }
            self.artifacts.write_text("repair-input", json.dumps(repair_payload, indent=2))
            repair_result = self.roles.repair.run(repair_payload)
            self.account(repair_result.usage)
            self.ensure_time()
            current_patch = self.patch_policy.validate(self.root, repair_result.output)
            self.write("repair-patch", repair_result.output)
            self.advance(RunStage.PATCH_PROPOSED)
            repair_approved = self.approve(ApprovalKind.REPAIR_PATCH, repair_result.output)
            self.ensure_time()
            if not repair_approved:
                return RunStatus.CANCELLED, "repair patch rejected"
            self.write("repair-patch-approved", repair_result.output)
            self.advance(RunStage.PATCH_APPROVED)
            self.ensure_time()
            self.patch_applier.apply(self.root, current_patch)
            applied_diffs.append(current_patch.proposal.diff)
            self.write("repair-patch-applied", repair_result.output)
            self.advance(RunStage.PATCH_APPLIED)
            self.ensure_time()
            self.validation = self.validator.run(self.root)
            self.ensure_time()
            self.write("validation", self.validation)
            self.advance(RunStage.VALIDATED)

        if not self.validation.passed:
            attempts = self.manifest.repair_attempts
            return (
                RunStatus.HUMAN_INTERVENTION_REQUIRED,
                f"validation failed after {attempts} repair attempts "
                f"(repair budget: {self.budget.max_repairs})",
            )

        self.ensure_time()
        qa_payload = {
            "requirements": self.requirements.model_dump(),
            "plan": self.plan.model_dump(),
            "validation": self.validation.model_dump(),
            "diff": "\n".join(applied_diffs),
        }
        self.artifacts.write_text("qa-input", json.dumps(qa_payload, indent=2))
        review_result = self.roles.qa.run(qa_payload)
        self.review = review_result.output
        self.account(review_result.usage)
        self.ensure_time()
        self.write("qa-review", self.review)
        self.advance(RunStage.REVIEWED)
        status = {
            MergeRecommendation.APPROVE: RunStatus.COMPLETED,
            MergeRecommendation.APPROVE_WITH_FINDINGS: RunStatus.COMPLETED_WITH_FINDINGS,
            MergeRecommendation.CHANGES_REQUESTED: RunStatus.CHANGES_REQUESTED,
        }[self.review.merge_recommendation]
        return status, None

    def ensure_time(self) -> None:
        if time.monotonic() - self.started_at > self.budget.timeout_seconds:
            raise TimeoutError("workflow timeout exceeded")

    def advance(self, stage: RunStage) -> None:
        self.manifest = self.manifest.model_copy(
            update={"stage": transition(self.manifest.stage, stage), "updated_at": utc_now()}
        )
        self.artifacts.update_manifest(self.manifest)

    def write(self, name: str, model: BaseModel) -> None:
        self.artifacts.write_model(name, model)

    def approve(self, kind: ApprovalKind, artifact: BaseModel | str) -> bool:
        record = self.approver.decide(kind, artifact)
        self.artifacts.write_model("approval", record)
        return record.decision is Decision.APPROVED

    def account(self, usage: ProviderUsage) -> None:
        tokens = self.manifest.token_usage + usage.input_tokens + usage.output_tokens
        cost = self.manifest.estimated_cost_usd + usage.estimated_cost_usd
        self.manifest = self.manifest.model_copy(
            update={"token_usage": tokens, "estimated_cost_usd": cost, "updated_at": utc_now()}
        )
        self.artifacts.write_model("provider-usage", usage)
        self.artifacts.update_manifest(self.manifest)
        if tokens > self.budget.max_tokens:
            raise BudgetExceeded("token budget exceeded")
        if cost > self.budget.max_cost_usd:
            raise BudgetExceeded("estimated cost budget exceeded")

    def finish(self, status: RunStatus, reason: str | None) -> RunManifest:
        final_stage = transition(self.manifest.stage, RunStage.FINISHED)
        self.manifest = self.manifest.model_copy(
            update={
                "status": status,
                "stage": final_stage,
                "reason": reason,
                "updated_at": utc_now(),
            }
        )
        self.artifacts.update_manifest(self.manifest)
        report = render_report(
            self.manifest, self.requirements, self.plan, self.validation, self.review
        )
        self.artifacts.write_final("report.md", report)
        return self.manifest
