import hashlib
import os
from pathlib import Path

import pytest

from repogent.candidates import (
    CandidateEvaluator,
    CandidatePolicy,
    CandidateSelector,
    ExpansionReason,
)
from repogent.domain import (
    CandidateEvidence,
    CandidateRecord,
    CheckResult,
    CheckStatus,
    PatchProposal,
    ProviderUsage,
    RiskLevel,
    ValidationReport,
)
from repogent.localization import LocalizationReport
from repogent.patching import PatchApplier, PatchPolicy


class RecordingValidator:
    def run(
        self, root: Path, *, timeout_seconds: float | None = None
    ) -> ValidationReport:
        del timeout_seconds
        assert (root / "app.py").exists()
        return ValidationReport(
            checks=[
                CheckResult(
                    name="pytest",
                    argv=["python", "-m", "pytest", "-q"],
                    status=CheckStatus.PASSED,
                    exit_code=0,
                )
            ]
        )


def repository_with_value(root: Path, value: int) -> Path:
    repository = root / "repository"
    repository.mkdir()
    (repository / "app.py").write_text(f"value = {value}\n")
    return repository


def proposal_changing_value(old: int, new: int) -> PatchProposal:
    return PatchProposal(
        summary="Change value",
        diff=f"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = {old}\n+value = {new}\n",
        acceptance_criteria_addressed=["value changes"],
        focused_tests=["pytest"],
    )


def candidate(candidate_id: str, old: int, new: int) -> CandidateRecord:
    proposal = proposal_changing_value(old, new)
    return CandidateRecord(
        candidate_id=candidate_id,
        proposal=proposal,
        generation_reason=(
            "initial candidate" if candidate_id == "candidate-1" else "validation failed"
        ),
        diff_sha256=hashlib.sha256(proposal.diff.encode()).hexdigest(),
        usage=ProviderUsage(model="scripted"),
    )


def localization_report(*, ambiguous: bool) -> LocalizationReport:
    return LocalizationReport(
        locations=[],
        snippets=[],
        ambiguous=ambiguous,
        ambiguity_reason="top locations are not concentrated" if ambiguous else None,
    )


def candidate_evidence(
    *,
    candidate_id: str = "candidate-1",
    eligible: bool = True,
    risk: RiskLevel = RiskLevel.LOW,
    changed_files: int = 1,
    changed_lines: int = 4,
    coverage: float = 1.0,
    required_failures: list[str] | None = None,
    skipped_checks: list[str] | None = None,
) -> CandidateEvidence:
    status = CheckStatus.PASSED if eligible else CheckStatus.FAILED
    return CandidateEvidence(
        candidate_id=candidate_id,
        validation=ValidationReport(
            checks=[
                CheckResult(
                    name="pytest",
                    argv=["pytest"],
                    status=status,
                    exit_code=0 if eligible else 1,
                )
            ]
        ),
        acceptance_criteria_coverage=coverage,
        risk_level=risk,
        changed_files=changed_files,
        changed_lines=changed_lines,
        duration_seconds=1,
        required_failures=(
            required_failures
            if required_failures is not None
            else ([] if eligible else ["pytest"])
        ),
        skipped_checks=skipped_checks or [],
        restored_to_baseline=True,
    )


@pytest.mark.parametrize(
    ("ambiguous", "eligible", "risk", "changed_lines", "coverage", "expected"),
    [
        (False, True, RiskLevel.LOW, 4, 1.0, None),
        (True, True, RiskLevel.LOW, 4, 1.0, ExpansionReason.AMBIGUOUS_LOCALIZATION),
        (False, False, RiskLevel.LOW, 4, 0.0, ExpansionReason.VALIDATION_FAILED),
        (False, True, RiskLevel.HIGH, 4, 1.0, ExpansionReason.HIGH_RISK),
        (False, True, RiskLevel.LOW, 501, 1.0, ExpansionReason.BROAD_PATCH),
        (False, True, RiskLevel.LOW, 4, 0.5, ExpansionReason.INCOMPLETE_ACCEPTANCE),
    ],
)
def test_candidate_policy_expands_only_for_objective_reasons(
    ambiguous: bool,
    eligible: bool,
    risk: RiskLevel,
    changed_lines: int,
    coverage: float,
    expected: ExpansionReason | None,
) -> None:
    policy = CandidatePolicy(max_candidates=3, broad_patch_lines=500)

    assert (
        policy.should_expand(
            localization_report(ambiguous=ambiguous),
            candidate_evidence(
                eligible=eligible,
                risk=risk,
                changed_lines=changed_lines,
                coverage=coverage,
            ),
            candidate_count=1,
        )
        is expected
    )


