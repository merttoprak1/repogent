from pathlib import Path

import pytest

from repogent.domain import CheckResult, CheckStatus
from repogent.execution import CommandSpec
from repogent.validation import ValidationPipeline


class StubExecutor:
    def __init__(self) -> None:
        self.timeouts: list[int] = []

    def available(self, command: CommandSpec) -> bool:
        self.timeouts.append(command.timeout_seconds)
        return command.name != "ruff"

    def run(self, command: CommandSpec, root: Path) -> CheckResult:
        del root
        return CheckResult(
            name=command.name,
            argv=list(command.argv),
            status=CheckStatus.PASSED,
            exit_code=0,
        )


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_pipeline_records_missing_optional_check_as_skipped(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    report = ValidationPipeline(StubExecutor()).run(tmp_path)
    ruff = next(check for check in report.checks if check.name == "ruff")
    assert ruff.status is CheckStatus.SKIPPED
    assert ruff.reason == "optional tool unavailable"
    assert report.passed is True


def test_pipeline_caps_each_command_by_remaining_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    executor = StubExecutor()
    monkeypatch.setattr("repogent.validation.time.monotonic", lambda: 10.0)

    ValidationPipeline(executor).run(tmp_path, timeout_seconds=7)

    assert executor.timeouts == [7, 7, 7, 7]


def test_pipeline_recaps_command_after_availability_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    run_timeouts: list[int] = []

    class SlowAvailabilityExecutor:
        def available(self, command: CommandSpec) -> bool:
            clock.advance(4)
            return True

        def run(self, command: CommandSpec, root: Path) -> CheckResult:
            del root
            run_timeouts.append(command.timeout_seconds)
            return CheckResult(
                name=command.name,
                argv=list(command.argv),
                status=CheckStatus.PASSED,
                exit_code=0,
            )

    class OneCommandPolicy:
        def commands(self, root: Path) -> list[CommandSpec]:
            del root
            return [CommandSpec("pytest", ("python", "-m", "pytest"), True)]

    monkeypatch.setattr("repogent.validation.time.monotonic", clock.monotonic)
    pipeline = ValidationPipeline(SlowAvailabilityExecutor(), policy=OneCommandPolicy())  # type: ignore[arg-type]

    pipeline.run(tmp_path, timeout_seconds=10)

    assert run_timeouts == [6]
