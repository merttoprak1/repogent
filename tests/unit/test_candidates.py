import hashlib
from pathlib import Path

import pytest

from repogent.candidates import CandidateEvaluator
from repogent.domain import (
    CandidateRecord,
    CheckResult,
    CheckStatus,
    PatchProposal,
    ProviderUsage,
    ValidationReport,
)
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


def test_candidate_evaluator_marks_restoration_mismatch_ineligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository_with_value(tmp_path, 1)
    applier = PatchApplier()
    monkeypatch.setattr(applier, "restore", lambda *_args: None)

    evidence = CandidateEvaluator(PatchPolicy(), applier, RecordingValidator()).evaluate(
        root, candidate("candidate-1", 1, 2), ["value changes"], 30
    )

    assert evidence.eligible is False
    assert evidence.restored_to_baseline is False
    assert evidence.required_failures == ["restoration"]
    assert (root / "app.py").read_text() == "value = 2\n"


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
