import json

from repogent.domain import (
    CandidateEvidence,
    CandidateRecord,
    CheckResult,
    CheckStatus,
    ContextSnippet,
    ImplementationPlan,
    PatchProposal,
    PlanStep,
    ProviderUsage,
    RequirementsSpec,
    RiskLevel,
    ValidationReport,
)
from repogent.localization import LocalizationReport, LocalizedSymbol
from repogent.provider_context import (
    MAX_PROVIDER_PAYLOAD_CHARS,
    MAX_PROVIDER_SNIPPETS,
    MAX_PROVIDER_STDIO_CHARS,
    ProviderContextBuilder,
)
from repogent.repository import FileRecord, RepositoryInventory


def _large_inventory() -> RepositoryInventory:
    return RepositoryInventory(
        root="/repository",
        files=[
            FileRecord(
                path=f"src/package/module_{index:04d}.py",
                size=100_000,
                sha256=f"{index:064x}"[-64:],
                kind="python",
                symbols=[f"symbol_{index}_{part}" for part in range(30)],
                imports=[f"dependency_{index}_{part}" for part in range(30)],
                text="private implementation\n" * 4_000,
            )
            for index in range(2_000)
        ],
    )


def _requirements() -> RequirementsSpec:
    return RequirementsSpec(
        objective="change behavior",
        functional_requirements=["preserve behavior"],
        acceptance_criteria=["tests pass"],
    )


def _plan() -> ImplementationPlan:
    return ImplementationPlan(
        files_to_modify=["src/package/module_0000.py"],
        steps=[PlanStep(id="change", description="change behavior")],
        tests=["pytest"],
    )


def _localization() -> LocalizationReport:
    locations = [
        LocalizedSymbol(
            symbol_id=f"symbol-{index}",
            path=f"src/package/module_{index:04d}.py",
            start_line=1,
            end_line=10,
            score=1 / (index + 1),
            signals=[],
        )
        for index in range(100)
    ]
    from repogent.domain import ContextSnippet

    snippets = [
        ContextSnippet(
            path=location.path,
            start_line=1,
            end_line=100,
            text="x" * 10_000,
            score=location.score,
            reason="ranked evidence",
        )
        for location in locations
    ]
    return LocalizationReport(
        locations=locations,
        snippets=snippets,
        ambiguous=False,
    )


def test_requirements_context_contains_bounded_metadata_without_file_contents() -> None:
    payload = ProviderContextBuilder().requirements("change behavior", _large_inventory())
    serialized = json.dumps(payload, sort_keys=True)

    assert len(serialized) <= MAX_PROVIDER_PAYLOAD_CHARS
    assert "private implementation" not in serialized
    assert payload["repository_inventory"]["total_files"] == 2_000  # type: ignore[index]
    assert payload["repository_inventory"]["truncated"] is True  # type: ignore[index]


def test_ranked_context_limits_locations_snippets_and_serialized_size() -> None:
    payload = ProviderContextBuilder().planning(_requirements(), _localization())
    localization = payload["localization"]
    assert isinstance(localization, dict)

    assert 1 <= len(localization["snippets"]) <= MAX_PROVIDER_SNIPPETS
    assert len(json.dumps(payload, sort_keys=True)) <= MAX_PROVIDER_PAYLOAD_CHARS
    assert "locations" not in payload


def test_repair_context_caps_and_marks_validation_output() -> None:
    proposal = PatchProposal(
        summary="change",
        diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
    )
    candidate = CandidateRecord(
        candidate_id="candidate-1",
        proposal=proposal,
        generation_reason="initial",
        diff_sha256="a" * 64,
        usage=ProviderUsage(model="test"),
    )
    evidence = CandidateEvidence(
        candidate_id="candidate-1",
        validation=ValidationReport(
            checks=[
                CheckResult(
                    name="pytest",
                    argv=["python", "-m", "pytest"],
                    status=CheckStatus.FAILED,
                    stdout="o" * (MAX_PROVIDER_STDIO_CHARS + 100),
                    stderr="s" * (MAX_PROVIDER_STDIO_CHARS + 100),
                )
            ]
        ),
        acceptance_criteria_coverage=0,
        risk_level=RiskLevel.LOW,
        changed_files=1,
        changed_lines=2,
        duration_seconds=1,
        required_failures=["pytest"],
        restored_to_baseline=True,
    )

    payload = ProviderContextBuilder().candidate(
        _requirements(),
        _plan(),
        _localization(),
        "candidate-2",
        previous=candidate,
        previous_evidence=evidence,
        generation_reason="validation_failure",
    )
    serialized = json.dumps(payload, sort_keys=True)

    assert len(serialized) <= MAX_PROVIDER_PAYLOAD_CHARS
    assert "[truncated]" in serialized
    assert "o" * (MAX_PROVIDER_STDIO_CHARS + 1) not in serialized
    assert "s" * (MAX_PROVIDER_STDIO_CHARS + 1) not in serialized


