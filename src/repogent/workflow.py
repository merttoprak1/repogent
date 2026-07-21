from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel

from repogent.agents import RoleSet
from repogent.approvals import Approver
from repogent.artifacts import ArtifactStore
from repogent.candidates import (
    CandidateEvaluator,
    CandidatePolicy,
    CandidateSelector,
    ExpansionReason,
)
from repogent.domain import (
    ApprovalKind,
    Budget,
    CandidateEvidence,
    CandidateRecord,
    CandidateSelection,
    CheckoutState,
    CheckResult,
    CheckStatus,
    Decision,
    EventKind,
    FinalValidationStatus,
    ImplementationPlan,
    MergeRecommendation,
    ProviderCallEvidence,
    ProviderUsage,
    QAReview,
    RequirementsSpec,
    RunEvent,
    RunManifest,
    RunStage,
    RunStatus,
    ValidationReport,
    utc_now,
)
from repogent.events import EventSink
from repogent.localization import LocalizationReport, PythonLocalizer
from repogent.patching import PatchApplier, PatchPolicy
from repogent.provider_context import ProviderContextBuilder
from repogent.providers import ProviderError
from repogent.reporting import render_report
from repogent.repository import LexicalRetriever, RepositoryInspector, RepositoryInventory
from repogent.symbols import PythonSymbolGraph, PythonSymbolGraphBuilder


