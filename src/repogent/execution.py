from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess  # nosec B404
import sys
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from typing import BinaryIO, Protocol

from repogent.domain import CheckResult, CheckStatus


class CommandPolicyError(ValueError):
    pass


DEFAULT_TIMEOUT_SECONDS = 300
_OUTPUT_READ_BYTES = 8_192


def _bounded_output(stream: BinaryIO, max_output_chars: int) -> str:
    chunks: deque[bytes] = deque()
    retained = 0
    while chunk := stream.read(_OUTPUT_READ_BYTES):
        if len(chunk) > max_output_chars:
            chunk = chunk[-max_output_chars:]
        chunks.append(chunk)
        retained += len(chunk)
        while retained > max_output_chars:
            discarded = chunks.popleft()
            excess = retained - max_output_chars
            if len(discarded) > excess:
                chunks.appendleft(discarded[excess:])
                retained -= excess
            else:
                retained -= len(discarded)
    return b"".join(chunks).decode(errors="replace")


@dataclass(frozen=True)
class _ProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


def _run_with_bounded_output(
    argv: Sequence[str],
    *,
    timeout_seconds: int,
    max_output_chars: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> _ProcessResult:
    process = subprocess.Popen(  # noqa: S603  # nosec B603
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("subprocess output pipes are unavailable")
    stdout = process.stdout
    stderr = process.stderr
    output: dict[str, str] = {}

    def collect_output(name: str, stream: BinaryIO) -> None:
        output[name] = _bounded_output(stream, max_output_chars)

    stdout_reader = Thread(target=collect_output, args=("stdout", stdout))
    stderr_reader = Thread(target=collect_output, args=("stderr", stderr))
    stdout_reader.start()
    stderr_reader.start()
    try:
        exit_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        timed_out = True
        exit_code = None
    else:
        timed_out = False
    stdout_reader.join()
    stderr_reader.join()
    return _ProcessResult(
        exit_code=exit_code,
        stdout=output["stdout"],
        stderr=output["stderr"],
        timed_out=timed_out,
    )


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    required: bool
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    module: str | None = None


class ValidationPolicy:
    def commands(self, root: Path) -> list[CommandSpec]:
        pytest_required = (root / "tests").is_dir() or any(root.glob("test_*.py"))
        return [
            CommandSpec(
                "pytest", ("python", "-m", "pytest", "-q"), pytest_required, module="pytest"
            ),
            CommandSpec("ruff", ("python", "-m", "ruff", "check", "."), False, module="ruff"),
            CommandSpec("mypy", ("python", "-m", "mypy", "."), False, module="mypy"),
            CommandSpec(
                "bandit", ("python", "-m", "bandit", "-q", "-r", "."), False, module="bandit"
            ),
        ]


class Executor(Protocol):
    def available(self, command: CommandSpec) -> bool: ...
    def run(self, command: CommandSpec, root: Path) -> CheckResult: ...


class _RestrictedExecutor:
    def __init__(
        self,
        *,
        allowed: dict[str, tuple[str, ...]] | None,
        timeout_limits: dict[str, int] | None,
        max_output_chars: int,
    ) -> None:
        defaults = ValidationPolicy().commands(Path.cwd())
        self.allowed = allowed or {command.name: command.argv for command in defaults}
        default_timeout_limits = {command.name: command.timeout_seconds for command in defaults}
        self.timeout_limits = timeout_limits or {
            name: default_timeout_limits.get(name, DEFAULT_TIMEOUT_SECONDS) for name in self.allowed
        }
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        self.max_output_chars = max_output_chars

    def _validate_command(self, command: CommandSpec) -> None:
        if self.allowed.get(command.name) != command.argv:
            raise CommandPolicyError(f"command is not allowlisted: {command.name}")
        if not self._has_approved_timeout(command):
            raise CommandPolicyError(f"command timeout is not approved: {command.name}")

    def _has_approved_timeout(self, command: CommandSpec) -> bool:
        timeout_limit = self.timeout_limits.get(command.name)
        return not (
            type(command.timeout_seconds) is not int
            or command.timeout_seconds <= 0
            or timeout_limit is None
            or command.timeout_seconds > timeout_limit
        )

    def _is_allowed(self, command: CommandSpec) -> bool:
        return (
            self.allowed.get(command.name) == command.argv
            and self._has_approved_timeout(command)
        )


class LocalExecutor(_RestrictedExecutor):
    def __init__(
        self,
        *,
        allowed: dict[str, tuple[str, ...]] | None = None,
        timeout_limits: dict[str, int] | None = None,
        max_output_chars: int = 100_000,
    ) -> None:
        super().__init__(
            allowed=allowed,
            timeout_limits=timeout_limits,
            max_output_chars=max_output_chars,
        )

    def available(self, command: CommandSpec) -> bool:
        return self._is_allowed(command) and (
            command.module is None or importlib.util.find_spec(command.module) is not None
        )

    def run(self, command: CommandSpec, root: Path) -> CheckResult:
        self._validate_command(command)
        repository = root.resolve(strict=True)
        argv = [
            sys.executable if argument == "python" and index == 0 else argument
            for index, argument in enumerate(command.argv)
        ]
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(repository),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        started = time.monotonic()
        result = _run_with_bounded_output(
            argv,
            cwd=repository,
            env=environment,
            timeout_seconds=command.timeout_seconds,
            max_output_chars=self.max_output_chars,
        )
        if result.timed_out:
            return CheckResult(
                name=command.name,
                argv=argv,
                status=CheckStatus.TIMED_OUT,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=time.monotonic() - started,
                reason="command timed out",
            )
        return CheckResult(
            name=command.name,
            argv=argv,
            status=CheckStatus.PASSED if result.exit_code == 0 else CheckStatus.FAILED,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=time.monotonic() - started,
        )


class DockerExecutor(_RestrictedExecutor):
    def __init__(
        self,
        *,
        image: str = "repogent-validator:py311",
        allowed: dict[str, tuple[str, ...]] | None = None,
        timeout_limits: dict[str, int] | None = None,
        max_output_chars: int = 100_000,
    ) -> None:
        self.image = image
        self.docker = shutil.which("docker")
        super().__init__(
            allowed=allowed,
            timeout_limits=timeout_limits,
            max_output_chars=max_output_chars,
        )

    def available(self, command: CommandSpec) -> bool:
        if not self._is_allowed(command):
            return False
        if self.docker is None:
            return False
        try:
            result = subprocess.run(  # noqa: S603  # nosec B603
                [self.docker, "image", "inspect", self.image],
                capture_output=True,
                check=False,
            )
        except OSError:
            return False
        return result.returncode == 0

    def run(self, command: CommandSpec, root: Path) -> CheckResult:
        self._validate_command(command)
        docker = self.docker
        if docker is None or not self.available(command):
            return CheckResult(
                name=command.name,
                argv=list(command.argv),
                status=CheckStatus.SKIPPED,
                reason="docker executable or validator image unavailable",
            )
        repository = root.resolve(strict=True)
        tmpfs = f"{Path(os.sep, 'tmp')}:rw,noexec,nosuid,size=256m"
        argv = [
            docker,
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--read-only",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--pids-limit",
            "256",
            "--tmpfs",
            tmpfs,
            "--mount",
            f"type=bind,src={repository},dst=/workspace,ro",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTEST_ADDOPTS=-p no:cacheprovider",
            "--workdir",
            "/workspace",
            self.image,
            *command.argv,
        ]
        started = time.monotonic()
        result = _run_with_bounded_output(
            argv,
            timeout_seconds=command.timeout_seconds,
            max_output_chars=self.max_output_chars,
        )
        if result.timed_out:
            return CheckResult(
                name=command.name,
                argv=list(command.argv),
                status=CheckStatus.TIMED_OUT,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=time.monotonic() - started,
                reason="container timed out",
            )
        return CheckResult(
            name=command.name,
            argv=list(command.argv),
            status=CheckStatus.PASSED if result.exit_code == 0 else CheckStatus.FAILED,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=time.monotonic() - started,
        )