def test_candidate_policy_stops_at_hard_candidate_cap() -> None:
    policy = CandidatePolicy(max_candidates=3)

    assert (
        policy.should_expand(
            localization_report(ambiguous=True),
            candidate_evidence(),
            candidate_count=3,
        )
        is None
    )


@pytest.mark.parametrize("max_candidates", [0, 4])
def test_candidate_policy_rejects_candidate_caps_outside_one_to_three(
    max_candidates: int,
) -> None:
    with pytest.raises(ValueError, match="max_candidates"):
        CandidatePolicy(max_candidates=max_candidates)


def test_candidate_selector_never_selects_an_ineligible_candidate() -> None:
    records = [candidate("candidate-1", 1, 2), candidate("candidate-2", 1, 3)]
    evidence = [
        candidate_evidence(candidate_id="candidate-1", eligible=False),
        candidate_evidence(candidate_id="candidate-2", coverage=0.5),
    ]

    selection = CandidateSelector().select(records, evidence)

    assert selection.selected_candidate_id == "candidate-2"
    assert selection.eligible_candidate_ids == ["candidate-2"]


def test_candidate_selector_deduplicates_identical_diffs_by_hash() -> None:
    duplicate = candidate("candidate-1", 1, 2)
    duplicate_two = duplicate.model_copy(update={"candidate_id": "candidate-2"})
    evidence = [
        candidate_evidence(candidate_id="candidate-1"),
        candidate_evidence(candidate_id="candidate-2"),
    ]

    selection = CandidateSelector().select([duplicate_two, duplicate], evidence)

    assert selection.selected_candidate_id == "candidate-1"
    assert selection.eligible_candidate_ids == ["candidate-1"]


def test_candidate_selector_ranks_fewer_required_failures_before_diff_size() -> None:
    records = [candidate("candidate-1", 1, 2), candidate("candidate-2", 1, 3)]
    evidence = [
        candidate_evidence(
            candidate_id="candidate-1",
            changed_lines=1,
            required_failures=["optional-analysis"],
        ),
        candidate_evidence(candidate_id="candidate-2", changed_lines=100),
    ]

    selection = CandidateSelector().select(records, evidence)

    assert selection.selected_candidate_id == "candidate-2"


def test_candidate_selector_returns_ambiguous_for_equal_ranked_candidates() -> None:
    records = [candidate("candidate-1", 1, 2), candidate("candidate-2", 1, 3)]
    evidence = [
        candidate_evidence(candidate_id="candidate-1"),
        candidate_evidence(candidate_id="candidate-2"),
    ]

    selection = CandidateSelector().select(records, evidence)

    assert selection.selected_candidate_id is None
    assert selection.ambiguous is True
    assert selection.eligible_candidate_ids == ["candidate-1", "candidate-2"]


def test_candidate_selector_detects_equal_top_rank_before_output_cap() -> None:
    records = [candidate("candidate-1", 1, 2), candidate("candidate-2", 1, 3)]
    evidence = [
        candidate_evidence(candidate_id="candidate-1"),
        candidate_evidence(candidate_id="candidate-2"),
    ]

    selection = CandidateSelector(max_candidates=1).select(records, evidence)

    assert selection.selected_candidate_id is None
    assert selection.ambiguous is True
    assert selection.eligible_candidate_ids == ["candidate-2"]


def test_candidate_selector_limits_accepted_candidate_ids_to_three() -> None:
    records = [candidate("candidate-1", 1, 2), candidate("candidate-2", 1, 3)]
    evidence = [
        candidate_evidence(candidate_id="candidate-1"),
        candidate_evidence(candidate_id="candidate-2"),
    ]

    selection = CandidateSelector(max_candidates=3).select(records, evidence)

    assert len(selection.eligible_candidate_ids) <= 3


