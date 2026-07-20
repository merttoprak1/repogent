import subprocess
from pathlib import Path

from repogent.domain import CheckResult
from repogent.execution import CommandSpec, ValidationPolicy
from repogent.preflight import (
    Preflight,
    ReadinessStatus,
    configuration_fingerprint,
)


class FakeExecutor:
    def __init__(self, *, ready: bool) -> None:
        self.ready = ready

    def readiness(self) -> tuple[bool, str | None]:
        return (self.ready, None if self.ready else "validator image unavailable")

    def available(self, command: CommandSpec) -> bool:
        del command
        return True

    def run(self, command: CommandSpec, root: Path) -> CheckResult:
        raise AssertionError(f"preflight must not run {command.name} in {root}")


def initialize_git_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    (repository / "tracked.py").write_text("value = 1\n")
    commands = [
        ("git", "init", "-q"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test User"),
        ("git", "add", "tracked.py"),
        ("git", "commit", "-qm", "initial"),
    ]
    for command in commands:
        subprocess.run(command, cwd=repository, check=True)  # noqa: S603,S607
    return repository


def test_preflight_reports_commit_dirty_state_and_executor(tmp_path: Path) -> None:
    repository = initialize_git_repository(tmp_path)
    (repository / "tracked.py").write_text("value = 2\n")

    report = Preflight(FakeExecutor(ready=True), ValidationPolicy()).run(repository)

    assert report.passed is True
    assert report.git_commit is not None
    assert report.dirty is True
    assert report.repository_fingerprint
    assert report.checks[-1].name == "executor"
    assert report.checks[-1].status is ReadinessStatus.PASSED


def test_preflight_blocks_unavailable_docker_before_provider_creation(tmp_path: Path) -> None:
    repository = initialize_git_repository(tmp_path)

    report = Preflight(FakeExecutor(ready=False), ValidationPolicy()).run(repository)

    assert report.passed is False
    assert report.checks[-1].name == "executor"
    assert report.checks[-1].required is True
    assert report.checks[-1].reason == "validator image unavailable"


def test_preflight_treats_non_git_directories_as_a_warning(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    report = Preflight(FakeExecutor(ready=True), ValidationPolicy()).run(repository)

    assert report.passed is True
    assert report.git_commit is None
    assert report.checks[0].status is ReadinessStatus.WARNING


def test_configuration_fingerprint_is_canonical_and_order_independent() -> None:
    pytest_command = CommandSpec("pytest", ("python", "-m", "pytest", "-q"), True)
    ruff_command = CommandSpec("ruff", ("python", "-m", "ruff", "check", "."), False)

    first = configuration_fingerprint("openai", "model", "docker", [ruff_command, pytest_command])
    second = configuration_fingerprint("openai", "model", "docker", [pytest_command, ruff_command])

    assert first == second
    assert len(first) == 64
