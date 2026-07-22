from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field

from repogent.domain import (
    CandidateEvidence,
    CandidateRecord,
    CandidateSelection,
    CheckResult,
    CheckStatus,
    RiskLevel,
    ValidationReport,
    VerificationStatus,
    VersionedModel,
)
from repogent.localization import LocalizationReport
from repogent.patching import PatchApplier, PatchPolicy, PatchPolicyError, ValidatedPatch
from repogent.sanitization import redact_text, sanitize_data

_EVALUATION_COPY_IGNORES = {
    ".git",
    ".hg",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


class EvaluationTimeout(TimeoutError):
    pass


class CandidateEvaluationError(RuntimeError):
    pass


class PatchPreview(VersionedModel):
    candidate: CandidateRecord
    touched_paths: list[str] = Field(max_length=20)
    changed_files: int = Field(ge=0, le=20)
    changed_lines: int = Field(ge=0)
    acceptance_criteria_coverage: float = Field(ge=0, le=1)
    verification_status: VerificationStatus = VerificationStatus.UNVALIDATED


def patch_preview_digest(preview: PatchPreview) -> str:
    canonical = json.dumps(
        preview.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class PatchPreviewer:
    def __init__(
        self,
        patch_policy: PatchPolicy,
        explicit_secrets: Sequence[str] = (),
    ) -> None:
        self.patch_policy = patch_policy
        self.explicit_secrets = tuple(explicit_secrets)

    def preview(
        self,
        root: Path,
        candidate: CandidateRecord,
        acceptance_criteria: Sequence[str],
    ) -> PatchPreview:
        unknown = set(candidate.proposal.acceptance_criteria_addressed) - set(
            acceptance_criteria
        )
        if unknown:
            raise CandidateEvaluationError(
                "proposal addresses criteria outside the supplied requirements"
            )
        exact_diff = candidate.proposal.diff
        if redact_text(exact_diff, self.explicit_secrets) != exact_diff:
            raise CandidateEvaluationError("preview contains secret-like patch content")
        sanitized = sanitize_data({"diff": exact_diff}, self.explicit_secrets)
        if not isinstance(sanitized, dict) or sanitized.get("diff") != exact_diff:
            raise CandidateEvaluationError("preview contains secret-like patch content")
        validated = self.patch_policy.validate(root, candidate.proposal)
        return PatchPreview(
            candidate=candidate,
            touched_paths=[path.as_posix() for path in validated.touched_paths],
            changed_files=len(validated.touched_paths),
            changed_lines=validated.changed_lines,
            acceptance_criteria_coverage=(
                len(candidate.proposal.acceptance_criteria_addressed)
                / max(1, len(acceptance_criteria))
            ),
        )


def _deadline_after(timeout_seconds: float) -> float:
    if timeout_seconds <= 0:
        raise EvaluationTimeout("candidate evaluation timeout exceeded")
    return time.monotonic() + timeout_seconds


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise EvaluationTimeout("candidate evaluation timeout exceeded")
    return remaining


class Validator(Protocol):
    def run(self, root: Path, *, timeout_seconds: float | None = None) -> ValidationReport: ...


@dataclass(frozen=True)
class NodeFingerprint:
    kind: str
    mode: int
    digest: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class RepositoryIntegritySnapshot:
    """Read-only whole-tree state used only to detect real-checkout drift."""

    entries: dict[Path, NodeFingerprint]

    @classmethod
    def capture(
        cls, root: Path, *, deadline: float | None = None
    ) -> RepositoryIntegritySnapshot:
        if deadline is not None:
            _remaining(deadline)
        repository = root.resolve(strict=True)
        entries: dict[Path, NodeFingerprint] = {
            Path(): NodeFingerprint("directory", stat.S_IMODE(repository.stat().st_mode))
        }
        descriptor = os.open(repository, PatchApplier._directory_flags())

        def walk(directory_fd: int, relative: Path) -> None:
            try:
                if deadline is not None:
                    _remaining(deadline)
                with os.scandir(directory_fd) as scanned:
                    names = sorted(entry.name for entry in scanned)
                for name in names:
                    if deadline is not None:
                        _remaining(deadline)
                    path = relative / name
                    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    mode = stat.S_IMODE(metadata.st_mode)
                    if stat.S_ISDIR(metadata.st_mode):
                        entries[path] = NodeFingerprint("directory", mode)
                        child = os.open(
                            name, PatchApplier._directory_flags(), dir_fd=directory_fd
                        )
                        walk(child, path)
                    elif stat.S_ISREG(metadata.st_mode):
                        entries[path] = NodeFingerprint(
                            "regular", mode, digest=_digest_file(directory_fd, name, deadline)
                        )
                    elif stat.S_ISLNK(metadata.st_mode):
                        entries[path] = NodeFingerprint(
                            "symlink", mode, target=os.readlink(name, dir_fd=directory_fd)
                        )
                    else:
                        entries[path] = NodeFingerprint(_special_kind(metadata.st_mode), mode)
            finally:
                os.close(directory_fd)

        walk(descriptor, Path())
        return cls(entries)

    def matches(self, root: Path, *, deadline: float | None = None) -> bool:
        try:
            current = self.capture(root, deadline=deadline)
        except (OSError, ValueError, EvaluationTimeout):
            return False
        return current == self


def _digest_file(directory_fd: int, name: str, deadline: float | None = None) -> str:
    import hashlib

    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65_536):
            if deadline is not None:
                _remaining(deadline)
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _special_kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISCHR(mode):
        return "character"
    if stat.S_ISBLK(mode):
        return "block"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "unknown"


def _copy_for_evaluation(source: Path, destination: Path, *, deadline: float) -> None:
    """Copy only into a disposable tree; never mutate or follow source symlinks."""

    _remaining(deadline)
    repository = source.resolve(strict=True)
    destination.mkdir(mode=0o700)
    source_fd = os.open(repository, PatchApplier._directory_flags())

    def copy_tree(directory_fd: int, source_directory: Path, target_directory: Path) -> None:
        try:
            _remaining(deadline)
            with os.scandir(directory_fd) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                _remaining(deadline)
                if name in _EVALUATION_COPY_IGNORES:
                    continue
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                target = target_directory / name
                if stat.S_ISDIR(metadata.st_mode):
                    target.mkdir(mode=stat.S_IMODE(metadata.st_mode))
                    child = os.open(
                        name, PatchApplier._directory_flags(), dir_fd=directory_fd
                    )
                    copy_tree(child, source_directory / name, target)
                    os.chmod(target, stat.S_IMODE(metadata.st_mode))
                elif stat.S_ISREG(metadata.st_mode):
                    _copy_regular_file(directory_fd, name, target, metadata, deadline)
                elif stat.S_ISLNK(metadata.st_mode):
                    link_target = os.readlink(name, dir_fd=directory_fd)
                    if _is_safe_symlink(repository, source_directory, link_target):
                        os.symlink(link_target, target)
        finally:
            os.close(directory_fd)

    copy_tree(source_fd, repository, destination)


def _is_safe_symlink(repository: Path, parent: Path, target: str) -> bool:
    if os.path.isabs(target):
        return False
    resolved = (parent / target).resolve()
    return resolved == repository or repository in resolved.parents


def _copy_regular_file(
    source_fd: int,
    name: str,
    target: Path,
    metadata: os.stat_result,
    deadline: float,
) -> None:
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
    destination = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IMODE(metadata.st_mode),
    )
    try:
        while chunk := os.read(source, 65_536):
            _remaining(deadline)
            _write_all(destination, chunk)
        os.fchmod(destination, stat.S_IMODE(metadata.st_mode))
    finally:
        os.close(source)
        os.close(destination)
    os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written == 0:
            raise OSError("short write while creating evaluation copy")
        offset += written


