from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from repogent.domain import CheckResult, CheckStatus


class CommandPolicyError(ValueError):
    pass


def _truncated_output(value: str | bytes | None, max_output_chars: int) -> str:
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return (value or "")[-max_output_chars:]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    required: bool
    timeout_seconds: int = 300
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


class LocalExecutor:
    def __init__(
        self,
        *,
        allowed: dict[str, tuple[str, ...]] | None = None,
        max_output_chars: int = 100_000,
    ) -> None:
        defaults = {
            command.name: command.argv for command in ValidationPolicy().commands(Path.cwd())
        }
        self.allowed = allowed or defaults
        self.max_output_chars = max_output_chars

    def available(self, command: CommandSpec) -> bool:
        return command.module is None or importlib.util.find_spec(command.module) is not None

    def run(self, command: CommandSpec, root: Path) -> CheckResult:
        if self.allowed.get(command.name) != command.argv:
            raise CommandPolicyError(f"command is not allowlisted: {command.name}")
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
        try:
            result = subprocess.run(  # noqa: S603  # nosec B603
                argv,
                cwd=repository,
                env=environment,
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return CheckResult(
                name=command.name,
                argv=argv,
                status=CheckStatus.TIMED_OUT,
                stdout=_truncated_output(error.stdout, self.max_output_chars),
                stderr=_truncated_output(error.stderr, self.max_output_chars),
                duration_seconds=time.monotonic() - started,
                reason="command timed out",
            )
        return CheckResult(
            name=command.name,
            argv=argv,
            status=CheckStatus.PASSED if result.returncode == 0 else CheckStatus.FAILED,
            exit_code=result.returncode,
            stdout=result.stdout[-self.max_output_chars :],
            stderr=result.stderr[-self.max_output_chars :],
            duration_seconds=time.monotonic() - started,
        )


class DockerExecutor:
    def __init__(
        self,
        *,
        image: str = "repogent-validator:py311",
        allowed: dict[str, tuple[str, ...]] | None = None,
        max_output_chars: int = 100_000,
    ) -> None:
        self.image = image
        self.docker = shutil.which("docker")
        defaults = {
            command.name: command.argv for command in ValidationPolicy().commands(Path.cwd())
        }
        self.allowed = allowed or defaults
        self.max_output_chars = max_output_chars

    def available(self, command: CommandSpec) -> bool:
        if self.allowed.get(command.name) != command.argv:
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
        if self.allowed.get(command.name) != command.argv:
            raise CommandPolicyError(f"command is not allowlisted: {command.name}")
        if self.docker is None:
            raise OSError("docker executable unavailable")
        repository = root.resolve(strict=True)
        tmpfs = f"{Path(os.sep, 'tmp')}:rw,noexec,nosuid,size=256m"
        argv = [
            self.docker,
            "run",
            "--rm",
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
        try:
            result = subprocess.run(  # noqa: S603  # nosec B603
                argv,
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return CheckResult(
                name=command.name,
                argv=list(command.argv),
                status=CheckStatus.TIMED_OUT,
                stdout=_truncated_output(error.stdout, self.max_output_chars),
                stderr=_truncated_output(error.stderr, self.max_output_chars),
                duration_seconds=time.monotonic() - started,
                reason="container timed out",
            )
        return CheckResult(
            name=command.name,
            argv=list(command.argv),
            status=CheckStatus.PASSED if result.returncode == 0 else CheckStatus.FAILED,
            exit_code=result.returncode,
            stdout=result.stdout[-self.max_output_chars :],
            stderr=result.stderr[-self.max_output_chars :],
            duration_seconds=time.monotonic() - started,
        )
