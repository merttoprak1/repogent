from decimal import Decimal

import pytest
from pydantic import ValidationError

from repogent.domain import (
    Budget,
    CheckResult,
    CheckStatus,
    ImplementationPlan,
    PlanStep,
    RequirementsSpec,
    RunManifest,
    RunStage,
    RunStatus,
    ValidationReport,
)


def test_requirements_reject_empty_objective() -> None:
    with pytest.raises(ValidationError):
        RequirementsSpec(objective="", functional_requirements=[], acceptance_criteria=[])


def test_plan_rejects_unknown_dependency() -> None:
    with pytest.raises(ValidationError, match="unknown dependency"):
        ImplementationPlan(
            files_to_modify=["app/main.py"],
            steps=[PlanStep(id="change", description="Change route", depends_on=["missing"])],
            tests=["pytest"],
        )


def test_validation_report_passes_only_when_every_check_passes_or_skips() -> None:
    report = ValidationReport(
        checks=[
            CheckResult(name="pytest", argv=["python", "-m", "pytest"], status=CheckStatus.PASSED),
            CheckResult(name="ruff", argv=["ruff", "check", "."], status=CheckStatus.SKIPPED),
        ]
    )
    assert report.passed is True


def test_budget_defaults_to_two_repairs_and_positive_limits() -> None:
    budget = Budget()
    assert budget.max_repairs == 2
    assert budget.max_tokens > 0
    assert budget.max_cost_usd == Decimal("20.00")


def test_manifest_starts_in_created_state() -> None:
    manifest = RunManifest(run_id="run-123", request="Add a health route")
    assert manifest.status is RunStatus.RUNNING
    assert manifest.stage is RunStage.CREATED