class ExpansionReason(StrEnum):
    AMBIGUOUS_LOCALIZATION = "ambiguous_localization"
    VALIDATION_FAILED = "validation_failed"
    HIGH_RISK = "high_risk"
    BROAD_PATCH = "broad_patch"
    INCOMPLETE_ACCEPTANCE = "incomplete_acceptance"


class CandidatePolicy:
    def __init__(self, *, max_candidates: int = 3, broad_patch_lines: int = 500) -> None:
        if not 1 <= max_candidates <= 3:
            raise ValueError("max_candidates must be between 1 and 3")
        if broad_patch_lines < 1:
            raise ValueError("broad_patch_lines must be positive")
        self.max_candidates = max_candidates
        self.broad_patch_lines = broad_patch_lines

    def should_expand(
        self,
        localization: LocalizationReport,
        evidence: CandidateEvidence,
        candidate_count: int,
    ) -> ExpansionReason | None:
        if candidate_count >= self.max_candidates:
            return None
        if not evidence.eligible:
            return ExpansionReason.VALIDATION_FAILED
        if localization.ambiguous:
            return ExpansionReason.AMBIGUOUS_LOCALIZATION
        if evidence.risk_level is RiskLevel.HIGH:
            return ExpansionReason.HIGH_RISK
        if evidence.changed_lines > self.broad_patch_lines:
            return ExpansionReason.BROAD_PATCH
        if evidence.acceptance_criteria_coverage < 1:
            return ExpansionReason.INCOMPLETE_ACCEPTANCE
        return None


