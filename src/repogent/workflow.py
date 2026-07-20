from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Callable, Mapping
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
    CheckResult,
    Decision,
    EventKind,
    ImplementationPlan,
    MergeRecommendation,
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
    requirements: RequirementsSpec | None = field(default=None, init=False)
    plan: ImplementationPlan | None = field(default=None, init=False)
    validation: ValidationReport | None = field(default=None, init=False)
    review: QAReview | None = field(default=None, init=False)
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
        except Exception as error:
            status = RunStatus.HUMAN_INTERVENTION_REQUIRED
            reason = str(error)
        try:
            return self.finish(status, reason)
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

        requirements_payload = {
            "request": self.request,
            "repository_inventory": inventory.model_dump(),
        }
        self._write_json("requirements-input", requirements_payload)
        requirements_result = self.roles.requirements.run(
            requirements_payload, timeout_seconds=self.remaining_time()
        )
        self.account(requirements_result.usage)
        self.requirements = requirements_result.output
        self.write("requirements", self.requirements)
        self.emit(EventKind.MODEL, "requirements generation completed", role="requirements")
        self.advance(RunStage.REQUIREMENTS)
        if not self.approve(ApprovalKind.REQUIREMENTS, self.requirements):
            return RunStatus.CANCELLED, "requirements rejected"
        self.advance(RunStage.REQUIREMENTS_APPROVED)

        localization = self._localize(inventory, graph)
        if not localization.locations:
            return RunStatus.HUMAN_INTERVENTION_REQUIRED, "no relevant localization found"

        plan_payload = {
            "requirements": self.requirements.model_dump(),
            "localization": localization.model_dump(),
            "localized_snippets": [snippet.model_dump() for snippet in localization.snippets],
        }
        self._write_json("planning-input", plan_payload)
        plan_result = self.roles.planning.run(plan_payload, timeout_seconds=self.remaining_time())
        self.account(plan_result.usage)
        self.plan = plan_result.output
        self.write("plan", self.plan)
        self.emit(EventKind.MODEL, "planning generation completed", role="planning")
        self.advance(RunStage.PLANNED)
        if not self.approve(ApprovalKind.PLAN, self.plan):
            return RunStatus.CANCELLED, "implementation plan rejected"
        self.advance(RunStage.PLAN_APPROVED)

        candidate_evaluator = cast(CandidateEvaluator, self.candidate_evaluator)
        approval_baseline = candidate_evaluator.capture_baseline(self.root)
        candidates, evidence = self._evaluate_candidates(localization)
        if not approval_baseline.matches(self.root, self.patch_applier):
            try:
                approval_baseline.restore(self.root, self.patch_applier)
            except Exception as error:
                raise RuntimeError(
                    "repository baseline changed before approval and could not be restored"
                ) from error
            raise RuntimeError("repository baseline changed before approval")
        candidate_selector = cast(CandidateSelector, self.candidate_selector)
        selection = candidate_selector.select(candidates, evidence)
        self.write("candidate-selection", selection)
        self.emit(
            EventKind.CANDIDATE,
            "candidate selection completed",
            selected_candidate_id=selection.selected_candidate_id,
            ambiguous=selection.ambiguous,
        )
        if selection.selected_candidate_id is None:
            reason = "candidate evidence is ambiguous" if selection.ambiguous else selection.reason
            return RunStatus.HUMAN_INTERVENTION_REQUIRED, reason

        selected = next(
            candidate
            for candidate in candidates
            if candidate.candidate_id == selection.selected_candidate_id
        )
        selected_evidence = next(
            item for item in evidence if item.candidate_id == selected.candidate_id
        )
        self.manifest = self.manifest.model_copy(
            update={
                "selected_candidate_id": selected.candidate_id,
                "repair_attempts": len(candidates) - 1,
                "updated_at": utc_now(),
            }
        )
        self.artifacts.update_manifest(self.manifest)
        self.advance(RunStage.PATCH_PROPOSED)
        approval_artifact = json.dumps(
            {
                "selected_candidate": selected.model_dump(mode="json"),
                "selection": selection.model_dump(mode="json"),
                "candidates": [
                    {
                        "candidate": candidate.model_dump(mode="json"),
                        "evidence": item.model_dump(mode="json"),
                        "selected": candidate.candidate_id == selected.candidate_id,
                    }
                    for candidate, item in zip(candidates, evidence, strict=True)
                ],
            },
            indent=2,
        )
        if not self.approve(ApprovalKind.PATCH, approval_artifact):
            return RunStatus.CANCELLED, "selected patch rejected"
        self.advance(RunStage.PATCH_APPROVED)

        self.ensure_time()
        validated = self.patch_policy.validate(self.root, selected.proposal)
        self.patch_applier.apply(self.root, validated)
        self.write("patch-applied", selected.proposal)
        self.advance(RunStage.PATCH_APPLIED)
        self.validation = self._run_validation()
        self.write("validation", self.validation)
        self.emit(EventKind.VALIDATION, "final validation completed", passed=self.validation.passed)
        self.advance(RunStage.VALIDATED)
        if not _same_required_results(selected_evidence.validation, self.validation):
            return RunStatus.HUMAN_INTERVENTION_REQUIRED, "changed validation evidence"
        if not self.validation.passed:
            return (
                RunStatus.HUMAN_INTERVENTION_REQUIRED,
                "selected candidate failed final validation",
            )

        qa_payload = {
            "requirements": self.requirements.model_dump(),
            "plan": self.plan.model_dump(),
            "acceptance_criteria": self.requirements.acceptance_criteria,
            "selected_candidate": selected.model_dump(mode="json"),
            "selection_reason": selection.reason,
            "final_validation": self.validation.model_dump(mode="json"),
            "diff": selected.proposal.diff,
        }
        self._write_json("qa-input", qa_payload)
        review_result = self.roles.qa.run(qa_payload, timeout_seconds=self.remaining_time())
        self.account(review_result.usage)
        self.review = review_result.output
        self.write("qa-review", self.review)
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

    def _evaluate_candidates(
        self, localization: LocalizationReport
    ) -> tuple[list[CandidateRecord], list[CandidateEvidence]]:
        if self.requirements is None or self.plan is None:
            raise RuntimeError("requirements and plan must exist before candidate evaluation")
        candidates: list[CandidateRecord] = []
        evidence: list[CandidateEvidence] = []
        expansion_reason: str | None = None
        while len(candidates) < 3:
            candidate_id = f"candidate-{len(candidates) + 1}"
            previous = candidates[-1] if candidates else None
            previous_evidence = evidence[-1] if evidence else None
            payload: dict[str, object] = {
                "requirements": self.requirements.model_dump(),
                "plan": self.plan.model_dump(),
                "localization": localization.model_dump(),
                "localized_snippets": [snippet.model_dump() for snippet in localization.snippets],
                "candidate_id": candidate_id,
            }
            role = self.roles.implementation if previous is None else self.roles.repair
            generation_reason = (
                "initial implementation" if previous is None else expansion_reason or "alternative"
            )
            if previous is not None and previous_evidence is not None:
                payload["previous_candidate"] = previous.model_dump(mode="json")
                payload["previous_failure"] = previous_evidence.model_dump(mode="json")
                payload["generation_reason"] = generation_reason
            self._write_json("candidate-input", payload)
            result = role.run(payload, timeout_seconds=self.remaining_time())
            self.account(result.usage)
            candidate = CandidateRecord(
                candidate_id=candidate_id,
                proposal=result.output,
                parent_candidate_id=previous.candidate_id if previous else None,
                generation_reason=generation_reason,
                diff_sha256=hashlib.sha256(result.output.diff.encode()).hexdigest(),
                usage=result.usage,
            )
            candidates.append(candidate)
            self.manifest = self.manifest.model_copy(
                update={
                    "candidate_ids": [item.candidate_id for item in candidates],
                    "repair_attempts": len(candidates) - 1,
                    "updated_at": utc_now(),
                }
            )
            self.artifacts.update_manifest(self.manifest)
            self.write("candidate", candidate)
            self.emit(EventKind.CANDIDATE, "candidate generated", candidate_id=candidate_id)
            candidate_evaluator = cast(CandidateEvaluator, self.candidate_evaluator)
            candidate_evidence = candidate_evaluator.evaluate(
                self.root,
                candidate,
                self.requirements.acceptance_criteria,
                self.remaining_time(),
            )
            evidence.append(candidate_evidence)
            self.write("candidate-evidence", candidate_evidence)
            self.emit(
                EventKind.VALIDATION,
                "candidate validation completed",
                candidate_id=candidate_id,
                passed=candidate_evidence.validation.passed,
                restored_to_baseline=candidate_evidence.restored_to_baseline,
            )
            if not candidate_evidence.restored_to_baseline:
                raise RuntimeError("candidate evaluation did not restore repository baseline")
            candidate_policy = cast(CandidatePolicy, self.candidate_policy)
            expansion = candidate_policy.should_expand(
                localization, candidate_evidence, len(candidates)
            )
            if expansion is None and localization.ambiguous and len(candidates) == 1:
                # The one bounded broader pass still left competing locations.  Gather
                # one independent alternative even when a caller configured a one-shot
                # policy, then let evidence decide rather than silently applying a tie.
                expansion = ExpansionReason.AMBIGUOUS_LOCALIZATION
            if expansion is None:
                break
            expansion_reason = expansion.value
        return candidates, evidence

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
        self._set_final_manifest(status, reason)
        persistence_error = self._persist_final_manifest()
        if persistence_error is not None:
            self._set_final_manifest(RunStatus.HUMAN_INTERVENTION_REQUIRED, str(persistence_error))
            retry_error = self._persist_final_manifest()
            if retry_error is not None:
                self.manifest = self.manifest.model_copy(
                    update={
                        "reason": f"{persistence_error}; final downgrade persistence failed: "
                        f"{retry_error}",
                        "updated_at": utc_now(),
                    }
                )
            self._write_final_report()
            return self.manifest
        if not self._event_failed:
            try:
                self.emit(
                    EventKind.TERMINAL,
                    "workflow finished",
                    status=self.manifest.status.value,
                    reason=self.manifest.reason,
                )
            except Exception as error:
                if self.manifest.status is not RunStatus.HUMAN_INTERVENTION_REQUIRED:
                    self._set_final_manifest(RunStatus.HUMAN_INTERVENTION_REQUIRED, str(error))
                    downgrade_error = self._persist_final_manifest()
                    if downgrade_error is not None:
                        self.manifest = self.manifest.model_copy(
                            update={
                                "reason": f"{error}; final downgrade persistence failed: "
                                f"{downgrade_error}",
                                "updated_at": utc_now(),
                            }
                        )
        self._write_final_report()
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

    def _write_final_report(self) -> None:
        self.artifacts.write_final(
            "report.md",
            render_report(
                self.manifest,
                self.requirements,
                self.plan,
                self.validation,
                self.review,
            ),
        )
    def _persist_final_manifest(self) -> Exception | None:
        try:
            self.artifacts.update_manifest(self.manifest)
        except Exception as error:
            return error
        return None


def _same_required_results(expected: ValidationReport, actual: ValidationReport) -> bool:
    return [_check_payload(item) for item in expected.checks if item.required] == [
        _check_payload(item) for item in actual.checks if item.required
    ]


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