def test_worst_case_provider_contexts_fit_deterministically_without_mutating_models() -> None:
    huge = "context-" + ("x" * 20_000)
    requirements = RequirementsSpec(
        objective=huge,
        functional_requirements=[f"functional-{index}-{huge}" for index in range(64)],
        non_functional_requirements=[huge for _ in range(64)],
        acceptance_criteria=[f"criterion-{index}-{huge}" for index in range(64)],
        technical_constraints=[huge for _ in range(64)],
        assumptions=[huge for _ in range(64)],
        open_questions=[huge for _ in range(64)],
    )
    plan = ImplementationPlan(
        files_to_inspect=[f"src/{index}-{huge}.py" for index in range(64)],
        files_to_modify=[f"src/change-{index}-{huge}.py" for index in range(64)],
        steps=[
            PlanStep(id=f"step_{index}", description=huge)
            for index in range(32)
        ],
        tests=[huge for _ in range(64)],
        security_considerations=[huge for _ in range(64)],
        regression_risks=[huge for _ in range(64)],
        rollback=huge,
    )
    proposal = PatchProposal(
        summary=huge,
        diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-" + huge + "\n+" + huge,
        acceptance_criteria_addressed=[huge for _ in range(64)],
        assumptions=[huge for _ in range(64)],
        risks=[huge for _ in range(64)],
    )
    previous = CandidateRecord(
        candidate_id="candidate-1",
        proposal=proposal,
        generation_reason="initial",
        diff_sha256="a" * 64,
        usage=ProviderUsage(model="test"),
    )
    checks = [
        CheckResult(
            name=f"check-{index}",
            argv=[huge for _ in range(16)],
            status=CheckStatus.FAILED,
            stdout=huge,
            stderr=huge,
            reason=f"critical-reason-{index}",
            required=True,
        )
        for index in range(4)
    ]
    validation = ValidationReport(checks=checks)
    evidence = CandidateEvidence(
        candidate_id="candidate-1",
        validation=validation,
        acceptance_criteria_coverage=0,
        risk_level=RiskLevel.HIGH,
        changed_files=1,
        changed_lines=2,
        duration_seconds=1,
        required_failures=[huge for _ in range(64)],
        skipped_checks=[huge for _ in range(64)],
        restored_to_baseline=True,
    )
    localization = _localization()
    before = (
        requirements.model_dump_json(),
        plan.model_dump_json(),
        previous.model_dump_json(),
        evidence.model_dump_json(),
        localization.model_dump_json(),
    )
    builder = ProviderContextBuilder()

    factories = [
        lambda: builder.requirements(huge, _large_inventory()),
        lambda: builder.planning(requirements, localization),
        lambda: builder.candidate(
            requirements, plan, localization, "candidate-2"
        ),
        lambda: builder.candidate(
            requirements,
            plan,
            localization,
            "candidate-2",
            previous=previous,
            previous_evidence=evidence,
            generation_reason="validation_failure",
        ),
        lambda: builder.qa(
            requirements,
            plan,
            previous,
            "selected because it preserves the critical behavior",
            validation,
        ),
    ]

    payloads = [factory() for factory in factories]
    assert payloads == [factory() for factory in factories]
    assert all(
        len(json.dumps(payload, sort_keys=True)) <= MAX_PROVIDER_PAYLOAD_CHARS
        for payload in payloads
    )
    repair = payloads[3]
    assert repair["candidate_id"] == "candidate-2"
    assert repair["generation_reason"] == "validation_failure"
    assert repair["previous_failure"]["candidate_id"] == "candidate-1"  # type: ignore[index]
    assert repair["previous_failure"]["checks"][0]["status"] == "failed"  # type: ignore[index]
    assert repair["previous_failure"]["checks"][0]["reason"] == "critical-reason-0"  # type: ignore[index]
    assert repair["context_truncation"]["truncated"] is True  # type: ignore[index]
    qa = payloads[4]
    assert qa["selected_candidate"]["candidate_id"] == "candidate-1"  # type: ignore[index]
    assert qa["selection_reason"] == "selected because it preserves the critical behavior"
    assert qa["final_validation"]["checks"][0]["status"] == "failed"  # type: ignore[index]
    assert qa["final_validation"]["checks"][0]["reason"] == "critical-reason-0"  # type: ignore[index]
    assert before == (
        requirements.model_dump_json(),
        plan.model_dump_json(),
        previous.model_dump_json(),
        evidence.model_dump_json(),
        localization.model_dump_json(),
    )


def test_truncated_snippet_end_line_matches_complete_included_lines() -> None:
    text = "\n".join(f"line-{index}-" + ("x" * 100) for index in range(1_000))
    localization = LocalizationReport(
        locations=[],
        snippets=[
            ContextSnippet(
                path="src/a.py",
                start_line=10,
                end_line=1_009,
                text=text,
                score=1,
                reason="ranked evidence",
            )
        ],
        ambiguous=False,
    )

    payload = ProviderContextBuilder().planning(_requirements(), localization)
    snippet = payload["localization"]["snippets"][0]  # type: ignore[index]

    assert snippet["text_truncated"] is True
    assert snippet["end_line"] == snippet["start_line"] + snippet["text"].count("\n")
    assert snippet["omitted_line_count"] > 0