class CandidateSelector:
    def __init__(self, *, max_candidates: int = 3) -> None:
        if not 1 <= max_candidates <= 3:
            raise ValueError("max_candidates must be between 1 and 3")
        self.max_candidates = max_candidates

    def select(
        self,
        candidates: Sequence[CandidateRecord],
        evidence: Sequence[CandidateEvidence],
    ) -> CandidateSelection:
        records_by_id = _records_by_id(candidates)
        evidence_by_id = _evidence_by_id(evidence)
        if set(records_by_id) != set(evidence_by_id):
            raise ValueError("candidate and evidence IDs do not match")

        eligible = [
            (record, evidence_by_id[candidate_id])
            for candidate_id, record in sorted(records_by_id.items())
            if evidence_by_id[candidate_id].eligible
        ]
        unique = _deduplicate_diffs(eligible)
        ranked = sorted(
            unique,
            key=lambda pair: (_rank(pair[0], pair[1]), pair[0].candidate_id),
            reverse=True,
        )

        if not ranked:
            return CandidateSelection(
                selected_candidate_id=None,
                eligible_candidate_ids=[],
                reason="no candidate passed required validation",
            )

        accepted = ranked[: self.max_candidates]
        eligible_ids = sorted(record.candidate_id for record, _ in accepted)
        if len(ranked) > 1 and _rank(*ranked[0]) == _rank(*ranked[1]):
            return CandidateSelection(
                selected_candidate_id=None,
                eligible_candidate_ids=eligible_ids,
                ambiguous=True,
                reason=(
                    "eligible candidates have equal ranking evidence: required_failures, "
                    "acceptance_criteria_coverage, skipped_checks, changed_files, "
                    "changed_lines, estimated_cost_usd"
                ),
            )

        winner, winner_evidence = accepted[0]
        return CandidateSelection(
            selected_candidate_id=winner.candidate_id,
            eligible_candidate_ids=eligible_ids,
            reason=_selection_reason(winner, winner_evidence, accepted[1:]),
        )


def _records_by_id(candidates: Sequence[CandidateRecord]) -> dict[str, CandidateRecord]:
    records_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(records_by_id) != len(candidates):
        raise ValueError("duplicate candidate IDs")
    return records_by_id


def _evidence_by_id(evidence: Sequence[CandidateEvidence]) -> dict[str, CandidateEvidence]:
    evidence_by_id = {item.candidate_id: item for item in evidence}
    if len(evidence_by_id) != len(evidence):
        raise ValueError("duplicate evidence IDs")
    return evidence_by_id


