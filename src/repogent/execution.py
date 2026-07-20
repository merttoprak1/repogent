from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import signal
import subprocess  # nosec B404
import sys
import time
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Thread
from typing import BinaryIO, Protocol

from repogent.domain import CheckResult, CheckStatus


class CommandPolicyError(ValueError):
    pass


DEFAULT_TIMEOUT_SECONDS = 300
_OUTPUT_READ_BYTES = 8_192
_OUTPUT_DRAIN_TIMEOUT_SECONDS = 0.5
_PROCESS_TERMINATION_TIMEOUT_SECONDS = 1
_DOCKER_CONTROL_TIMEOUT_SECONDS = 5


class _BoundedOutput:
    def __init__(self, max_output_chars: int) -> None:
        self._max_output_chars = max_output_chars
        self._chunks: deque[bytes] = deque()
        self._retained = 0
        self._lock = Lock()

    def collect(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(_OUTPUT_READ_BYTES):
                self._append(chunk)
        except (OSError, ValueError):
            # A bounded caller may return while a descendant still holds the pipe.
            # If another owner closes the stream, retain everything read so far.
            return

    def _append(self, chunk: bytes) -> None:
        if len(chunk) > self._max_output_chars:
            chunk = chunk[-self._max_output_chars :]
        with self._lock:
            self._chunks.append(chunk)
            self._retained += len(chunk)
            while self._retained > self._max_output_chars:
                discarded = self._chunks.popleft()
                excess = self._retained - self._max_output_chars
                if len(discarded) > excess:
                    self._chunks.appendleft(discarded[excess:])
                    self._retained -= excess
                else:
                    self._retained -= len(discarded)

    def text(self) -> str:
        with self._lock:
            return b"".join(self._chunks).decode(errors="replace")


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
    terminate_process_group: bool = False,
) -> _ProcessResult:
    use_process_group = terminate_process_group and os.name == "posix"
    process = subprocess.Popen(  # noqa: S603  # nosec B603
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=use_process_group,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise RuntimeError("subprocess output pipes are unavailable")
    stdout = process.stdout
    stderr = process.stderr
    stdout_output = _BoundedOutput(max_output_chars)
    stderr_output = _BoundedOutput(max_output_chars)
    stdout_reader = Thread(target=stdout_output.collect, args=(stdout,), daemon=True)
    stderr_reader = Thread(target=stderr_output.collect, args=(stderr,), daemon=True)
    stdout_reader.start()
    stderr_reader.start()
    try:
        exit_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        if use_process_group:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                process.kill()
        else:
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_PROCESS_TERMINATION_TIMEOUT_SECONDS)
        timed_out = True
        exit_code = None
    else:
        timed_out = False
    drain_deadline = time.monotonic() + _OUTPUT_DRAIN_TIMEOUT_SECONDS
    for reader in (stdout_reader, stderr_reader):
        reader.join(timeout=max(0.0, drain_deadline - time.monotonic()))
    return _ProcessResult(
        exit_code=exit_code,
        stdout=stdout_output.text(),
        stderr=stderr_output.text(),
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
    def readiness(self) -> tuple[bool, str | None]: ...
    def available(self, command: CommandSpec) -> bool: ...
    def run(self, command: CommandSpec, root: Path) -> CheckResult: ...


class _RestrictedExecutor:
    def __init__(
        self,
        *,
        allowed: dict[str, tuple[str, ...]] | None,
        max_output_chars: int,
    ) -> None:
        defaults = ValidationPolicy().commands(Path.cwd())
        self.allowed = (
            {command.name: command.argv for command in defaults} if allowed is None else allowed
        )
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        self.max_output_chars = max_output_chars

    def _validate_command(self, command: CommandSpec) -> None:
        if self.allowed.get(command.name) != command.argv:
            raise CommandPolicyError(f"command is not allowlisted: {command.name}")
        if not self._has_approved_timeout(command):
            raise CommandPolicyError(f"command timeout is not approved: {command.name}")

    def _has_approved_timeout(self, command: CommandSpec) -> bool:
        return not (
            type(command.timeout_seconds) is not int
            or command.timeout_seconds <= 0
            or command.timeout_seconds > DEFAULT_TIMEOUT_SECONDS
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
        max_output_chars: int = 100_000,
    ) -> None:
        super().__init__(
            allowed=allowed,
            max_output_chars=max_output_chars,
        )

    def readiness(self) -> tuple[bool, str | None]:
        return (True, "restricted local execution provides weaker isolation")

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
            terminate_process_group=True,
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
        max_output_chars: int = 100_000,
    ) -> None:
        self.image = image
        self.docker = shutil.which("docker")
        super().__init__(
            allowed=allowed,
            max_output_chars=max_output_chars,
        )

    def readiness(self) -> tuple[bool, str | None]:
        if self.docker is None:
            return (False, "docker executable is unavailable")
        inspection = self._inspect_image()
        if inspection is None:
            return (False, "docker image inspection failed")
        if inspection.timed_out:
            return (False, "docker image inspection timed out")
        if inspection.exit_code != 0:
            return (False, f"validator image is unavailable: {self.image}")
        return (True, None)

    def available(self, command: CommandSpec) -> bool:
        if not self._is_allowed(command):
            return False
        inspection = self._inspect_image(
            timeout_seconds=min(
                _DOCKER_CONTROL_TIMEOUT_SECONDS, command.timeout_seconds
            )
        )
        return (
            inspection is not None
            and not inspection.timed_out
            and inspection.exit_code == 0
        )

    def _inspect_image(
        self, *, timeout_seconds: int = _DOCKER_CONTROL_TIMEOUT_SECONDS
    ) -> _ProcessResult | None:
        docker = self.docker
        if docker is None:
            return None
        try:
            return _run_with_bounded_output(
                [docker, "image", "inspect", self.image],
                timeout_seconds=timeout_seconds,
                max_output_chars=self.max_output_chars,
            )
        except OSError:
            return None

    def run(self, command: CommandSpec, root: Path) -> CheckResult:
        self._validate_command(command)
        docker = self.docker
        inspection = self._inspect_image(
            timeout_seconds=min(
                _DOCKER_CONTROL_TIMEOUT_SECONDS, command.timeout_seconds
            )
        )
        if inspection is not None and inspection.timed_out:
            return CheckResult(
                name=command.name,
                argv=list(command.argv),
                status=CheckStatus.SKIPPED,
                reason="docker image inspection timed out",
            )
        if docker is None or inspection is None or inspection.exit_code != 0:
            return CheckResult(
                name=command.name,
                argv=list(command.argv),
                status=CheckStatus.SKIPPED,
                reason="docker executable or validator image unavailable",
            )
        repository = root.resolve(strict=True)
        tmpfs = f"{Path(os.sep, 'tmp')}:rw,noexec,nosuid,size=256m"
        container_name = f"repogent-validator-{secrets.token_hex(16)}"
        argv = [
            docker,
            "run",
            "--rm",
            "--name",
            container_name,
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
            with suppress(OSError):
                _run_with_bounded_output(
                    [docker, "rm", "--force", container_name],
                    timeout_seconds=_DOCKER_CONTROL_TIMEOUT_SECONDS,
                    max_output_chars=self.max_output_chars,
                )
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
