from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from repogent.domain import ExecutionMode, IsolationLevel
from repogent.execution import DockerExecutor, Executor, LocalExecutor, ValidationPolicy
from repogent.mcp_models import ExecutorAvailability, ExecutorOption
from repogent.preflight import Preflight, PreflightReport, ReadinessStatus
from repogent.validation import ValidationPipeline

LOCAL_RISK_STATEMENT = (
    "Local validation executes trusted-repository checks on the host with reduced "
    "isolation and may access resources available to the Repogent process."
)

_DOCKER_REMEDIATION = "Install Docker and ensure docker is on PATH"
_IMAGE_REMEDIATION = "Build the validator image with make validator-image"
_COMMAND_REMEDIATION = "Install the required validation command in the selected executor"


class ExecutorSelectionError(RuntimeError):
    def __init__(
        self, message: str, *, preflight: PreflightReport | None = None
    ) -> None:
        super().__init__(message)
        self.preflight = preflight


@dataclass(frozen=True)
class PreparedExecutor:
    mode: ExecutionMode
    isolation_level: IsolationLevel
    preflight: PreflightReport
    validator: ValidationPipeline


def option_digest(
    run_id: str,
    preview_digest: str,
    mode: ExecutionMode,
    risk_statement: str | None,
) -> str:
    payload = {
        "run_id": run_id,
        "preview_digest": preview_digest,
        "mode": mode.value,
        "risk_statement": risk_statement,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ExecutorRegistry:
    def __init__(
        self,
        *,
        docker_factory: Callable[[], Executor] = DockerExecutor,
        local_factory: Callable[..., Executor] = LocalExecutor,
    ) -> None:
        self._docker_factory = docker_factory
        self._local_factory = local_factory

    def inspect_availability(
        self,
        root: Path,
        policy: ValidationPolicy,
    ) -> list[ExecutorAvailability]:
        return [
            self._availability(root, ExecutionMode.DOCKER, policy),
            self._availability(root, ExecutionMode.LOCAL, policy),
        ]

    def build_options(
        self,
        run_id: str,
        preview_digest: str,
        availability: Sequence[ExecutorAvailability],
    ) -> list[ExecutorOption]:
        return [
            ExecutorOption(
                mode=item.mode,
                available=item.available,
                isolation_level=item.isolation_level,
                option_digest=option_digest(
                    run_id, preview_digest, item.mode, item.risk_statement
                ),
                message=item.message,
                remediation=item.remediation,
                risk_statement=item.risk_statement,
            )
            for item in availability
        ]

    def prepare(
        self,
        root: Path,
        mode: ExecutionMode,
        policy: ValidationPolicy,
    ) -> PreparedExecutor:
        executor, preflight = self._preflight(root, mode, policy)
        if not preflight.passed:
            raise ExecutorSelectionError(
                "selected executor is unavailable", preflight=preflight
            )
        return PreparedExecutor(
            mode=mode,
            isolation_level=_isolation_level(mode),
            preflight=preflight,
            validator=ValidationPipeline(executor, policy),
        )

    def _availability(
        self,
        root: Path,
        mode: ExecutionMode,
        policy: ValidationPolicy,
    ) -> ExecutorAvailability:
        _executor, preflight = self._preflight(root, mode, policy)
        available = preflight.passed
        return ExecutorAvailability(
            mode=mode,
            available=available,
            isolation_level=_isolation_level(mode),
            message=(
                f"{mode.value.capitalize()} validation is available"
                if available
                else f"{mode.value.capitalize()} validation is unavailable"
            ),
            remediation=None if available else _remediation(preflight, mode),
            risk_statement=(LOCAL_RISK_STATEMENT if mode is ExecutionMode.LOCAL else None),
        )

    def _preflight(
        self,
        root: Path,
        mode: ExecutionMode,
        policy: ValidationPolicy,
    ) -> tuple[Executor, PreflightReport]:
        commands = policy.commands(root)
        executor: Executor
        if mode is ExecutionMode.DOCKER:
            executor = self._docker_factory()
        else:
            executor = self._local_factory(
                allowed={command.name: command.argv for command in commands}
            )
        return executor, Preflight(executor, policy).run(root)


def _isolation_level(mode: ExecutionMode) -> IsolationLevel:
    return (
        IsolationLevel.ISOLATED
        if mode is ExecutionMode.DOCKER
        else IsolationLevel.REDUCED_ISOLATION
    )


def _remediation(preflight: PreflightReport, mode: ExecutionMode) -> str:
    executor_check = next(
        (check for check in preflight.checks if check.name == "executor"), None
    )
    if executor_check is not None and executor_check.status is not ReadinessStatus.PASSED:
        if mode is ExecutionMode.DOCKER and "image" in (executor_check.reason or "").lower():
            return _IMAGE_REMEDIATION
        if mode is ExecutionMode.DOCKER:
            return _DOCKER_REMEDIATION
    return _COMMAND_REMEDIATION
