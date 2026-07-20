import json

from repogent.domain import (
    CandidateEvidence,
    CandidateRecord,
    CheckResult,
    CheckStatus,
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
