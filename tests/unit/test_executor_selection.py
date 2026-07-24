from __future__ import annotations

from pathlib import Path

import pytest

from repogent.domain import ExecutionMode
from repogent.execution import CommandSpec, ValidationPolicy
from repogent.executor_selection import ExecutorRegistry, ExecutorSelectionError
from repogent.mcp_models import ExecutorAvailability


class ReadyDocker:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def readiness(self) -> tuple[bool, str | None]:
        return self.ready, None if self.ready else "docker executable is unavailable"

    def available(self, _command: CommandSpec) -> bool:
        return self.ready

    def run(self, _command: CommandSpec, _root: Path) -> object:
        raise AssertionError("availability inspection must not run target-repository commands")


class RequiredMissingPytestPolicy(ValidationPolicy):
    def commands(self, _root: Path) -> list[CommandSpec]:
        return [
            CommandSpec(
                "pytest",
                ("python", "-m", "pytest", "-q"),
                True,
                module="repogent_test_missing_pytest_module",
            )
        ]


def make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    return repository


def local_availability(options: list[ExecutorAvailability]) -> ExecutorAvailability:
    return next(option for option in options if option.mode is ExecutionMode.LOCAL)


def test_prepare_rechecks_availability_after_inspect(tmp_path: Path) -> None:
    root = make_repository(tmp_path)
    policy = ValidationPolicy()
    attempts = iter([True, False])

    def flapping_docker_factory() -> ReadyDocker:
        return ReadyDocker(next(attempts))

    registry = ExecutorRegistry(docker_factory=flapping_docker_factory)

    assert registry.inspect_availability(root, policy)[0].available is True
    with pytest.raises(ExecutorSelectionError, match="selected executor is unavailable"):
        registry.prepare(root, ExecutionMode.DOCKER, policy)


def test_local_option_is_unavailable_when_required_pytest_module_is_missing(
    tmp_path: Path,
) -> None:
    root = make_repository(tmp_path)
    policy = RequiredMissingPytestPolicy()
    registry = ExecutorRegistry()

    option = local_availability(registry.inspect_availability(root, policy))

    assert option.available is False
    assert option.remediation == "Install the required validation command in the selected executor"
