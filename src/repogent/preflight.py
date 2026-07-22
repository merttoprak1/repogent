from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from pydantic import computed_field

from repogent.domain import VersionedModel
from repogent.execution import CommandSpec, Executor, ValidationPolicy

_GIT_TIMEOUT_SECONDS = 5


class ReadinessStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"


class PreflightCheck(VersionedModel):
    name: str
    status: ReadinessStatus
    required: bool
    reason: str | None = None


class PreflightReport(VersionedModel):
    checks: list[PreflightCheck]
    git_commit: str | None
    dirty: bool
    repository_fingerprint: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return all(
            not check.required or check.status is ReadinessStatus.PASSED for check in self.checks
        )


class Preflight:
    def __init__(self, executor: Executor, policy: ValidationPolicy) -> None:
        self.executor = executor
        self.policy = policy

    def run(self, root: Path) -> PreflightReport:
        repository = root.resolve(strict=True)
        if repository.parent == repository:
            raise ValueError("filesystem root repositories are unsupported")
        base = repository_preflight(repository, self.policy)
        ready, reason = self.executor.readiness()
        commands = self.policy.commands(repository)
        executor_check = PreflightCheck(
            name="executor",
            status=ReadinessStatus.PASSED if ready else ReadinessStatus.FAILED,
            required=True,
            reason=reason,
        )
        checks = [*base.checks, executor_check]
        if ready:
            for command in commands:
                available = self.executor.available(command)
                checks.append(
                    PreflightCheck(
                        name=f"command:{command.name}",
                        status=(
                            ReadinessStatus.PASSED
                            if available
                            else ReadinessStatus.FAILED
                            if command.required
                            else ReadinessStatus.WARNING
                        ),
                        required=command.required,
                        reason=(
                            None
                            if available
                            else "required validation command unavailable"
                            if command.required
                            else "optional validation command unavailable"
                        ),
                    )
                )
        return PreflightReport(
            checks=checks,
            git_commit=base.git_commit,
            dirty=base.dirty,
            repository_fingerprint=base.repository_fingerprint,
        )


def repository_preflight(root: Path, policy: ValidationPolicy) -> PreflightReport:
    repository = root.resolve(strict=True)
    if not repository.is_dir() or repository.parent == repository:
        raise ValueError("repository must be a supported directory")
    git_commit, dirty_output, git_check = _git_state(repository)
    commands = policy.commands(repository)
    return PreflightReport(
        checks=[git_check],
        git_commit=git_commit,
        dirty=bool(dirty_output),
        repository_fingerprint=_sha256(
            {
                "root": str(repository),
                "commit": git_commit or "no-commit",
                "dirty": dirty_output,
                "commands": sorted(command.name for command in commands),
            }
        ),
    )


def configuration_fingerprint(
    provider: str,
    model: str,
    executor: str,
    commands: Sequence[CommandSpec],
) -> str:
    ordered_commands = sorted(
        commands,
        key=lambda command: (
            command.name,
            command.argv,
            command.required,
            command.timeout_seconds,
            command.module or "",
        ),
    )
    return _sha256(
        {
            "provider": provider,
            "model": model,
            "executor": executor,
            "commands": [
                {
                    "name": command.name,
                    "argv": command.argv,
                    "required": command.required,
                    "timeout_seconds": command.timeout_seconds,
                    "module": command.module,
                }
                for command in ordered_commands
            ],
        }
    )


def _git_state(root: Path) -> tuple[str | None, str, PreflightCheck]:
    try:
        commit = _run_git(root, ("git", "rev-parse", "HEAD"))
        dirty = _run_git(root, ("git", "status", "--porcelain"))
    except (OSError, subprocess.TimeoutExpired) as error:
        return (
            None,
            "",
            PreflightCheck(
                name="git",
                status=ReadinessStatus.WARNING,
                required=False,
                reason=f"git metadata unavailable: {error}",
            ),
        )
    if commit.returncode != 0 or dirty.returncode != 0:
        return (
            None,
            "",
            PreflightCheck(
                name="git",
                status=ReadinessStatus.WARNING,
                required=False,
                reason="git commit metadata unavailable",
            ),
        )
    return (
        commit.stdout.strip() or None,
        dirty.stdout,
        PreflightCheck(name="git", status=ReadinessStatus.PASSED, required=False),
    )


def _run_git(root: Path, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603
        argv,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _sha256(payload: object) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()