def _deduplicate_diffs(
    eligible: Sequence[tuple[CandidateRecord, CandidateEvidence]],
) -> list[tuple[CandidateRecord, CandidateEvidence]]:
    by_hash: dict[str, tuple[CandidateRecord, CandidateEvidence]] = {}
    for record, item in eligible:
        current = by_hash.get(record.diff_sha256)
        if current is None or _is_better_duplicate((record, item), current):
            by_hash[record.diff_sha256] = (record, item)
    return list(by_hash.values())


def _is_better_duplicate(
    contender: tuple[CandidateRecord, CandidateEvidence],
    incumbent: tuple[CandidateRecord, CandidateEvidence],
) -> bool:
    contender_rank = _rank(*contender)
    incumbent_rank = _rank(*incumbent)
    return contender_rank > incumbent_rank or (
        contender_rank == incumbent_rank
        and contender[0].candidate_id < incumbent[0].candidate_id
    )


def _rank(
    candidate: CandidateRecord, evidence: CandidateEvidence
) -> tuple[int, float, int, int, int, float]:
    return (
        -len(evidence.required_failures),
        evidence.acceptance_criteria_coverage,
        -len(evidence.skipped_checks),
        -evidence.changed_files,
        -evidence.changed_lines,
        -float(candidate.usage.estimated_cost_usd),
    )


def _selection_reason(
    candidate: CandidateRecord,
    evidence: CandidateEvidence,
    alternatives: Sequence[tuple[CandidateRecord, CandidateEvidence]],
) -> str:
    fields = _decisive_fields(candidate, evidence, alternatives)
    return (
        f"selected {candidate.candidate_id} on {', '.join(fields)}: "
        f"required_failures={len(evidence.required_failures)}, "
        f"acceptance_criteria_coverage={evidence.acceptance_criteria_coverage}, "
        f"skipped_checks={len(evidence.skipped_checks)}, "
        f"changed_files={evidence.changed_files}, "
        f"changed_lines={evidence.changed_lines}, "
        f"estimated_cost_usd={float(candidate.usage.estimated_cost_usd)}"
    )


def _decisive_fields(
    candidate: CandidateRecord,
    evidence: CandidateEvidence,
    alternatives: Sequence[tuple[CandidateRecord, CandidateEvidence]],
) -> list[str]:
    names = [
        "required_failures",
        "acceptance_criteria_coverage",
        "skipped_checks",
        "changed_files",
        "changed_lines",
        "estimated_cost_usd",
    ]
    if not alternatives:
        return names
    winner_rank = _rank(candidate, evidence)
    runner_up_rank = _rank(*alternatives[0])
    return [
        name for name, winner, runner_up in zip(names, winner_rank, runner_up_rank, strict=True)
        if winner != runner_up
    ]


