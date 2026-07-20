import sys
from pathlib import Path

import pytest

from repogent.domain import CheckStatus
from repogent.execution import CommandPolicyError, CommandSpec, LocalExecutor, ValidationPolicy


def test_policy_returns_only_fixed_module_commands(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    commands = ValidationPolicy().commands(tmp_path)
    assert [command.name for command in commands] == ["pytest", "ruff", "mypy", "bandit"]
    assert commands[0].argv == ("python", "-m", "pytest", "-q")
    assert all(
        not any(token in {"sh", "bash", "-c"} for token in command.argv) for command in commands
    )


def test_local_executor_runs_allowlisted_command_without_shell(tmp_path: Path) -> None:
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
        timeout_seconds=10,
    )
    executor = LocalExecutor(allowed={"python": command.argv})
    result = executor.run(command, tmp_path)
    assert result.status is CheckStatus.PASSED
    assert result.stdout.strip() == "ok"
    assert result.argv[0] == sys.executable


def test_local_executor_rejects_changed_argv(tmp_path: Path) -> None:
    command = CommandSpec(name="pytest", argv=("python", "-m", "pytest", "--pwn"), required=True)
    with pytest.raises(CommandPolicyError):
        LocalExecutor(allowed={"pytest": ("python", "-m", "pytest", "-q")}).run(command, tmp_path)


def test_local_executor_reports_missing_module_as_unavailable() -> None:
    command = CommandSpec(
        name="optional",
        argv=("python", "-m", "optional"),
        required=False,
        module="repogent_module_that_does_not_exist",
    )
    assert not LocalExecutor(allowed={"optional": command.argv}).available(command)


def test_local_executor_returns_timeout_result(tmp_path: Path) -> None:
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "import time; print('before'); time.sleep(2)"),
        required=True,
        timeout_seconds=1,
    )
    result = LocalExecutor(allowed={"python": command.argv}).run(command, tmp_path)
    assert result.status is CheckStatus.TIMED_OUT
    assert result.stdout.strip() == "before"
    assert result.reason == "command timed out"