class IllegalTransition(ValueError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class Validator(Protocol):
    def run(self, root: Path, *, timeout_seconds: float | None = None) -> ValidationReport: ...


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
    RunStage.VALIDATED: {RunStage.REVIEWED},
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
    budget: Budget
    # Kept optional for the two public constructors from v0.1 while v0.2 callers
    # should provide the explicit Phase 2 collaborators.
    retriever: LexicalRetriever | None = None
    symbol_builder: PythonSymbolGraphBuilder | None = None
    localizer: PythonLocalizer | None = None
    candidate_evaluator: CandidateEvaluator | None = None
    candidate_policy: CandidatePolicy | None = None
    candidate_selector: CandidateSelector | None = None
    events: EventSink | None = None
    context_builder: ProviderContextBuilder | None = None
    requirements: RequirementsSpec | None = field(default=None, init=False)
    plan: ImplementationPlan | None = field(default=None, init=False)
    validation: ValidationReport | None = field(default=None, init=False)
    review: QAReview | None = field(default=None, init=False)
    localization: LocalizationReport | None = field(default=None, init=False)
    candidates: list[CandidateRecord] = field(default_factory=list, init=False)
    candidate_evidence: list[CandidateEvidence] = field(default_factory=list, init=False)
    selection: CandidateSelection | None = field(default=None, init=False)
    started_at: float = field(default=0, init=False)
    deadline: float = field(default=0, init=False)
    elapsed_seconds: float = field(default=0, init=False)
    _sequence: int = field(default=0, init=False)
    _event_failed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.symbol_builder = self.symbol_builder or PythonSymbolGraphBuilder()
        self.localizer = self.localizer or PythonLocalizer()
        self.candidate_evaluator = self.candidate_evaluator or CandidateEvaluator(
            self.patch_policy, self.patch_applier, self.validator
        )
        self.candidate_policy = self.candidate_policy or CandidatePolicy()
        self.candidate_selector = self.candidate_selector or CandidateSelector()
        self.events = self.events or self.artifacts.event_store()
        self.context_builder = self.context_builder or ProviderContextBuilder()
        if self.manifest.events_file is None:
            self.manifest = self.manifest.model_copy(update={"events_file": "events.jsonl"})

    def run(self) -> RunManifest:
        self.started_at = time.monotonic()
        self.deadline = self.started_at + self.budget.timeout_seconds
        status: RunStatus
        reason: str | None
        try:
            self.artifacts.update_manifest(self.manifest)
            self.emit(EventKind.WARNING, "workflow started")
            status, reason = self._execute()
            if status in {
                RunStatus.COMPLETED,
                RunStatus.COMPLETED_WITH_FINDINGS,
                RunStatus.CHANGES_REQUESTED,
            }:
                self.ensure_time()
        except (KeyboardInterrupt, SystemExit):
            self._mark_post_apply_interrupted()
            status = RunStatus.CANCELLED
            reason = "workflow interrupted by user"
        except ProviderError as error:
            self._mark_post_apply_interrupted()
            if error.evidence is not None:
                try:
                    self.artifacts.write_model("provider-failure", error.evidence)
                except Exception as persistence_error:
                    status = RunStatus.HUMAN_INTERVENTION_REQUIRED
                    reason = str(persistence_error)
                else:
                    status = RunStatus.HUMAN_INTERVENTION_REQUIRED
                    reason = str(error)
            else:
                status = RunStatus.HUMAN_INTERVENTION_REQUIRED
                reason = str(error)
        except Exception as error:
            self._mark_post_apply_interrupted()
            status = RunStatus.HUMAN_INTERVENTION_REQUIRED
            reason = str(error)
        try:
            return self.finish(status, reason)
        except (KeyboardInterrupt, SystemExit):
            self._mark_post_apply_interrupted()
            return self._terminalize_without_event(
                RunStatus.CANCELLED, "workflow interrupted during terminalization"
            )
        except Exception as error:
            self._mark_post_apply_interrupted()
            return self._terminalize_without_event(
                RunStatus.HUMAN_INTERVENTION_REQUIRED, str(error)
            )
        finally:
            self.elapsed_seconds = time.monotonic() - self.started_at

    def _execute(self) -> tuple[RunStatus, str | None]:
        inventory = self._inspect_repository()
        self.write("inventory", inventory)
        self.advance(RunStage.ANALYZED)
        symbol_builder = cast(PythonSymbolGraphBuilder, self.symbol_builder)
        graph = symbol_builder.build(inventory)
        self.write("symbol-graph", graph)
        self.emit(EventKind.MODEL, "repository graph built", node_count=len(graph.nodes))

        context_builder = cast(ProviderContextBuilder, self.context_builder)
        requirements_payload = context_builder.requirements(self.request, inventory)
        self._write_json("requirements-input", requirements_payload)
        requirements_result = self.roles.requirements.run(
            requirements_payload, timeout_seconds=self.remaining_time()
        )
        self.requirements = requirements_result.output
        self.write("requirements", self.requirements)
        self.account(
            requirements_result.usage,
            generated_artifact="requirements",
            evidence=requirements_result.evidence,
        )
        self.emit(EventKind.MODEL, "requirements generation completed", role="requirements")
        self.advance(RunStage.REQUIREMENTS)
        if not self.approve(ApprovalKind.REQUIREMENTS, self.requirements):
            return RunStatus.CANCELLED, "requirements rejected"
        self.advance(RunStage.REQUIREMENTS_APPROVED)

        self.localization = self._localize(inventory, graph)
        if not self.localization.locations:
            return RunStatus.HUMAN_INTERVENTION_REQUIRED, "no relevant localization found"

        plan_payload = context_builder.planning(self.requirements, self.localization)
        self._write_json("planning-input", plan_payload)
        plan_result = self.roles.planning.run(plan_payload, timeout_seconds=self.remaining_time())
        self.plan = plan_result.output
        self.write("plan", self.plan)
        self.account(
            plan_result.usage,
            generated_artifact="plan",
            evidence=plan_result.evidence,
        )
        self.emit(EventKind.MODEL, "planning generation completed", role="planning")
        self.advance(RunStage.PLANNED)
        if not self.approve(ApprovalKind.PLAN, self.plan):
            return RunStatus.CANCELLED, "implementation plan rejected"
        self.advance(RunStage.PLAN_APPROVED)

        candidate_evaluator = cast(CandidateEvaluator, self.candidate_evaluator)
        approval_baseline = candidate_evaluator.capture_baseline(
            self.root, deadline=self.deadline
        )
        self._evaluate_candidates(self.localization)
        if not approval_baseline.matches(self.root, deadline=self.deadline):
            raise RuntimeError("repository baseline changed before approval")
        candidate_selector = cast(CandidateSelector, self.candidate_selector)
        self.selection = candidate_selector.select(self.candidates, self.candidate_evidence)
        self.write("candidate-selection", self.selection)
        self.emit(
            EventKind.CANDIDATE,
            "candidate selection completed",
            selected_candidate_id=self.selection.selected_candidate_id,
            ambiguous=self.selection.ambiguous,
        )
        if self.selection.selected_candidate_id is None:
            reason = (
                "candidate evidence is ambiguous"
                if self.selection.ambiguous
                else self.selection.reason
            )
            return RunStatus.HUMAN_INTERVENTION_REQUIRED, reason

        selected = next(
            candidate
            for candidate in self.candidates
            if candidate.candidate_id == self.selection.selected_candidate_id
        )
        selected_evidence = next(
            item for item in self.candidate_evidence if item.candidate_id == selected.candidate_id
        )
        self.manifest = self.manifest.model_copy(
            update={
                "selected_candidate_id": selected.candidate_id,
                "repair_attempts": len(self.candidates) - 1,
                "updated_at": utc_now(),
            }
        )
        self.artifacts.update_manifest(self.manifest)
        self.advance(RunStage.PATCH_PROPOSED)
        approval_artifact = json.dumps(
            {
                "selected_candidate": selected.model_dump(mode="json"),
                "selection": self.selection.model_dump(mode="json"),
                "candidates": [
                    {
                        "candidate": candidate.model_dump(mode="json"),
                        "evidence": item.model_dump(mode="json"),
                        "selected": candidate.candidate_id == selected.candidate_id,
                    }
                    for candidate, item in zip(
                        self.candidates, self.candidate_evidence, strict=True
                    )
                ],
            },
            indent=2,
        )
        if not self.approve(ApprovalKind.PATCH, approval_artifact):
            return RunStatus.CANCELLED, "selected patch rejected"
        if not approval_baseline.matches(self.root, deadline=self.deadline):
            return (
                RunStatus.HUMAN_INTERVENTION_REQUIRED,
                "repository baseline changed after approval",
            )
        self.advance(RunStage.PATCH_APPROVED)

        self.ensure_time()
        validated = self.patch_policy.validate(self.root, selected.proposal)
        paths = [path.as_posix() for path in validated.touched_paths]
        pre_apply_baseline = candidate_evaluator.capture_baseline(
            self.root, deadline=self.deadline
        )
        self._mark_checkout_recovery_unknown(paths)
        try:
            self.artifacts.update_manifest(self.manifest)
        except (Exception, KeyboardInterrupt, SystemExit):
            self._mark_patch_not_applied()
            raise
        applied_state_durable = False
        try:
            self.patch_applier.apply(self.root, validated)
            self._mark_patch_applied(paths)
            self.artifacts.update_manifest(self.manifest)
            applied_state_durable = True
        except (Exception, KeyboardInterrupt, SystemExit):
            if not applied_state_durable:
                if pre_apply_baseline.matches(self.root, deadline=self.deadline):
                    self._mark_patch_not_applied()
                else:
                    self._mark_checkout_recovery_unknown(paths)
                # The durable write-ahead state remains conservative when this
                # best-effort refinement cannot be persisted.
                with suppress(Exception, KeyboardInterrupt, SystemExit):
                    self.artifacts.update_manifest(self.manifest)
            raise
        self.write("patch-applied", selected.proposal)
        self.advance(RunStage.PATCH_APPLIED)
        self._set_final_validation_status(FinalValidationStatus.RUNNING)
        self.artifacts.update_manifest(self.manifest)
        post_patch_baseline = candidate_evaluator.capture_baseline(
            self.root, deadline=self.deadline
        )
        self.validation, final_root_stable = candidate_evaluator.validate_isolated(
            self.root,
            timeout_seconds=self.remaining_time(),
            baseline=post_patch_baseline,
        )
        final_validation_status = (
            FinalValidationStatus.PASSED
            if final_root_stable
            and _same_required_results(selected_evidence.validation, self.validation)
            and self.validation.passed
            else FinalValidationStatus.FAILED
        )
        self._set_final_validation_status(final_validation_status)
        self.artifacts.update_manifest(self.manifest)
        self.write("validation", self.validation)
        self.emit(
            EventKind.VALIDATION,
            "final validation completed",
            **_validation_summary(self.validation),
        )
        self.advance(RunStage.VALIDATED)
        if not final_root_stable:
            return RunStatus.HUMAN_INTERVENTION_REQUIRED, "repository drift during final validation"
        if not _same_required_results(selected_evidence.validation, self.validation):
            return RunStatus.HUMAN_INTERVENTION_REQUIRED, "changed validation evidence"
        if not self.validation.passed:
            return (
                RunStatus.HUMAN_INTERVENTION_REQUIRED,
                "selected candidate failed final validation",
            )

        qa_payload = context_builder.qa(
            self.requirements,
            self.plan,
            selected,
            self.selection.reason,
            self.validation,
        )
        self._write_json("qa-input", qa_payload)
        review_result = self.roles.qa.run(qa_payload, timeout_seconds=self.remaining_time())
        self.review = review_result.output
        self.write("qa-review", self.review)
        self.account(
            review_result.usage,
            generated_artifact="qa-review",
            evidence=review_result.evidence,
        )
        self.emit(EventKind.MODEL, "QA generation completed", role="qa")
        self.advance(RunStage.REVIEWED)
        status = {
            MergeRecommendation.APPROVE: RunStatus.COMPLETED,
            MergeRecommendation.APPROVE_WITH_FINDINGS: RunStatus.COMPLETED_WITH_FINDINGS,
            MergeRecommendation.CHANGES_REQUESTED: RunStatus.CHANGES_REQUESTED,
        }[self.review.merge_recommendation]
        return status, None

    def _localize(
        self, inventory: RepositoryInventory, graph: PythonSymbolGraph
    ) -> LocalizationReport:
        if self.requirements is None:
            raise RuntimeError("requirements must be generated before localization")
        localizer = cast(PythonLocalizer, self.localizer)
        localization = localizer.localize(
            inventory, graph, self.request, self.requirements.acceptance_criteria
        )
        self.write("localization", localization)
        self.emit(
            EventKind.CANDIDATE,
            "repository localization completed",
            locations=len(localization.locations),
            ambiguous=localization.ambiguous,
        )
        if localization.locations and not localization.ambiguous:
            return localization
        broader = PythonLocalizer(
            max_snippets=localizer.max_snippets * 2,
            max_total_chars=localizer.max_total_chars * 2,
        ).localize(
            inventory,
            graph,
            self.request,
            self.requirements.acceptance_criteria,
            self.validation,
        )
        self.write("localization-broadened", broader)
        self.emit(
            EventKind.WARNING,
            "broader repository localization completed",
            locations=len(broader.locations),
            ambiguous=broader.ambiguous,
        )
        return broader

    def _evaluate_candidates(self, localization: LocalizationReport) -> None:
        if self.requirements is None or self.plan is None:
            raise RuntimeError("requirements and plan must exist before candidate evaluation")
        self.candidates = []
        self.candidate_evidence = []
        expansion_reason: str | None = None
        while len(self.candidates) < 3:
            candidate_id = f"candidate-{len(self.candidates) + 1}"
            previous = self.candidates[-1] if self.candidates else None
            previous_evidence = self.candidate_evidence[-1] if self.candidate_evidence else None
            role = self.roles.implementation if previous is None else self.roles.repair
            generation_reason = (
                "initial implementation" if previous is None else expansion_reason or "alternative"
            )
            context_builder = cast(ProviderContextBuilder, self.context_builder)
            payload = context_builder.candidate(
                self.requirements,
                self.plan,
                localization,
                candidate_id,
                previous=previous,
                previous_evidence=previous_evidence,
                generation_reason=generation_reason if previous is not None else None,
            )
            self._write_json("candidate-input", payload)
            result = role.run(payload, timeout_seconds=self.remaining_time())
            candidate = CandidateRecord(
                candidate_id=candidate_id,
                proposal=result.output,
                parent_candidate_id=previous.candidate_id if previous else None,
                generation_reason=generation_reason,
                diff_sha256=hashlib.sha256(result.output.diff.encode()).hexdigest(),
                usage=result.usage,
            )
            self.candidates.append(candidate)
            self.manifest = self.manifest.model_copy(
                update={
                    "candidate_ids": [item.candidate_id for item in self.candidates],
                    "repair_attempts": len(self.candidates) - 1,
                    "updated_at": utc_now(),
                }
            )
            self.artifacts.update_manifest(self.manifest)
            self.write("candidate", candidate)
            self.account(
                result.usage,
                generated_artifact="candidate",
                evidence=result.evidence,
            )
            self.emit(EventKind.CANDIDATE, "candidate generated", candidate_id=candidate_id)
            candidate_evaluator = cast(CandidateEvaluator, self.candidate_evaluator)
            candidate_evidence = candidate_evaluator.evaluate(
                self.root,
                candidate,
                self.requirements.acceptance_criteria,
                self.remaining_time(),
            )
            self.candidate_evidence.append(candidate_evidence)
            self.write("candidate-evidence", candidate_evidence)
            self.emit(
                EventKind.VALIDATION,
                "candidate validation completed",
                candidate_id=candidate_id,
                **_validation_summary(candidate_evidence.validation, candidate.usage),
                restored_to_baseline=candidate_evidence.restored_to_baseline,
            )
            if not candidate_evidence.restored_to_baseline:
                raise RuntimeError("candidate evaluation did not restore repository baseline")
            candidate_policy = cast(CandidatePolicy, self.candidate_policy)
            expansion = candidate_policy.should_expand(
                localization, candidate_evidence, len(self.candidates)
            )
            if expansion is None and localization.ambiguous and len(self.candidates) == 1:
                # The one bounded broader pass still left competing locations.  Gather
                # one independent alternative even when a caller configured a one-shot
                # policy, then let evidence decide rather than silently applying a tie.
                expansion = ExpansionReason.AMBIGUOUS_LOCALIZATION
            if expansion is None:
                break
            expansion_reason = expansion.value

    def ensure_time(self) -> None:
        self.remaining_time()

    def remaining_time(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("workflow timeout exceeded")
        return remaining

    def _inspect_repository(self) -> RepositoryInventory:
        self.ensure_time()
        if _accepts_keyword(self.inspector.inspect, "deadline"):
            return self.inspector.inspect(self.root, deadline=self.deadline)
        return self.inspector.inspect(self.root)

    def _run_validation(self) -> ValidationReport:
        remaining = self.remaining_time()
        if _accepts_keyword(self.validator.run, "timeout_seconds"):
            run_with_timeout = cast(Callable[..., ValidationReport], self.validator.run)
            return run_with_timeout(self.root, timeout_seconds=remaining)
        return self.validator.run(self.root)

    def emit(self, kind: EventKind, message: str, **data: object) -> None:
        self._sequence += 1
        events = cast(EventSink, self.events)
        try:
            events.emit(
                RunEvent(
                    run_id=self.manifest.run_id,
                    sequence=self._sequence,
                    kind=kind,
                    stage=self.manifest.stage.value,
                    message=message,
                    data=data,
                )
            )
        except Exception:
            self._event_failed = True
            raise

    def advance(self, stage: RunStage) -> None:
        self.manifest = self.manifest.model_copy(
            update={"stage": transition(self.manifest.stage, stage), "updated_at": utc_now()}
        )
        self.artifacts.update_manifest(self.manifest)
        self.emit(EventKind.STAGE, "workflow stage changed", stage=stage.value)

    def write(self, name: str, model: BaseModel) -> None:
        self.artifacts.write_model(name, model)

    def _write_json(self, name: str, payload: Mapping[str, object]) -> None:
        self.artifacts.write_text(name, json.dumps(payload, indent=2, default=str))

    def approve(self, kind: ApprovalKind, artifact: BaseModel | str) -> bool:
        record = self.approver.decide(kind, artifact)
        self.artifacts.write_model("approval", record)
        self.emit(
            EventKind.APPROVAL,
            "approval decision recorded",
            approval_kind=kind.value,
            decision=record.decision.value,
        )
        return record.decision is Decision.APPROVED

    def account(
        self,
        usage: ProviderUsage,
        *,
        generated_artifact: str | None = None,
        evidence: ProviderCallEvidence | None = None,
    ) -> None:
        tokens = self.manifest.token_usage + usage.input_tokens + usage.output_tokens
        cost = self.manifest.estimated_cost_usd + usage.estimated_cost_usd
        self.manifest = self.manifest.model_copy(
            update={"token_usage": tokens, "estimated_cost_usd": cost, "updated_at": utc_now()}
        )
        self.artifacts.write_model("provider-usage", usage)
        if evidence is not None:
            self.artifacts.write_model("provider-call", evidence)
        self.artifacts.update_manifest(self.manifest)
        exceeded_reason: str | None = None
        if tokens > self.budget.max_tokens:
            exceeded_reason = "token budget exceeded"
        elif cost > self.budget.max_cost_usd:
            exceeded_reason = "estimated cost budget exceeded"
        if exceeded_reason is not None and generated_artifact is not None:
            generated = list(self.manifest.generated_but_not_consumed)
            if generated_artifact not in generated:
                generated.append(generated_artifact)
            self.manifest = self.manifest.model_copy(
                update={"generated_but_not_consumed": generated, "updated_at": utc_now()}
            )
            self.artifacts.update_manifest(self.manifest)
        if exceeded_reason == "token budget exceeded":
            raise BudgetExceeded("token budget exceeded")
        if exceeded_reason is not None:
            raise BudgetExceeded("estimated cost budget exceeded")

    def _mark_patch_applied(self, paths: list[str]) -> None:
        joined = ", ".join(paths) or "the affected paths"
        self.manifest = self.manifest.model_copy(
            update={
                "selected_patch_applied": True,
                "checkout_state": CheckoutState.APPLIED,
                "applied_paths": paths,
                "final_validation_status": FinalValidationStatus.NOT_STARTED,
                "recovery_guidance": (
                    f"Review {joined}, run the required validation commands, and revert the "
                    "approved patch manually if it should not remain."
                ),
                "updated_at": utc_now(),
            }
        )

    def _mark_checkout_recovery_unknown(self, paths: list[str]) -> None:
        joined = ", ".join(paths) or "the affected paths"
        self.manifest = self.manifest.model_copy(
            update={
                "selected_patch_applied": False,
                "checkout_state": CheckoutState.RECOVERY_UNKNOWN,
                "applied_paths": paths,
                "final_validation_status": FinalValidationStatus.INTERRUPTED,
                "recovery_guidance": (
                    f"Stop and manually inspect and restore {joined} before continuing; "
                    "Repogent could not prove whether the attempted patch was rolled back."
                ),
                "updated_at": utc_now(),
            }
        )

    def _mark_patch_not_applied(self) -> None:
        self.manifest = self.manifest.model_copy(
            update={
                "selected_patch_applied": False,
                "checkout_state": CheckoutState.NOT_APPLIED,
                "applied_paths": [],
                "final_validation_status": FinalValidationStatus.NOT_STARTED,
                "recovery_guidance": None,
                "updated_at": utc_now(),
            }
        )

    def _set_final_validation_status(self, status: FinalValidationStatus) -> None:
        paths = ", ".join(self.manifest.applied_paths) or "the affected paths"
        guidance = self.manifest.recovery_guidance
        if self.manifest.selected_patch_applied:
            if status is FinalValidationStatus.PASSED:
                guidance = (
                    f"Review the terminal reason and evidence for {paths}; deterministic final "
                    "validation passed. Revert the approved patch manually if it should not remain."
                )
            elif status is FinalValidationStatus.FAILED:
                guidance = (
                    f"Review {paths}, fix the reported validation failure, and run the required "
                    "validation commands again; revert the approved patch manually if it should "
                    "not remain."
                )
            elif status is FinalValidationStatus.INTERRUPTED:
                guidance = (
                    f"Review {paths} and run every required validation command because final "
                    "validation was interrupted; revert the approved patch manually if it should "
                    "not remain."
                )
        self.manifest = self.manifest.model_copy(
            update={
                "final_validation_status": status,
                "recovery_guidance": guidance,
                "updated_at": utc_now(),
            }
        )

    def _mark_post_apply_interrupted(self) -> None:
        if self.manifest.selected_patch_applied and self.manifest.final_validation_status in {
            FinalValidationStatus.NOT_STARTED,
            FinalValidationStatus.RUNNING,
        }:
            self._set_final_validation_status(FinalValidationStatus.INTERRUPTED)

    def finish(self, status: RunStatus, reason: str | None) -> RunManifest:
        self._set_final_manifest(status, reason)
        persistence_error = self._persist_final_manifest()
        if persistence_error is not None:
            return self._downgrade_finalization(str(persistence_error))
        report_error = self._write_final_report()
        if report_error is not None:
            return self._downgrade_finalization(str(report_error))
        if self._event_failed:
            return self.manifest
        try:
            self.emit(
                EventKind.TERMINAL,
                "workflow finished",
                status=self.manifest.status.value,
                reason=self.manifest.reason,
            )
        except Exception as error:
            if self.manifest.status is not RunStatus.HUMAN_INTERVENTION_REQUIRED:
                return self._downgrade_finalization(str(error))
        return self.manifest

    def _downgrade_finalization(self, reason: str) -> RunManifest:
        self._set_final_manifest(RunStatus.HUMAN_INTERVENTION_REQUIRED, reason)
        persistence_error = self._persist_final_manifest()
        if persistence_error is not None:
            self._set_final_manifest(
                RunStatus.HUMAN_INTERVENTION_REQUIRED,
                f"{reason}; final downgrade persistence failed: {persistence_error}",
            )
            self._persist_final_manifest()
        report_error = self._write_final_report()
        if report_error is not None:
            self._set_final_manifest(RunStatus.HUMAN_INTERVENTION_REQUIRED, str(report_error))
            self._persist_final_manifest()
            self._write_final_report()
        if not self._event_failed:
            try:
                self.emit(
                    EventKind.TERMINAL,
                    "workflow finished",
                    status=self.manifest.status.value,
                    reason=self.manifest.reason,
                )
            except Exception:
                return self.manifest
        return self.manifest

    def _set_final_manifest(self, status: RunStatus, reason: str | None) -> None:
        final_stage = (
            self.manifest.stage
            if self.manifest.stage is RunStage.FINISHED
            else transition(self.manifest.stage, RunStage.FINISHED)
        )
        self.manifest = self.manifest.model_copy(
            update={
                "status": status,
                "stage": final_stage,
                "reason": reason,
                "updated_at": utc_now(),
            }
        )

    def _write_final_report(self) -> Exception | None:
        try:
            self.artifacts.write_final(
                "report.md",
                render_report(
                    self.manifest,
                    self.requirements,
                    self.plan,
                    self.validation,
                    self.review,
                    localization=self.localization,
                    candidates=self._report_candidates(),
                    selection=self.selection,
                ),
            )
        except Exception as error:
            return error
        return None

    def _report_candidates(self) -> tuple[tuple[CandidateRecord, CandidateEvidence | None], ...]:
        """Pair durable candidate records with first matching evidence for terminal reports."""
        known_ids = {candidate.candidate_id for candidate in self.candidates}
        evidence_by_id: dict[str, CandidateEvidence] = {}
        for evidence in self.candidate_evidence:
            if evidence.candidate_id in known_ids and evidence.candidate_id not in evidence_by_id:
                evidence_by_id[evidence.candidate_id] = evidence
        return tuple(
            (candidate, evidence_by_id.get(candidate.candidate_id))
            for candidate in self.candidates
        )

    def _persist_final_manifest(self) -> Exception | None:
        try:
            self.artifacts.update_manifest(self.manifest)
        except Exception as error:
            return error
        return None

    def _terminalize_without_event(
        self, status: RunStatus, reason: str
    ) -> RunManifest:
        """Last-resort, non-recursive durability path after terminalization itself fails."""
        self._set_final_manifest(status, reason)
        try:
            self.artifacts.update_manifest(self.manifest)
        except (Exception, KeyboardInterrupt, SystemExit):
            return self.manifest
        try:
            self._write_final_report()
        except (KeyboardInterrupt, SystemExit):
            return self.manifest
        return self.manifest


def _same_required_results(expected: ValidationReport, actual: ValidationReport) -> bool:
    return [_check_payload(item) for item in expected.checks if item.required] == [
        _check_payload(item) for item in actual.checks if item.required
    ]


def _validation_summary(
    validation: ValidationReport, usage: ProviderUsage | None = None
) -> dict[str, object]:
    summary: dict[str, object] = {
        "passed": sum(check.status is CheckStatus.PASSED for check in validation.checks),
        "failed": sum(
            check.status in {CheckStatus.FAILED, CheckStatus.TIMED_OUT}
            for check in validation.checks
        ),
        "skipped": sum(check.status is CheckStatus.SKIPPED for check in validation.checks),
    }
    if usage is not None:
        summary["cost_usd"] = str(usage.estimated_cost_usd)
    return summary


def _check_payload(check: CheckResult) -> dict[str, object]:
    # Command output and elapsed time are inherently variable (for example pytest's
    # duration line). Candidate evidence compares the deterministic required result.
    return check.model_dump(
        exclude={"duration_seconds", "schema_version", "stdout", "stderr"}
    )


def _accepts_keyword(callable_object: object, name: str) -> bool:
    try:
        parameters = inspect.signature(callable_object).parameters.values()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
