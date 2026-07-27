import hashlib
from pathlib import Path

import pytest

from repogent.candidates import (
    CandidateEvaluationError,
    PatchPreviewer,
    patch_preview_digest,
)
from repogent.domain import (
    CandidateRecord,
    ExecutionMode,
    IsolationLevel,
    PatchProposal,
    ProviderUsage,
    VerificationStatus,
)
from repogent.executor_selection import FixedExecutorSelector, PreparedExecutor
from repogent.patching import PatchPolicy
from repogent.preflight import PreflightReport
from repogent.workflow import ExecutorSelectionRejected


class ExplodingValidator:
    def run(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("validator must not run")


class ExplodingPatchApplier:
    def apply(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("applier must not run")


def make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("value = 1\n")
    return repository


def safe_candidate(*, diff: str | None = None) -> CandidateRecord:
    proposal = PatchProposal(
        summary="Change value",
        diff=diff
        or "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n",
        acceptance_criteria_addressed=["health endpoint exists"],
        focused_tests=["pytest"],
    )
    return CandidateRecord(
        candidate_id="candidate-1",
        proposal=proposal,
        generation_reason="initial implementation",
        diff_sha256=hashlib.sha256(proposal.diff.encode()).hexdigest(),
        usage=ProviderUsage(model="scripted"),
    )


def test_static_preview_never_invokes_validator_or_patch_applier(tmp_path: Path) -> None:
    validator = ExplodingValidator()
    applier = ExplodingPatchApplier()

    preview = PatchPreviewer(PatchPolicy()).preview(
        root=make_repository(tmp_path),
        candidate=safe_candidate(),
        acceptance_criteria=["health endpoint exists"],
    )

    assert validator is not None and applier is not None
    assert preview.verification_status is VerificationStatus.UNVALIDATED
    assert preview.touched_paths == ["app.py"]
    assert preview.changed_files == 1
    assert preview.changed_lines == 2
    assert preview.acceptance_criteria_coverage == 1


def test_patch_preview_digest_is_canonical_and_sensitive_to_exact_diff(
    tmp_path: Path,
) -> None:
    previewer = PatchPreviewer(PatchPolicy())
    repository = make_repository(tmp_path)
    preview = previewer.preview(
        repository,
        safe_candidate(),
        ["health endpoint exists"],
    )
    changed = previewer.preview(
        repository,
        safe_candidate(
            diff="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 3\n"
        ),
        ["health endpoint exists"],
    )

    assert patch_preview_digest(preview) == patch_preview_digest(
        preview.model_validate(preview.model_dump(mode="json"))
    )
    assert patch_preview_digest(preview) != patch_preview_digest(changed)


@pytest.mark.parametrize(
    "replacement",
    [
        "api_key = 'super-secret-value'",
        "token: github_pat_abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_static_preview_fails_closed_when_exact_diff_would_be_sanitized(
    tmp_path: Path, replacement: str
) -> None:
    diff = f"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+{replacement}\n"

    with pytest.raises(CandidateEvaluationError, match="secret-like patch content"):
        PatchPreviewer(PatchPolicy()).preview(
            make_repository(tmp_path),
            safe_candidate(diff=diff),
            ["health endpoint exists"],
        )


def test_static_preview_rejects_acceptance_criteria_outside_requirements(
    tmp_path: Path,
) -> None:
    with pytest.raises(CandidateEvaluationError, match="outside the supplied requirements"):
        PatchPreviewer(PatchPolicy()).preview(
            make_repository(tmp_path), safe_candidate(), ["different criterion"]
        )


def test_fixed_executor_selector_returns_the_prepared_executor(tmp_path: Path) -> None:
    prepared = PreparedExecutor(
        mode=ExecutionMode.LOCAL,
        isolation_level=IsolationLevel.REDUCED_ISOLATION,
        preflight=PreflightReport(
            checks=[], git_commit=None, dirty=False, repository_fingerprint="repository"
        ),
        validator=ExplodingValidator(),  # type: ignore[arg-type]
    )
    preview = PatchPreviewer(PatchPolicy()).preview(
        make_repository(tmp_path), safe_candidate(), ["health endpoint exists"]
    )

    selected = FixedExecutorSelector(prepared).select(preview, timeout_seconds=10)

    assert selected is prepared


def test_fixed_executor_selector_rejects_a_missing_preview() -> None:
    prepared = PreparedExecutor(
        mode=ExecutionMode.LOCAL,
        isolation_level=IsolationLevel.REDUCED_ISOLATION,
        preflight=PreflightReport(
            checks=[], git_commit=None, dirty=False, repository_fingerprint="repository"
        ),
        validator=ExplodingValidator(),  # type: ignore[arg-type]
    )

    with pytest.raises(ExecutorSelectionRejected, match="preview"):
        FixedExecutorSelector(prepared).select(None, timeout_seconds=10)  # type: ignore[arg-type]
