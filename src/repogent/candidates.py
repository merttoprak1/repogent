from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from repogent.domain import (
    CandidateEvidence,
    CandidateRecord,
    CheckResult,
    CheckStatus,
    RiskLevel,
    ValidationReport,
)
from repogent.patching import PatchApplier, PatchPolicy, PatchPolicyError, Snapshot, ValidatedPatch


class Validator(Protocol):
    def run(self, root: Path, *, timeout_seconds: float | None = None) -> ValidationReport: ...


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
        before: dict[Path, _PathFingerprint] = {}
        evaluation_error: Exception | None = None
        validator_error: Exception | None = None

        try:
            before = self._fingerprints(root, validated)
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

        if validator_error is not None:
            validation = _with_failure(validation, "validation", str(validator_error))
        if evaluation_error is not None:
            name = "restoration" if _is_restoration_error(evaluation_error) else "patch-apply"
            validation = _with_failure(validation, name, str(evaluation_error))

        restored_to_baseline = False
        if before:
            try:
                restored_to_baseline = before == self._fingerprints(root, validated)
            except Exception as error:
                validation = _with_failure(validation, "restoration", str(error))
        elif evaluation_error is None:
            # An empty touched-path set cannot arise from PatchPolicy, but keep evidence safe
            # if a future implementation supplies one.
            restored_to_baseline = True

        if not restored_to_baseline and not any(
            check.name == "restoration" for check in validation.checks
        ):
            validation = _with_failure(
                validation,
                "restoration",
                "repository state did not match the recorded baseline after evaluation",
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

    def _fingerprints(
        self, root: Path, validated: ValidatedPatch
    ) -> dict[Path, _PathFingerprint]:
        snapshots, _ = self.patch_applier.snapshot(root, validated)
        return {
            path: _PathFingerprint.from_snapshot(snapshot) for path, snapshot in snapshots.items()
        }

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


class _PathFingerprint(tuple[bool, str | None, int | None]):
    @classmethod
    def from_snapshot(cls, snapshot: Snapshot) -> _PathFingerprint:
        digest = hashlib.sha256(snapshot.content).hexdigest() if snapshot.existed else None
        return cls((snapshot.existed, digest, snapshot.mode))


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
