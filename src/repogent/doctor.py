from __future__ import annotations

import platform
from pathlib import Path

from repogent.codex_cli import CodexCliProvider
from repogent.execution import DockerExecutor, LocalExecutor, ValidationPolicy
from repogent.executor_selection import ExecutorRegistry
from repogent.mcp_models import (
    DoctorCheck,
    DoctorReport,
    DoctorRequest,
    ExecutorAvailability,
)
from repogent.preflight import (
    Preflight,
    PreflightCheck,
    ReadinessStatus,
    repository_preflight,
)

_PYTHON_REMEDIATION = "Install and run Repogent with Python 3.11 or newer"
_DOCKER_REMEDIATION = "Install Docker and ensure docker is on PATH"
_IMAGE_REMEDIATION = "Build the validator image with make validator-image"
_COMMAND_REMEDIATION = "Install the required validation command in the selected executor"
_CODEX_INSTALL_REMEDIATION = "Install the Codex CLI and ensure codex is on PATH"
_CODEX_LOGIN_REMEDIATION = "Run codex login in your terminal"
_CODEX_REPAIR_REMEDIATION = "Inspect or reinstall the Codex CLI"


class DoctorService:
    """Read-only readiness checks for a future Repogent run."""

    def run(self, request: DoctorRequest) -> DoctorReport:
        checks: list[DoctorCheck] = []
        repository = self._repository_check(request.repository, checks)
        if repository is None:
            return self._report(request, checks)

        if tuple(map(int, platform.python_version_tuple()[:2])) < (3, 11):
            checks.append(
                DoctorCheck(
                    name="python",
                    passed=False,
                    required=True,
                    message="Python 3.11 or newer is required",
                    remediation=_PYTHON_REMEDIATION,
                )
            )
            return self._report(request, checks, repository)
        checks.append(
            DoctorCheck(
                name="python",
                passed=True,
                required=True,
                message="Python version is supported",
            )
        )

        policy = ValidationPolicy()
        if request.executor == "deferred":
            base_preflight = repository_preflight(repository, policy)
            checks.extend(self._preflight_checks(base_preflight.checks))
            if request.provider == "codex-cli":
                readiness = CodexCliProvider(
                    model=request.model, target_root=repository
                ).check_ready()
                checks.append(self._provider_check(readiness.ready, readiness.reason))
            return self._report(
                request,
                checks,
                repository,
                executors=ExecutorRegistry().inspect_availability(repository, policy),
            )

        commands = policy.commands(repository)
        executor = (
            DockerExecutor()
            if request.executor == "docker"
            else LocalExecutor(allowed={command.name: command.argv for command in commands})
        )
        preflight = Preflight(executor, policy).run(repository)
        checks.extend(self._preflight_checks(preflight.checks))
        if not preflight.passed:
            return self._report(request, checks, repository)

        if request.provider == "codex-cli":
            readiness = CodexCliProvider(
                model=request.model, target_root=repository
            ).check_ready()
            checks.append(self._provider_check(readiness.ready, readiness.reason))
        return self._report(request, checks, repository)

    @staticmethod
    def _repository_check(repository: Path, checks: list[DoctorCheck]) -> Path | None:
        try:
            resolved = repository.resolve(strict=True)
        except (OSError, RuntimeError):
            checks.append(
                DoctorCheck(
                    name="repository",
                    passed=False,
                    required=True,
                    message="repository is not accessible",
                    remediation="Choose an accessible repository directory",
                )
            )
            return None
        if resolved.parent == resolved:
            checks.append(
                DoctorCheck(
                    name="repository",
                    passed=False,
                    required=True,
                    message="filesystem root repositories are unsupported",
                    remediation="Choose a repository directory below the filesystem root",
                )
            )
            return None
        if not resolved.is_dir():
            checks.append(
                DoctorCheck(
                    name="repository",
                    passed=False,
                    required=True,
                    message="repository must be a directory",
                    remediation="Choose an accessible repository directory",
                )
            )
            return None
        checks.append(
            DoctorCheck(
                name="repository",
                passed=True,
                required=True,
                message="repository is accessible",
            )
        )
        return resolved

    @staticmethod
    def _preflight_checks(preflight_checks: list[PreflightCheck]) -> list[DoctorCheck]:
        checks: list[DoctorCheck] = []
        for check in preflight_checks:
            passed = check.status is ReadinessStatus.PASSED
            remediation = None if passed or not check.required else _remediation_for(check)
            checks.append(
                DoctorCheck(
                    name=check.name,
                    passed=passed,
                    required=check.required,
                    message=_message_for(check),
                    remediation=remediation,
                )
            )
        return checks

    @staticmethod
    def _provider_check(ready: bool, reason: str | None) -> DoctorCheck:
        if ready:
            return DoctorCheck(
                name="provider", passed=True, required=True, message="Codex CLI is ready"
            )
        if reason is not None and "executable not found" in reason.lower():
            message, remediation = "Codex CLI executable not found", _CODEX_INSTALL_REMEDIATION
        elif reason is not None and "not authenticated" in reason.lower():
            message, remediation = "Codex CLI is not authenticated", _CODEX_LOGIN_REMEDIATION
        else:
            message, remediation = "Codex CLI is not ready", _CODEX_REPAIR_REMEDIATION
        return DoctorCheck(
            name="provider",
            passed=False,
            required=True,
            message=message,
            remediation=remediation,
        )

    @staticmethod
    def _report(
        request: DoctorRequest,
        checks: list[DoctorCheck],
        repository: Path | None = None,
        *,
        executors: list[ExecutorAvailability] | None = None,
    ) -> DoctorReport:
        return DoctorReport(
            ready=all(check.passed or not check.required for check in checks),
            repository=str(repository if repository is not None else request.repository),
            provider=request.provider,
            executor=request.executor,
            checks=checks,
            executors=executors or [],
        )


def _message_for(check: PreflightCheck) -> str:
    if check.name == "executor":
        return (
            "selected executor is ready"
            if check.status is ReadinessStatus.PASSED
            else "selected executor is unavailable"
        )
    if check.name.startswith("command:"):
        if check.status is ReadinessStatus.PASSED:
            return "validation command is available"
        return (
            "required validation command unavailable"
            if check.required
            else "optional validation command unavailable"
        )
    if check.name == "git":
        return (
            "git metadata is available"
            if check.status is ReadinessStatus.PASSED
            else "git metadata is unavailable"
        )
    return (
        "preflight check passed"
        if check.status is ReadinessStatus.PASSED
        else "preflight check failed"
    )


def _remediation_for(check: PreflightCheck) -> str:
    reason = check.reason or ""
    if check.name == "executor":
        if "image" in reason.lower():
            return _IMAGE_REMEDIATION
        return _DOCKER_REMEDIATION
    return _COMMAND_REMEDIATION
