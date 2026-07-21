import subprocess
from pathlib import Path

import pytest

from repogent import execution as execution_module
from repogent.domain import CheckResult
from repogent.execution import CommandSpec, DockerExecutor, ValidationPolicy
from repogent.preflight import (
    Preflight,
    ReadinessStatus,
    configuration_fingerprint,
)


class FakeExecutor:
    def __init__(self, *, ready: bool, unavailable: set[str] | None = None) -> None:
        self.ready = ready
        self.unavailable = unavailable or set()

    def readiness(self) -> tuple[bool, str | None]:
        return (self.ready, None if self.ready else "validator image unavailable")

    def available(self, command: CommandSpec) -> bool:
        return command.name not in self.unavailable

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


def test_preflight_blocks_when_required_validation_command_is_unavailable(tmp_path: Path) -> None:
    repository = initialize_git_repository(tmp_path)
    nested_test = repository / "quality" / "regression" / "test_value.py"
    nested_test.parent.mkdir(parents=True)
    nested_test.write_text("def test_value(): pass\n")

    report = Preflight(
        FakeExecutor(ready=True, unavailable={"pytest"}), ValidationPolicy()
    ).run(repository)

    pytest_check = next(check for check in report.checks if check.name == "command:pytest")
    assert report.passed is False
    assert pytest_check.required is True
    assert pytest_check.status is ReadinessStatus.FAILED
    assert pytest_check.reason == "required validation command unavailable"


def test_preflight_warns_when_optional_validation_command_is_unavailable(tmp_path: Path) -> None:
    repository = initialize_git_repository(tmp_path)

    report = Preflight(
        FakeExecutor(ready=True, unavailable={"ruff"}), ValidationPolicy()
    ).run(repository)

    ruff_check = next(check for check in report.checks if check.name == "command:ruff")
    assert report.passed is True
    assert ruff_check.required is False
    assert ruff_check.status is ReadinessStatus.WARNING
    assert ruff_check.reason == "optional validation command unavailable"


def test_docker_preflight_warns_when_optional_module_is_missing_inside_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = initialize_git_repository(tmp_path)
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")

    def fake_bounded_run(argv: list[str], **_kwargs: object) -> object:
        if argv[1:3] == ["image", "inspect"]:
            return execution_module._ProcessResult(0, "", "", False)
        module = argv[-1]
        return execution_module._ProcessResult(
            1 if module == "ruff" else 0, "", "", False
        )

    monkeypatch.setattr("repogent.execution._run_with_bounded_output", fake_bounded_run)

    report = Preflight(DockerExecutor(), ValidationPolicy()).run(repository)

    ruff_check = next(check for check in report.checks if check.name == "command:ruff")
    assert report.passed is True
    assert ruff_check.status is ReadinessStatus.WARNING
    assert ruff_check.reason == "optional validation command unavailable"


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
