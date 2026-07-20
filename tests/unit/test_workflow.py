from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from repogent.agents import RoleSet
from repogent.approvals import FakeApprover
from repogent.artifacts import ArtifactStore
from repogent.domain import (
    ApprovalKind,
    Budget,
    CheckResult,
    CheckStatus,
    Decision,
    ProviderUsage,
    RunManifest,
    RunStage,
    RunStatus,
    ValidationReport,
)
from repogent.patching import PatchApplier, PatchPolicy
from repogent.providers import ScriptedProvider
from repogent.repository import LexicalRetriever, RepositoryInspector
from repogent.workflow import BudgetExceeded, IllegalTransition, Workflow, transition

BASE_OUTPUTS = [
    {
        "objective": "Change value",
        "functional_requirements": ["value is 2"],
        "acceptance_criteria": ["tests pass"],
    },
    {
        "files_to_modify": ["app.py"],
        "steps": [{"id": "change", "description": "Change value"}],
        "tests": ["pytest"],
    },
    {
        "summary": "Change value",
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n",
    },
    {
        "acceptance_criteria_coverage": 1,
        "test_quality_score": 1,
        "security_score": 1,
        "regression_risk": "low",
        "merge_recommendation": "approve",
    },
]


class SequenceValidator:
    def __init__(self, statuses: list[CheckStatus]) -> None:
        self.statuses = statuses

    def run(self, root: Path) -> ValidationReport:
        del root
        status = self.statuses.pop(0)
        return ValidationReport(
            checks=[
                CheckResult(
                    name="pytest",
                    argv=["pytest"],
                    status=status,
                    exit_code=0 if status is CheckStatus.PASSED else 1,
                )
            ]
        )


def make_workflow(
    tmp_path: Path,
    outputs: list[dict[str, object]],
    decisions: list[Decision],
    statuses: list[CheckStatus],
    *,
    budget: Budget | None = None,
) -> Workflow:
    target = tmp_path / "target"
    target.mkdir()
    (target / "app.py").write_text("value = 1\n")
    store = ArtifactStore.create(tmp_path / "runs", target, "change", run_id="run-1")
    return Workflow(
        root=target,
        request="change value",
        manifest=RunManifest(run_id="run-1", request="change value"),
        roles=RoleSet.from_provider(ScriptedProvider(outputs)),
        approver=FakeApprover(decisions),
        patch_policy=PatchPolicy(),
        patch_applier=PatchApplier(),
        validator=SequenceValidator(statuses),
        artifacts=store,
        inspector=RepositoryInspector(),
        retriever=LexicalRetriever(),
        budget=budget or Budget(),
    )


def test_illegal_transition_is_rejected() -> None:
    with pytest.raises(IllegalTransition):
        transition(RunStage.CREATED, RunStage.PATCH_APPLIED)


def test_plan_rejection_finishes_cancelled_without_modifying_target(tmp_path: Path) -> None:
    workflow = make_workflow(
        tmp_path,
        BASE_OUTPUTS,
        [Decision.APPROVED, Decision.REJECTED],
        [CheckStatus.PASSED],
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.CANCELLED
    assert (workflow.root / "app.py").read_text() == "value = 1\n"
    assert (workflow.artifacts.root / "report.md").exists()


def test_successful_run_applies_patch_validates_and_reports(tmp_path: Path) -> None:
    workflow = make_workflow(
        tmp_path, BASE_OUTPUTS, [Decision.APPROVED] * 3, [CheckStatus.PASSED]
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.COMPLETED
    assert (workflow.root / "app.py").read_text() == "value = 2\n"
    assert (workflow.artifacts.root / "report.md").exists()
    assert json.loads((workflow.artifacts.root / "run.json").read_text())["stage"] == "finished"


def test_failed_validation_uses_approved_repair(tmp_path: Path) -> None:
    initial = BASE_OUTPUTS[:3]
    initial[2] = {
        "summary": "No-op comment",
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n value = 1\n+# initial\n",
    }
    repair = {
        "summary": "Repair value",
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-value = 1\n+value = 2\n # initial\n",
    }
    outputs = [*initial, repair, BASE_OUTPUTS[3]]
    workflow = make_workflow(
        tmp_path, outputs, [Decision.APPROVED] * 4, [CheckStatus.FAILED, CheckStatus.PASSED]
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.COMPLETED
    assert manifest.repair_attempts == 1


def test_rejected_repair_patch_is_never_applied(tmp_path: Path) -> None:
    initial = BASE_OUTPUTS[:3]
    initial[2] = {
        "summary": "No-op comment",
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n value = 1\n+# initial\n",
    }
    repair = {
        "summary": "Repair value",
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-value = 1\n+value = 2\n # initial\n",
    }
    workflow = make_workflow(
        tmp_path,
        [*initial, repair],
        [Decision.APPROVED, Decision.APPROVED, Decision.APPROVED, Decision.REJECTED],
        [CheckStatus.FAILED],
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.CANCELLED
    assert (workflow.root / "app.py").read_text() == "value = 1\n# initial\n"
    assert workflow.approver.records[-1].kind is ApprovalKind.REPAIR_PATCH
    assert (workflow.artifacts.root / "report.md").exists()


def test_two_failed_repairs_require_human_intervention(tmp_path: Path) -> None:
    no_op = {
        "summary": "Add comment",
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n value = 1\n+# note\n",
    }
    second = {
        "summary": "Add second comment",
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,3 @@\n value = 1\n # note\n+# note 2\n",
    }
    outputs = [*BASE_OUTPUTS[:2], no_op, second, {"summary": "Third", "diff": second["diff"]}]
    workflow = make_workflow(
        tmp_path, outputs, [Decision.APPROVED] * 5, [CheckStatus.FAILED] * 3
    )

    manifest = workflow.run()

    assert manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
    assert manifest.repair_attempts == 2
    assert (workflow.artifacts.root / "report.md").exists()


def test_account_persists_usage_before_enforcing_token_and_cost_budget(tmp_path: Path) -> None:
    workflow = make_workflow(
        tmp_path,
        [],
        [],
        [],
        budget=Budget(max_tokens=1, max_cost_usd=Decimal("0.01")),
    )

    with pytest.raises(BudgetExceeded, match="token budget"):
        workflow.account(
            ProviderUsage(
                model="test",
                input_tokens=1,
                output_tokens=1,
                estimated_cost_usd=Decimal("0.02"),
            )
        )

    persisted = json.loads((workflow.artifacts.root / "run.json").read_text())
    assert workflow.manifest.token_usage == 2
    assert persisted["token_usage"] == 2


def test_timeout_is_enforced_before_provider_work(tmp_path: Path) -> None:
    workflow = make_workflow(
        tmp_path, [], [], [], budget=Budget(timeout_seconds=1)
    )
    workflow.started_at = time.monotonic() - 2

    with pytest.raises(TimeoutError, match="workflow timeout"):
        workflow.ensure_time()