class CandidateEvaluator:
    def __init__(
        self,
        patch_policy: PatchPolicy,
        patch_applier: PatchApplier,
        validator: Validator,
    ) -> None:
        self.patch_policy = patch_policy
        self.patch_applier = patch_applier
        self.validator = validator

    def evaluate(
        self,
        root: Path,
        candidate: CandidateRecord,
        acceptance_criteria: Sequence[str],
        timeout_seconds: float,
    ) -> CandidateEvidence:
        started = time.monotonic()
        try:
            deadline = _deadline_after(timeout_seconds)
        except EvaluationTimeout as error:
            return self._failure_evidence(candidate.candidate_id, "timeout", str(error), started)
        unknown_criteria = set(candidate.proposal.acceptance_criteria_addressed) - set(
            acceptance_criteria
        )
        if unknown_criteria:
            return self._failure_evidence(
                candidate.candidate_id,
                "acceptance-mapping",
                "proposal addresses criteria outside the supplied requirements: "
                + ", ".join(sorted(unknown_criteria)),
                started,
            )

        validation = ValidationReport(checks=[])
        baseline: RepositoryIntegritySnapshot | None = None
        validated: ValidatedPatch | None = None
        evaluation_error: Exception | None = None
        validator_error: Exception | None = None
        timeout_error: EvaluationTimeout | None = None

        try:
            baseline = self.capture_baseline(root, deadline=deadline)
            with tempfile.TemporaryDirectory(prefix="repogent-candidate-") as temporary:
                evaluation_root = Path(temporary) / "repository"
                _copy_for_evaluation(root, evaluation_root, deadline=deadline)
                _remaining(deadline)
                try:
                    validated = self.patch_policy.validate(evaluation_root, candidate.proposal)
                except PatchPolicyError as error:
                    evaluation_error = error
                if validated is not None:
                    try:
                        with self.patch_applier.transaction(evaluation_root, validated):
                            try:
                                validation = self.validator.run(
                                    evaluation_root, timeout_seconds=_remaining(deadline)
                                )
                            except Exception as error:
                                validator_error = error
                    except Exception as error:
                        evaluation_error = error
        except EvaluationTimeout as error:
            timeout_error = error
        except Exception as error:
            evaluation_error = error

        restored_to_baseline = False
        if baseline is not None:
            try:
                restored_to_baseline = baseline == RepositoryIntegritySnapshot.capture(
                    root, deadline=deadline
                )
            except EvaluationTimeout as error:
                timeout_error = error
            except (OSError, ValueError):
                restored_to_baseline = False

        if validator_error is not None:
            validation = _with_failure(validation, "validation", str(validator_error))
        if timeout_error is not None:
            validation = _with_failure(validation, "timeout", str(timeout_error))
        if evaluation_error is not None and not isinstance(evaluation_error, PatchPolicyError):
            validation = _with_failure(validation, "evaluation", str(evaluation_error))
        if not restored_to_baseline:
            validation = _with_failure(
                validation,
                "repository-drift",
                "real repository state changed during candidate evaluation",
            )
        if validated is None:
            failure_name = (
                "timeout"
                if timeout_error is not None
                else "patch-policy"
                if isinstance(evaluation_error, PatchPolicyError)
                else "evaluation"
            )
            failure_reason = (
                str(timeout_error)
                if timeout_error is not None
                else str(evaluation_error)
                if evaluation_error is not None
                else "candidate evaluation did not produce a validated patch"
            )
            return self._failure_evidence(
                candidate.candidate_id,
                failure_name,
                failure_reason,
                started,
                restored_to_baseline=restored_to_baseline,
                validation=validation,
            )

        required_failures = [
            check.name
            for check in validation.checks
            if check.required and check.status is not CheckStatus.PASSED
        ]
        skipped_checks = [
            check.name for check in validation.checks if check.status is CheckStatus.SKIPPED
        ]
        coverage = _acceptance_coverage(
            validation, candidate.proposal.acceptance_criteria_addressed, acceptance_criteria
        )
        return CandidateEvidence(
            candidate_id=candidate.candidate_id,
            validation=validation,
            acceptance_criteria_coverage=coverage,
            risk_level=_risk_level(validated),
            changed_files=len(validated.touched_paths),
            changed_lines=validated.changed_lines,
            duration_seconds=max(0.0, time.monotonic() - started),
            required_failures=required_failures,
            skipped_checks=skipped_checks,
            restored_to_baseline=restored_to_baseline,
        )

    def validate_isolated(
        self,
        root: Path,
        *,
        timeout_seconds: float,
        baseline: RepositoryIntegritySnapshot | None = None,
    ) -> tuple[ValidationReport, bool]:
        deadline = _deadline_after(timeout_seconds)
        baseline = baseline or RepositoryIntegritySnapshot.capture(root, deadline=deadline)
        validation = ValidationReport(checks=[])
        error: Exception | None = None
        timeout_error: EvaluationTimeout | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="repogent-final-validation-") as temporary:
                evaluation_root = Path(temporary) / "repository"
                _copy_for_evaluation(root, evaluation_root, deadline=deadline)
                validation = self.validator.run(
                    evaluation_root, timeout_seconds=_remaining(deadline)
                )
        except EvaluationTimeout as caught:
            timeout_error = caught
        except Exception as caught:
            error = caught
        stable = False
        try:
            stable = baseline == RepositoryIntegritySnapshot.capture(root, deadline=deadline)
        except EvaluationTimeout as caught:
            timeout_error = caught
        except (OSError, ValueError):
            stable = False
        if timeout_error is not None:
            validation = _with_failure(validation, "timeout", str(timeout_error))
        if error is not None:
            validation = _with_failure(validation, "validation", str(error))
        if not stable:
            validation = _with_failure(
                validation,
                "repository-drift",
                "real repository state changed during isolated final validation",
            )
        return validation, stable

    def capture_baseline(
        self, root: Path, *, deadline: float | None = None
    ) -> RepositoryIntegritySnapshot:
        return RepositoryIntegritySnapshot.capture(root, deadline=deadline)

    @staticmethod
    def _failure_evidence(
        candidate_id: str,
        name: str,
        reason: str,
        started: float,
        *,
        restored_to_baseline: bool = True,
        validation: ValidationReport | None = None,
    ) -> CandidateEvidence:
        validation = validation or ValidationReport(checks=[])
        if not any(check.name == name for check in validation.checks):
            validation = _with_failure(validation, name, reason)
        required_failures = [
            check.name
            for check in validation.checks
            if check.required and check.status is not CheckStatus.PASSED
        ]
        skipped_checks = [
            check.name for check in validation.checks if check.status is CheckStatus.SKIPPED
        ]
        return CandidateEvidence(
            candidate_id=candidate_id,
            validation=validation,
            acceptance_criteria_coverage=0.0,
            risk_level=RiskLevel.LOW,
            changed_files=0,
            changed_lines=0,
            duration_seconds=max(0.0, time.monotonic() - started),
            required_failures=required_failures,
            skipped_checks=skipped_checks,
            restored_to_baseline=restored_to_baseline,
        )


