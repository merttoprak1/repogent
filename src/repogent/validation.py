from pathlib import Path

from repogent.domain import CheckResult, CheckStatus, ValidationReport
from repogent.execution import Executor, ValidationPolicy


class ValidationPipeline:
    def __init__(self, executor: Executor, policy: ValidationPolicy | None = None) -> None:
        self.executor = executor
        self.policy = policy or ValidationPolicy()

    def run(self, root: Path) -> ValidationReport:
        checks: list[CheckResult] = []
        for command in self.policy.commands(root):
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
            checks.append(self.executor.run(command, root))
        return ValidationReport(checks=checks)
