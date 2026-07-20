import math
import time
from dataclasses import replace
from pathlib import Path

from repogent.domain import CheckResult, CheckStatus, ValidationReport
from repogent.execution import CommandSpec, Executor, ValidationPolicy


class ValidationPipeline:
    def __init__(self, executor: Executor, policy: ValidationPolicy | None = None) -> None:
        self.executor = executor
        self.policy = policy or ValidationPolicy()

    def run(self, root: Path, *, timeout_seconds: float | None = None) -> ValidationReport:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise TimeoutError("validation timeout exceeded")
        deadline = (
            time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        )
        checks: list[CheckResult] = []
        for command in self.policy.commands(root):
            if deadline is not None:
                command = _cap_command_timeout(command, deadline)
            if not self.executor.available(command):
                status = CheckStatus.FAILED if command.required else CheckStatus.SKIPPED
                reason = (
                    "required tool unavailable" if command.required else "optional tool unavailable"
                )
                checks.append(
                    CheckResult(
                        name=command.name,
                        argv=list(command.argv),
                        status=status,
                        reason=reason,
                    )
                )
                continue
            if deadline is not None:
                command = _cap_command_timeout(command, deadline)
            checks.append(self.executor.run(command, root))
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("validation timeout exceeded")
        return ValidationReport(checks=checks)


def _cap_command_timeout(command: CommandSpec, deadline: float) -> CommandSpec:
    remaining_seconds = math.floor(deadline - time.monotonic())
    if remaining_seconds < 1:
        raise TimeoutError("validation timeout exceeded")
    return replace(
        command, timeout_seconds=min(command.timeout_seconds, remaining_seconds)
    )
