from __future__ import annotations

import os
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from repogent.domain import (
    CandidateEvidence,
    CandidateRecord,
    CandidateSelection,
    CheckResult,
    CheckStatus,
    RiskLevel,
    ValidationReport,
)
from repogent.localization import LocalizationReport
from repogent.patching import PatchApplier, PatchPolicy, PatchPolicyError, Snapshot, ValidatedPatch
from repogent.repository import IGNORED_DIRECTORIES


class Validator(Protocol):
    def run(self, root: Path, *, timeout_seconds: float | None = None) -> ValidationReport: ...


@dataclass(frozen=True)
class RepositoryBaseline:
    """A complete regular-file baseline outside VCS metadata."""

    files: dict[Path, Snapshot]
    directories: frozenset[Path]

    @classmethod
    def capture(cls, root: Path, applier: PatchApplier) -> RepositoryBaseline:
        files, directories = _repository_paths(root)
        return cls(
            files={path: applier._snapshot(root, path) for path in files},
            directories=frozenset(directories),
        )

    def restore(self, root: Path, applier: PatchApplier) -> None:
        current_files, current_directories = _repository_paths(root)
        errors: list[Exception] = []
        for path in sorted(
            current_files - set(self.files), key=lambda item: len(item.parts), reverse=True
        ):
            try:
                applier._restore_one(root, path, Snapshot(existed=False, content=b"", mode=None))
            except Exception as error:
                errors.append(error)
        for path, snapshot in self.files.items():
            try:
                applier._restore_one(root, path, snapshot)
            except Exception as error:
                errors.append(error)
        for path in sorted(
            current_directories - set(self.directories),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                applier._remove_empty_directory(root, path)
            except Exception as error:
                errors.append(error)
        if errors:
            raise RuntimeError(
                "repository baseline restoration failed: " + "; ".join(map(str, errors))
            )

    def matches(self, root: Path, applier: PatchApplier) -> bool:
        try:
            current = self.capture(root, applier)
        except Exception:
            return False
        return current.files == self.files and current.directories == self.directories


def _repository_paths(root: Path) -> tuple[set[Path], set[Path]]:
    repository = root.resolve(strict=True)
    files: set[Path] = set()
    directories: set[Path] = set()

    def walk(descriptor: int, relative: Path) -> None:
        try:
            with os.scandir(descriptor) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                if name in IGNORED_DIRECTORIES:
                    continue
                path = relative / name
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    directories.add(path)
                    child = os.open(name, PatchApplier._directory_flags(), dir_fd=descriptor)
                    walk(child, path)
                elif stat.S_ISREG(metadata.st_mode):
                    files.add(path)
                else:
                    raise PatchPolicyError(f"cannot establish repository baseline for {path}")
        finally:
            os.close(descriptor)

    descriptor = os.open(repository, PatchApplier._directory_flags())
    walk(descriptor, Path())
    return files, directories


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
            validated = self.patch_policy.validate(root, candidate.proposal)
        except PatchPolicyError as error:
            return self._failure_evidence(
                candidate.candidate_id,
                "patch-policy",
                str(error),
                started,
            )

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

        return self._evaluate_validated(
            root,
            candidate,
            validated,
            acceptance_criteria,
            timeout_seconds,
            started,
        )

    def capture_baseline(self, root: Path) -> RepositoryBaseline:
        return RepositoryBaseline.capture(root, self.patch_applier)

    def _evaluate_validated(
        self,
        root: Path,
        candidate: CandidateRecord,
        validated: ValidatedPatch,
        acceptance_criteria: Sequence[str],
        timeout_seconds: float,
        started: float,
    ) -> CandidateEvidence:
        validation = ValidationReport(checks=[])
        baseline: RepositoryBaseline | None = None
        evaluation_error: Exception | None = None
        validator_error: Exception | None = None
        unexpected_mutation = False

        try:
            baseline = self.capture_baseline(root)
            try:
                with self.patch_applier.transaction(root, validated):
                    try:
                        validation = self.validator.run(root, timeout_seconds=timeout_seconds)
                    except Exception as error:
                        validator_error = error
            except Exception as error:
                evaluation_error = error
        except Exception as error:
            evaluation_error = error

        if baseline is not None:
            unexpected_mutation = not baseline.matches(root, self.patch_applier)
            try:
                baseline.restore(root, self.patch_applier)
            except Exception as error:
                evaluation_error = error

        if validator_error is not None:
            validation = _with_failure(validation, "validation", str(validator_error))
        if evaluation_error is not None:
            name = "restoration" if _is_restoration_error(evaluation_error) else "patch-apply"
            validation = _with_failure(validation, name, str(evaluation_error))

        restored_to_baseline = (
            baseline is not None and baseline.matches(root, self.patch_applier)
        )

        if not restored_to_baseline and not any(
            check.name == "restoration" for check in validation.checks
        ):
            validation = _with_failure(
                validation,
                "restoration",
                "repository state did not match the recorded baseline after evaluation",
            )
        if unexpected_mutation:
            validation = _with_failure(
                validation,
                "repository-mutation",
                "candidate validation modified repository state outside the proposed patch",
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

    @staticmethod
    def _failure_evidence(
        candidate_id: str,
        name: str,
        reason: str,
        started: float,
    ) -> CandidateEvidence:
        validation = _with_failure(ValidationReport(checks=[]), name, reason)
        return CandidateEvidence(
            candidate_id=candidate_id,
            validation=validation,
            acceptance_criteria_coverage=0.0,
            risk_level=RiskLevel.LOW,
            changed_files=0,
            changed_lines=0,
            duration_seconds=max(0.0, time.monotonic() - started),
            required_failures=[name],
            skipped_checks=[],
            restored_to_baseline=True,
        )


def _with_failure(validation: ValidationReport, name: str, reason: str) -> ValidationReport:
    return ValidationReport(
        checks=[
            *validation.checks,
            CheckResult(
                name=name,
                argv=[],
                status=CheckStatus.FAILED,
                reason=reason,
                required=True,
            ),
        ]
    )


def _is_restoration_error(error: Exception) -> bool:
    return str(error).startswith("patch restoration failed:")


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
