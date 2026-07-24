import pytest

from repogent.domain import (
    CandidateEvidence,
    CandidateRecord,
    CandidateSelection,
    CheckoutState,
    CheckResult,
    CheckStatus,
    ExecutionMode,
    FinalValidationStatus,
    ImplementationPlan,
    IsolationLevel,
    MergeRecommendation,
    PlanStep,
    QAReview,
    RequirementsSpec,
    RiskLevel,
    RunManifest,
    RunStatus,
    ValidationReport,
    VerificationStatus,
)
from repogent.localization import LocalizationReport, LocalizedSymbol
from repogent.reporting import derive_trust_label, render_report


def manifest_with_execution(
    *, mode: ExecutionMode | None, verification_status: VerificationStatus
) -> RunManifest:
    isolation_level = None
    if mode is ExecutionMode.LOCAL:
        isolation_level = IsolationLevel.REDUCED_ISOLATION
    elif mode is ExecutionMode.DOCKER:
        isolation_level = IsolationLevel.ISOLATED
    return RunManifest(
        run_id="run-1",
        request="change",
        execution_mode=mode,
        isolation_level=isolation_level,
        verification_status=verification_status,
    )


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

    interrupted_report = render_report(
        manifest,
        None,
        None,
        None,
        None,
        candidates=[(candidates[0][0], None)],
    )

    assert "candidate-2" in interrupted_report
    assert "not evaluated" in interrupted_report
    assert "evaluation interrupted; recovery unknown" in interrupted_report


def test_report_distinguishes_disposable_recovery_from_applied_real_patch() -> None:
    manifest = RunManifest(
        run_id="run-1",
        request="change",
        status=RunStatus.HUMAN_INTERVENTION_REQUIRED,
        selected_patch_applied=True,
        applied_paths=["src/app.py"],
        final_validation_status=FinalValidationStatus.FAILED,
        recovery_guidance=(
            "Review src/app.py, run required validation, and revert the approved patch manually "
            "if it should not remain."
        ),
        checkout_state=CheckoutState.APPLIED,
    )

    report = render_report(manifest, None, None, None, None)

    assert "Real checkout patch: remains applied" in report
    assert "src/app.py" in report
    assert "Final validation: failed" in report
    assert "revert the approved patch manually" in report


def test_report_never_claims_not_applied_when_checkout_recovery_is_unknown() -> None:
    manifest = RunManifest(
        run_id="run-1",
        request="change",
        status=RunStatus.HUMAN_INTERVENTION_REQUIRED,
        checkout_state=CheckoutState.RECOVERY_UNKNOWN,
        selected_patch_applied=False,
        applied_paths=["src/app.py"],
        recovery_guidance="Inspect and manually restore src/app.py before continuing.",
    )

    report = render_report(manifest, None, None, None, None)

    assert "Real checkout patch: recovery unknown" in report
    assert "Real checkout patch: not applied" not in report
    assert "Inspect and manually restore src/app.py" in report


@pytest.mark.parametrize(
    ("mode", "status", "label"),
    [
        (None, VerificationStatus.UNVALIDATED, "UNVALIDATED"),
        (ExecutionMode.LOCAL, VerificationStatus.VALIDATING, "REDUCED ISOLATION"),
        (ExecutionMode.LOCAL, VerificationStatus.PASSED, "REDUCED ISOLATION"),
        (ExecutionMode.LOCAL, VerificationStatus.FAILED, "REDUCED ISOLATION"),
        (ExecutionMode.DOCKER, VerificationStatus.PASSED, "ISOLATED VERIFIED"),
        (ExecutionMode.DOCKER, VerificationStatus.FAILED, "UNVALIDATED"),
    ],
)
def test_report_never_overstates_verification(
    mode: ExecutionMode | None, status: VerificationStatus, label: str
) -> None:
    manifest = manifest_with_execution(mode=mode, verification_status=status)
    assert derive_trust_label(manifest) == label


def test_report_shows_isolated_verified_only_for_docker_and_passed() -> None:
    manifest = RunManifest(
        run_id="run-1",
        request="add route",
        status=RunStatus.COMPLETED,
        execution_mode=ExecutionMode.DOCKER,
        isolation_level=IsolationLevel.ISOLATED,
        verification_status=VerificationStatus.PASSED,
        preview_digest="a" * 64,
    )

    report = render_report(manifest, None, None, None, None)

    assert "Verification: ISOLATED VERIFIED" in report
    assert "Execution mode: docker" in report
    assert f"Preview digest: {'a' * 64}" in report


def test_report_shows_none_for_unset_execution_evidence() -> None:
    manifest = RunManifest(run_id="run-1", request="add route", status=RunStatus.COMPLETED)

    report = render_report(manifest, None, None, None, None)

    assert "Verification: UNVALIDATED" in report
    assert "Execution mode: none" in report
    assert "Preview digest: none" in report


def test_report_downgrades_docker_failure_to_unvalidated() -> None:
    manifest = RunManifest(
        run_id="run-1",
        request="add route",
        status=RunStatus.HUMAN_INTERVENTION_REQUIRED,
        execution_mode=ExecutionMode.DOCKER,
        isolation_level=IsolationLevel.ISOLATED,
        verification_status=VerificationStatus.FAILED,
    )

    report = render_report(manifest, None, None, None, None)

    assert "Verification: UNVALIDATED" in report


def test_report_downgrades_docker_passed_without_isolated_level() -> None:
    """Docker plus a passed check must not imply ISOLATED VERIFIED on its own.

    ``isolation_level`` must also be ``ISOLATED``; a manifest that somehow
    reports Docker execution and a passed check without isolation actually
    having been applied must still be downgraded to UNVALIDATED.
    """
    manifest = RunManifest(
        run_id="run-1",
        request="add route",
        status=RunStatus.COMPLETED,
        execution_mode=ExecutionMode.DOCKER,
        isolation_level=IsolationLevel.REDUCED_ISOLATION,
        verification_status=VerificationStatus.PASSED,
    )

    assert derive_trust_label(manifest) == "UNVALIDATED"