@pytest.mark.parametrize(
    ("records", "evidence", "message"),
    [
        (
            [candidate("candidate-1", 1, 2), candidate("candidate-1", 1, 3)],
            [candidate_evidence(candidate_id="candidate-1")],
            "duplicate candidate IDs",
        ),
        (
            [candidate("candidate-1", 1, 2)],
            [
                candidate_evidence(candidate_id="candidate-1"),
                candidate_evidence(candidate_id="candidate-2"),
            ],
            "do not match",
        ),
        (
            [candidate("candidate-1", 1, 2)],
            [],
            "do not match",
        ),
    ],
)
def test_candidate_selector_rejects_non_one_to_one_candidate_evidence_ids(
    records: list[CandidateRecord], evidence: list[CandidateEvidence], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CandidateSelector().select(records, evidence)


def test_candidate_evaluator_uses_same_baseline_for_each_candidate(tmp_path: Path) -> None:
    root = repository_with_value(tmp_path, 1)
    evaluator = CandidateEvaluator(PatchPolicy(), PatchApplier(), RecordingValidator())

    first = evaluator.evaluate(root, candidate("candidate-1", 1, 2), ["value changes"], 30)
    second = evaluator.evaluate(root, candidate("candidate-2", 1, 3), ["value changes"], 30)

    assert first.restored_to_baseline is True
    assert second.restored_to_baseline is True
    assert (root / "app.py").read_text() == "value = 1\n"


def test_candidate_evaluator_restores_after_validator_exception(tmp_path: Path) -> None:
    class ExplodingValidator:
        def run(
            self, root: Path, *, timeout_seconds: float | None = None
        ) -> ValidationReport:
            del root, timeout_seconds
            raise RuntimeError("validator exploded")

    root = repository_with_value(tmp_path, 1)
    evidence = CandidateEvaluator(PatchPolicy(), PatchApplier(), ExplodingValidator()).evaluate(
        root, candidate("candidate-1", 1, 2), ["value changes"], 30
    )

    assert evidence.eligible is False
    assert evidence.required_failures == ["validation"]
    assert evidence.restored_to_baseline is True
    assert (root / "app.py").read_text() == "value = 1\n"


def test_candidate_evaluator_confines_validator_changes_to_disposable_copy(
    tmp_path: Path,
) -> None:
    class MutatingValidator:
        def run(
            self, root: Path, *, timeout_seconds: float | None = None
        ) -> ValidationReport:
            del timeout_seconds
            (root / "other.py").write_text("value = 99\n")
            (root / "created.py").write_text("created = True\n")
            return ValidationReport(
                checks=[CheckResult(name="pytest", argv=["pytest"], status=CheckStatus.PASSED)]
            )

    root = repository_with_value(tmp_path, 1)
    (root / "other.py").write_text("value = 1\n")

    evidence = CandidateEvaluator(PatchPolicy(), PatchApplier(), MutatingValidator()).evaluate(
        root, candidate("candidate-1", 1, 2), ["value changes"], 30
    )

    assert evidence.eligible is True
    assert "repository-drift" not in evidence.required_failures
    assert evidence.restored_to_baseline is True
    assert (root / "app.py").read_text() == "value = 1\n"
    assert (root / "other.py").read_text() == "value = 1\n"
    assert not (root / "created.py").exists()


def test_candidate_evaluator_uses_disposable_root_for_patch_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository_with_value(tmp_path, 1)
    del monkeypatch
    applier = PatchApplier()

    evidence = CandidateEvaluator(PatchPolicy(), applier, RecordingValidator()).evaluate(
        root, candidate("candidate-1", 1, 2), ["value changes"], 30
    )

    assert evidence.eligible is True
    assert evidence.restored_to_baseline is True
    assert evidence.required_failures == []
    assert (root / "app.py").read_text() == "value = 1\n"


def test_candidate_evaluator_detects_real_root_drift_without_restoring_it(tmp_path: Path) -> None:
    class MaliciousValidator:
        def run(
            self, evaluation_root: Path, *, timeout_seconds: float | None = None
        ) -> ValidationReport:
            del evaluation_root, timeout_seconds
            (root / ".git" / "config").write_text("changed\n")
            os.chmod(root / "package", 0o500)
            (root / "other.py").unlink()
            (root / "other.py").symlink_to("app.py")
            return ValidationReport(
                checks=[CheckResult(name="pytest", argv=["pytest"], status=CheckStatus.PASSED)]
            )

    root = repository_with_value(tmp_path, 1)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("original\n")
    (root / "package").mkdir()
    os.chmod(root / "package", 0o700)
    (root / "other.py").write_text("other = 1\n")

    evidence = CandidateEvaluator(PatchPolicy(), PatchApplier(), MaliciousValidator()).evaluate(
        root, candidate("candidate-1", 1, 2), ["value changes"], 30
    )

    assert evidence.eligible is False
    assert evidence.restored_to_baseline is False
    assert "repository-drift" in evidence.required_failures
    assert (root / ".git" / "config").read_text() == "changed\n"
    assert (root / "other.py").is_symlink()
    assert (root / "package").stat().st_mode & 0o777 == 0o500


def test_candidate_evaluator_copies_safe_symlinks_and_cleans_special_eval_nodes(
    tmp_path: Path,
) -> None:
    class EvalOnlyValidator:
        def run(
            self, evaluation_root: Path, *, timeout_seconds: float | None = None
        ) -> ValidationReport:
            del timeout_seconds
            assert evaluation_root != root
            assert (evaluation_root / "safe-link.py").is_symlink()
            (evaluation_root / "created-link.py").symlink_to("app.py")
            os.mkfifo(evaluation_root / "created.fifo")
            return ValidationReport(
                checks=[CheckResult(name="pytest", argv=["pytest"], status=CheckStatus.PASSED)]
            )

    root = repository_with_value(tmp_path, 1)
    (root / "safe-link.py").symlink_to("app.py")
    unchanged_mtime = (root / "app.py").stat().st_mtime_ns

    evidence = CandidateEvaluator(PatchPolicy(), PatchApplier(), EvalOnlyValidator()).evaluate(
        root, candidate("candidate-1", 1, 2), ["value changes"], 30
    )

    assert evidence.eligible is True
    assert evidence.restored_to_baseline is True
    assert (root / "safe-link.py").is_symlink()
    assert not (root / "created-link.py").exists()
    assert not (root / "created.fifo").exists()
    assert (root / "app.py").stat().st_mtime_ns == unchanged_mtime


def test_candidate_evaluator_passes_remaining_timeout_after_copy_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    class TimeoutRecordingValidator:
        received: float | None = None

        def run(
            self, root: Path, *, timeout_seconds: float | None = None
        ) -> ValidationReport:
            del root
            self.received = timeout_seconds
            return ValidationReport(
                checks=[CheckResult(name="pytest", argv=["pytest"], status=CheckStatus.PASSED)]
            )

    clock = Clock()
    root = repository_with_value(tmp_path, 1)
    validator = TimeoutRecordingValidator()
    evaluator = CandidateEvaluator(PatchPolicy(), PatchApplier(), validator)
    from repogent import candidates as candidates_module

    original_copy = candidates_module._copy_for_evaluation

    def delayed_copy(source: Path, destination: Path, *, deadline: float) -> None:
        clock.advance(3)
        original_copy(source, destination, deadline=deadline)

    monkeypatch.setattr("repogent.candidates.time.monotonic", clock.monotonic)
    monkeypatch.setattr(candidates_module, "_copy_for_evaluation", delayed_copy)

    evidence = evaluator.evaluate(root, candidate("candidate-1", 1, 2), ["value changes"], 10)

    assert evidence.eligible is True
    assert validator.received == 7


def test_candidate_evaluator_times_out_during_copy_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    root = repository_with_value(tmp_path, 1)
    from repogent import candidates as candidates_module

    original_copy = candidates_module._copy_for_evaluation

    def expired_copy(source: Path, destination: Path, *, deadline: float) -> None:
        clock.now = 11
        original_copy(source, destination, deadline=deadline)

    monkeypatch.setattr("repogent.candidates.time.monotonic", clock.monotonic)
    monkeypatch.setattr(candidates_module, "_copy_for_evaluation", expired_copy)

    evidence = CandidateEvaluator(PatchPolicy(), PatchApplier(), RecordingValidator()).evaluate(
        root, candidate("candidate-1", 1, 2), ["value changes"], 10
    )

    assert evidence.eligible is False
    assert evidence.required_failures == ["timeout"]


def test_candidate_evaluator_rejects_unmapped_acceptance_without_mutation(tmp_path: Path) -> None:
    root = repository_with_value(tmp_path, 1)

    evidence = CandidateEvaluator(PatchPolicy(), PatchApplier(), RecordingValidator()).evaluate(
        root, candidate("candidate-1", 1, 2), ["different criterion"], 30
    )

    assert evidence.eligible is False
    assert evidence.required_failures == ["acceptance-mapping"]
    assert evidence.changed_files == 0
    assert evidence.changed_lines == 0
    assert (root / "app.py").read_text() == "value = 1\n"
