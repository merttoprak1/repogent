from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from repogent.domain import WorkflowKind, WorkflowOutcome


class RunOperation(StrEnum):
    GET = "get"
    CANCEL = "cancel"
    GET_REPORT = "get_report"
    APPROVE_REQUIREMENTS = "approve_requirements"
    APPROVE_PLAN = "approve_plan"
    SELECT_EXECUTOR = "select_executor"
    APPLY_PATCH = "apply_patch"


class CapabilityPolicyError(ValueError):
    code: str = "operation_not_allowed"

    @classmethod
    def operation_not_allowed(
        cls, kind: WorkflowKind, operation: RunOperation
    ) -> CapabilityPolicyError:
        return cls(f"{operation.value} is not allowed for {kind.value}")


@dataclass(frozen=True)
class CapabilityDefinition:
    kind: WorkflowKind
    mutates_checkout: bool
    allowed_operations: frozenset[RunOperation]
    allowed_outcomes: frozenset[WorkflowOutcome]


_READ_OPERATIONS = frozenset({RunOperation.GET, RunOperation.CANCEL, RunOperation.GET_REPORT})
_MUTATING_OPERATIONS = _READ_OPERATIONS | frozenset(
    {
        RunOperation.APPROVE_REQUIREMENTS,
        RunOperation.APPROVE_PLAN,
        RunOperation.SELECT_EXECUTOR,
        RunOperation.APPLY_PATCH,
    }
)

DEFAULT_CAPABILITIES = (
    CapabilityDefinition(
        kind=WorkflowKind.VERIFIED_CHANGE,
        mutates_checkout=True,
        allowed_operations=_MUTATING_OPERATIONS,
        allowed_outcomes=frozenset(
            {
                WorkflowOutcome.PATCH_READY,
                WorkflowOutcome.APPLIED,
                WorkflowOutcome.HUMAN_INTERVENTION_REQUIRED,
            }
        ),
    ),
    CapabilityDefinition(
        kind=WorkflowKind.PATCH_REVIEW,
        mutates_checkout=False,
        allowed_operations=_READ_OPERATIONS,
        allowed_outcomes=frozenset(
            {
                WorkflowOutcome.APPROVE,
                WorkflowOutcome.REQUEST_CHANGES,
                WorkflowOutcome.INCONCLUSIVE,
            }
        ),
    ),
    CapabilityDefinition(
        kind=WorkflowKind.CI_TRIAGE,
        mutates_checkout=False,
        allowed_operations=_READ_OPERATIONS,
        allowed_outcomes=frozenset(
            {WorkflowOutcome.ROOT_CAUSE_IDENTIFIED, WorkflowOutcome.INCONCLUSIVE}
        ),
    ),
    CapabilityDefinition(
        kind=WorkflowKind.DEPENDENCY_UPDATE,
        mutates_checkout=True,
        allowed_operations=_MUTATING_OPERATIONS,
        allowed_outcomes=frozenset(
            {
                WorkflowOutcome.CANDIDATES_FOUND,
                WorkflowOutcome.APPLIED,
                WorkflowOutcome.HUMAN_INTERVENTION_REQUIRED,
            }
        ),
    ),
    CapabilityDefinition(
        kind=WorkflowKind.SECURITY_FIX,
        mutates_checkout=True,
        allowed_operations=_MUTATING_OPERATIONS,
        allowed_outcomes=frozenset(
            {
                WorkflowOutcome.PATCH_READY,
                WorkflowOutcome.APPLIED,
                WorkflowOutcome.HUMAN_INTERVENTION_REQUIRED,
            }
        ),
    ),
    CapabilityDefinition(
        kind=WorkflowKind.RELEASE_GATE,
        mutates_checkout=False,
        allowed_operations=_READ_OPERATIONS,
        allowed_outcomes=frozenset(
            {WorkflowOutcome.RELEASE_VERIFIED, WorkflowOutcome.RELEASE_BLOCKED}
        ),
    ),
)


class CapabilityRegistry:
    def __init__(self, definitions: dict[WorkflowKind, CapabilityDefinition]) -> None:
        self._definitions = definitions

    @classmethod
    def defaults(cls) -> CapabilityRegistry:
        return cls({item.kind: item for item in DEFAULT_CAPABILITIES})

    def definition(self, kind: WorkflowKind) -> CapabilityDefinition:
        return self._definitions[kind]

    def require(self, kind: WorkflowKind, operation: RunOperation) -> None:
        if operation not in self.definition(kind).allowed_operations:
            raise CapabilityPolicyError.operation_not_allowed(kind, operation)

    def validate_outcome(self, kind: WorkflowKind, outcome: WorkflowOutcome) -> None:
        if outcome not in self.definition(kind).allowed_outcomes:
            raise ValueError(f"{outcome.value} is not valid for {kind.value}")
