from repogent.domain import (
    CheckResult,
    CheckStatus,
    ImplementationPlan,
    MergeRecommendation,
    PlanStep,
    QAReview,
    RequirementsSpec,
    RiskLevel,
    RunManifest,
    RunStatus,
    ValidationReport,
)
from repogent.reporting import render_report


def test_report_separates_tool_evidence_from_qa_interpretation() -> None:
    manifest = RunManifest(run_id="run-1", request="add route", status=RunStatus.COMPLETED)
    requirements = RequirementsSpec(
        objective="Add route", functional_requirements=[], acceptance_criteria=["tests pass"]
    )
    plan = ImplementationPlan(
        files_to_modify=["app.py"],
        steps=[PlanStep(id="change", description="Add route")],
        tests=["pytest"],
    )
    validation = ValidationReport(
        checks=[
            CheckResult(
                name="pytest", argv=["pytest"], status=CheckStatus.PASSED, exit_code=0
            )
        ]
    )
    review = QAReview(
        acceptance_criteria_coverage=1,
        test_quality_score=0.9,
        security_score=0.9,
        regression_risk=RiskLevel.LOW,
        merge_recommendation=MergeRecommendation.APPROVE,
    )
    report = render_report(manifest, requirements, plan, validation, review)
    assert "## Deterministic validation" in report
    assert "pytest: passed (exit 0)" in report
    assert "## Model-generated QA review" in report
