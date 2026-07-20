import subprocess
import sys
from pathlib import Path

import pytest

from repogent.domain import CheckStatus
from repogent.execution import (
    CommandPolicyError,
    CommandSpec,
    DockerExecutor,
    LocalExecutor,
    ValidationPolicy,
)


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


@pytest.mark.parametrize("timeout_seconds", [0, -1, 301])
def test_local_executor_rejects_invalid_or_enlarged_timeout(
    tmp_path: Path, timeout_seconds: int
) -> None:
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
        timeout_seconds=timeout_seconds,
    )

    with pytest.raises(CommandPolicyError, match="timeout"):
        LocalExecutor(allowed={"python": command.argv}).run(command, tmp_path)


def test_executors_report_an_unapproved_timeout_as_unavailable(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(
        "repogent.execution.subprocess.run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
    )
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
        timeout_seconds=301,
    )

    assert not LocalExecutor(allowed={"python": command.argv}).available(command)
    assert not DockerExecutor(allowed={"python": command.argv}).available(command)


def test_local_executor_boundedly_collects_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Stream:
        def __init__(self, value: bytes) -> None:
            self.value = value
            self.offset = 0
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            assert size > 0
            self.read_sizes.append(size)
            chunk = self.value[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    class Process:
        def __init__(self) -> None:
            self.stdout = Stream(b"a" * 100_000)
            self.stderr = Stream(b"b" * 100_000)
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            pytest.fail("the successful process must not be killed")

    process = Process()
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> Process:
        popen_calls.append((argv, kwargs))
        return process

    monkeypatch.setattr("repogent.execution.subprocess.Popen", fake_popen)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
    )

    result = LocalExecutor(allowed={"python": command.argv}, max_output_chars=7).run(
        command, tmp_path
    )

    assert result.status is CheckStatus.PASSED
    assert result.stdout == "a" * 7
    assert result.stderr == "b" * 7
    assert all(size > 0 for size in process.stdout.read_sizes + process.stderr.read_sizes)
    assert len(popen_calls) == 1
    argv, kwargs = popen_calls[0]
    assert argv == [sys.executable, "-c", "print('ok')"]
    assert kwargs["cwd"] == tmp_path.resolve()
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["text"] is False
    assert "capture_output" not in kwargs


def test_docker_executor_skips_when_docker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: None)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
    )
    executor = DockerExecutor(allowed={"python": command.argv})

    result = executor.run(command, tmp_path)

    assert not executor.available(command)
    assert result.status is CheckStatus.SKIPPED
    assert result.argv == list(command.argv)
    assert result.reason == "docker executable or validator image unavailable"


def test_docker_executor_skips_missing_image_without_running_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    docker_calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> object:
        docker_calls.append(argv)
        return type("Result", (), {"returncode": 1})()

    monkeypatch.setattr("repogent.execution.subprocess.run", fake_run)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
    )

    result = DockerExecutor(allowed={"python": command.argv}).run(command, tmp_path)

    assert result.status is CheckStatus.SKIPPED
    assert docker_calls == [
        ["/usr/local/bin/docker", "image", "inspect", "repogent-validator:py311"]
    ]


def test_docker_executor_never_pulls_an_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Stream:
        def read(self, size: int = -1) -> bytes:
            assert size > 0
            return b""

    class Process:
        stdout = Stream()
        stderr = Stream()
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            pytest.fail("the successful process must not be killed")

    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(
        "repogent.execution.subprocess.run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
    )
    docker_run_argv: list[str] = []

    def fake_popen(argv: list[str], **_: object) -> Process:
        docker_run_argv.extend(argv)
        return Process()

    monkeypatch.setattr("repogent.execution.subprocess.Popen", fake_popen)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
    )

    result = DockerExecutor(allowed={"python": command.argv}).run(command, tmp_path)

    assert result.status is CheckStatus.PASSED
    assert "--pull=never" in docker_run_argv
