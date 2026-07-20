from pathlib import Path

from repogent.domain import CheckResult, CheckStatus
from repogent.execution import CommandSpec
from repogent.validation import ValidationPipeline


class StubExecutor:
    def available(self, command: CommandSpec) -> bool:
        return command.name != "ruff"

    def run(self, command: CommandSpec, root: Path) -> CheckResult:
        del root
        return CheckResult(
            name=command.name,
            argv=list(command.argv),
            status=CheckStatus.PASSED,
            exit_code=0,
        )


def test_pipeline_records_missing_optional_check_as_skipped(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    report = ValidationPipeline(StubExecutor()).run(tmp_path)
    ruff = next(check for check in report.checks if check.name == "ruff")
    assert ruff.status is CheckStatus.SKIPPED
    assert ruff.reason == "optional tool unavailable"
    assert report.passed is True
