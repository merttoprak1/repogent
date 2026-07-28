import pytest

from repogent.capabilities import (
    CapabilityPolicyError,
    CapabilityRegistry,
    RunOperation,
)
from repogent.domain import WorkflowKind, WorkflowOutcome


def test_patch_review_denies_patch_application() -> None:
    """Catch a policy regression that gives a read-only review checkout authority."""
    registry = CapabilityRegistry.defaults()

    with pytest.raises(CapabilityPolicyError) as caught:
        registry.require(WorkflowKind.PATCH_REVIEW, RunOperation.APPLY_PATCH)

    assert caught.value.code == "operation_not_allowed"


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        (WorkflowKind.VERIFIED_CHANGE, WorkflowOutcome.PATCH_READY),
        (WorkflowKind.PATCH_REVIEW, WorkflowOutcome.APPROVE),
        (WorkflowKind.CI_TRIAGE, WorkflowOutcome.ROOT_CAUSE_IDENTIFIED),
        (WorkflowKind.DEPENDENCY_UPDATE, WorkflowOutcome.CANDIDATES_FOUND),
        (WorkflowKind.SECURITY_FIX, WorkflowOutcome.APPLIED),
        (WorkflowKind.RELEASE_GATE, WorkflowOutcome.RELEASE_VERIFIED),
    ],
)
def test_each_workflow_kind_accepts_its_declared_terminal_outcome(
    kind: WorkflowKind, outcome: WorkflowOutcome
) -> None:
    """Catch an incomplete registry that strands a supported workflow kind."""
    CapabilityRegistry.defaults().validate_outcome(kind, outcome)


def test_every_kind_has_disjoint_allowed_outcomes() -> None:
    """Catch a policy regression that lets review runs report applied patches."""
    registry = CapabilityRegistry.defaults()

    assert registry.definition(WorkflowKind.RELEASE_GATE).allowed_outcomes == frozenset(
        {WorkflowOutcome.RELEASE_VERIFIED, WorkflowOutcome.RELEASE_BLOCKED}
    )
    assert (
        WorkflowOutcome.APPLIED
        not in registry.definition(WorkflowKind.PATCH_REVIEW).allowed_outcomes
    )


@pytest.mark.parametrize(
    "kind",
    [WorkflowKind.VERIFIED_CHANGE, WorkflowKind.DEPENDENCY_UPDATE, WorkflowKind.SECURITY_FIX],
)
def test_mutating_workflows_allow_patch_application(kind: WorkflowKind) -> None:
    """Catch a policy regression that removes checkout authority from mutating runs."""
    CapabilityRegistry.defaults().require(kind, RunOperation.APPLY_PATCH)