def _with_failure(validation: ValidationReport, name: str, reason: str) -> ValidationReport:
    """Record one authoritative failure per check name.

    A timeout can occur while both the disposable copy and the post-run
    integrity check are executing.  Replacing an existing same-named result
    keeps the report unambiguous while retaining distinct failures such as
    ``repository-drift``.
    """
    return ValidationReport(
        checks=[
            *(check for check in validation.checks if check.name != name),
            CheckResult(
                name=name,
                argv=[],
                status=CheckStatus.FAILED,
                reason=reason,
                required=True,
            ),
        ]
    )


def _acceptance_coverage(
    validation: ValidationReport,
    addressed: Sequence[str],
    required: Sequence[str],
) -> float:
    if not validation.passed:
        return 0.0
    required_set = set(required)
    if not required_set:
        return 1.0
    return len(set(addressed) & required_set) / len(required_set)


def _risk_level(patch: ValidatedPatch) -> RiskLevel:
    changed_files = len(patch.touched_paths)
    changed_lines = patch.changed_lines
    if changed_files > 5 or changed_lines > 500:
        risk = RiskLevel.HIGH
    elif changed_files > 2 or changed_lines > 100:
        risk = RiskLevel.MEDIUM
    else:
        risk = RiskLevel.LOW

    if any(_is_sensitive_path(path) for path in patch.touched_paths):
        return _raise_risk(risk)
    return risk


def _is_sensitive_path(path: Path) -> bool:
    name = path.name
    return (
        path.as_posix() == "pyproject.toml"
        or name.endswith(".lock")
        or name in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
        or ".github" in path.parts
        or name == "__init__.py"
    )


def _raise_risk(risk: RiskLevel) -> RiskLevel:
    if risk is RiskLevel.LOW:
        return RiskLevel.MEDIUM
    return RiskLevel.HIGH
