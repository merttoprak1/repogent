from repogent.domain import (
    CandidateEvidence,
    CandidateRecord,
    CandidateSelection,
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
from repogent.localization import LocalizationReport, LocalizedSymbol
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


def test_report_shows_localization_candidate_evidence_and_recovery() -> None:
    manifest = RunManifest(run_id="run-1", request="add route | safely", status=RunStatus.COMPLETED)
    validation = ValidationReport(
        checks=[
            CheckResult(
                name="pytest | unit",
                argv=["pytest"],
                status=CheckStatus.PASSED,
                exit_code=0,
            )
        ]
    )
    localization = LocalizationReport(
        locations=[
            LocalizedSymbol(
                symbol_id="route", path="app.py", start_line=1, end_line=4, score=1,
                signals=[],
            )
        ],
        snippets=[],
        ambiguous=True,
        ambiguity_reason="two paths | tied",
    )
    candidates = [
        (
            CandidateRecord.model_validate(
                {
                    "candidate_id": "candidate-2",
                    "proposal": {
                        "summary": "Rejected | unsafe",
                        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
                    },
                    "generation_reason": "alternative",
                    "diff_sha256": "a" * 64,
                    "usage": {"model": "test", "estimated_cost_usd": "0.18"},
                }
            ),
            CandidateEvidence(
                candidate_id="candidate-2",
                validation=validation,
                acceptance_criteria_coverage=0.875,
                risk_level=RiskLevel.HIGH,
                changed_files=2,
                changed_lines=17,
                duration_seconds=1.5,
                required_failures=["pytest | unit"],
                skipped_checks=["lint"],
                restored_to_baseline=True,
            ),
        ),
        (
            CandidateRecord.model_validate(
                {
                    "candidate_id": "candidate-1",
                    "proposal": {
                        "summary": "Selected",
                        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
                    },
                    "generation_reason": "initial implementation",
                    "diff_sha256": "b" * 64,
                    "usage": {"model": "test", "estimated_cost_usd": "0.08"},
                }
            ),
            CandidateEvidence(
                candidate_id="candidate-1",
                validation=validation,
                acceptance_criteria_coverage=1.0,
                risk_level=RiskLevel.LOW,
                changed_files=1,
                changed_lines=2,
                duration_seconds=0.5,
                restored_to_baseline=True,
            ),
        ),
    ]
    selection = CandidateSelection(
        selected_candidate_id="candidate-1",
        eligible_candidate_ids=["candidate-1"],
        reason="candidate-1 has the strongest evidence",
    )

    report = render_report(
        manifest,
        None,
        None,
        validation,
        None,
        localization=localization,
        candidates=candidates,
        selection=selection,
    )

    for section in (
        "## Localization",
        "## Candidate comparison",
        "## Selection",
        "## Deterministic validation",
        "## Cost and duration",
        "## Recovery",
    ):
        assert section in report
    assert report.index("candidate-1") < report.index("candidate-2")
    assert "candidate-2" in report
    assert "pytest \\| unit" in report
    assert "two paths \\| tied" in report
    assert "selected" in report
    assert "0.875" in report
    assert "$0.18" in report
    assert "restored" in report
