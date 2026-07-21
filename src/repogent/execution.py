from __future__ import annotations

import configparser
import importlib.util
import os
import secrets
import shutil
import signal
import stat
import subprocess  # nosec B404
import sys
import time
import tomllib
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
_DISCOVERY_MAX_ENTRIES = 20_000
_DISCOVERY_MAX_DEPTH = 16
_PYTEST_CONFIG_MAX_BYTES = 256_000
_SUPPORTS_FD_RELATIVE_CONFIG = (
    os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)
_DOCKER_MODULE_PROBE = (
    "import importlib.util,sys;"
    "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 1)"
)
_DISCOVERY_IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "node_modules",
    "site-packages",
}


class _PytestConfigurationUncertain(Exception):
    pass


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
        pytest_required = _has_pytest_suite(root)
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


def _has_pytest_suite(root: Path) -> bool:
    if _has_pytest_configuration(root):
        return True
    pending: list[tuple[Path, int]] = [(root, 0)]
    entries_seen = 0
    while pending:
        directory, depth = pending.pop()
        try:
            entries = []
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    entries_seen += 1
                    if entries_seen > _DISCOVERY_MAX_ENTRIES:
                        return True
                    entries.append(entry)
        except OSError:
            return True
        for entry in sorted(entries, key=lambda item: item.name):
            name = entry.name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if name in _DISCOVERY_IGNORED_DIRECTORIES:
                        continue
                    if name in {"test", "tests"}:
                        return True
                    if depth >= _DISCOVERY_MAX_DEPTH:
                        return True
                    pending.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False) and name.endswith(".py"):
                    if name.startswith("test_") or name.endswith("_test.py"):
                        return True
            except OSError:
                return True
    return False


def _has_pytest_configuration(root: Path) -> bool:
    try:
        pyproject = _read_recognized_configuration(root, "pyproject.toml")
    except _PytestConfigurationUncertain:
        return True
    if pyproject is not None:
        try:
            data = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError:
            return True
        tool = data.get("tool", {})
        if isinstance(tool, dict) and isinstance(tool.get("pytest"), dict):
            return True
    for name, sections in (
        ("pytest.ini", {"pytest"}),
        ("setup.cfg", {"tool:pytest"}),
        ("tox.ini", {"pytest"}),
    ):
        try:
            contents = _read_recognized_configuration(root, name)
        except _PytestConfigurationUncertain:
            return True
        if contents is None:
            continue
        try:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(contents)
        except configparser.Error:
            return True
        if sections & set(parser.sections()):
            return True
    return False


def _read_recognized_configuration(root: Path, name: str) -> str | None:
    """Read one root config without following links or blocking on special files."""

    try:
        repository = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _PytestConfigurationUncertain from error
    if _SUPPORTS_FD_RELATIVE_CONFIG:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            directory_fd = os.open(repository, directory_flags)
        except OSError as error:
            raise _PytestConfigurationUncertain from error
        try:
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            except OSError as error:
                raise _PytestConfigurationUncertain from error
            _validate_configuration_metadata(metadata)
            try:
                descriptor = os.open(
                    name, _configuration_open_flags(), dir_fd=directory_fd
                )
            except OSError as error:
                raise _PytestConfigurationUncertain from error
            return _read_configuration_descriptor(descriptor, metadata)
        finally:
            os.close(directory_fd)

    path = repository / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _PytestConfigurationUncertain from error
    _validate_configuration_metadata(metadata)
    try:
        descriptor = os.open(path, _configuration_open_flags())
    except OSError as error:
        raise _PytestConfigurationUncertain from error
    return _read_configuration_descriptor(descriptor, metadata)


def _configuration_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _validate_configuration_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise _PytestConfigurationUncertain
    if metadata.st_size > _PYTEST_CONFIG_MAX_BYTES:
        raise _PytestConfigurationUncertain


def _read_configuration_descriptor(
    descriptor: int, expected: os.stat_result
) -> str:
    try:
        opened = os.fstat(descriptor)
        _validate_configuration_metadata(opened)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise _PytestConfigurationUncertain
        chunks: list[bytes] = []
        remaining = _PYTEST_CONFIG_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(_OUTPUT_READ_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        contents = b"".join(chunks)
        if len(contents) > _PYTEST_CONFIG_MAX_BYTES:
            raise _PytestConfigurationUncertain
        after = os.fstat(descriptor)
        if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise _PytestConfigurationUncertain
        try:
            return contents.decode("utf-8")
        except UnicodeError as error:
            raise _PytestConfigurationUncertain from error
    except OSError as error:
        raise _PytestConfigurationUncertain from error
    finally:
        os.close(descriptor)


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
        self._availability_cache: dict[tuple[str, str], bool] = {}
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
        docker = self.docker
        if docker is None or not self._is_allowed(command):
            return False
        tool = command.module or f"executable:{command.argv[0]}"
        cache_key = (self.image, tool)
        if cache_key in self._availability_cache:
            return self._availability_cache[cache_key]
        probe_command = (
            ["python", "-c", _DOCKER_MODULE_PROBE, command.module]
            if command.module is not None
            else [command.argv[0], "--version"]
        )
        argv = [
            docker,
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--read-only",
            "--cpus",
            "0.25",
            "--memory",
            "128m",
            "--pids-limit",
            "64",
            self.image,
            *probe_command,
        ]
        try:
            result = _run_with_bounded_output(
                argv,
                timeout_seconds=min(
                    _DOCKER_CONTROL_TIMEOUT_SECONDS, command.timeout_seconds
                ),
                max_output_chars=self.max_output_chars,
            )
        except OSError:
            available = False
        else:
            available = not result.timed_out and result.exit_code == 0
        self._availability_cache[cache_key] = available
        return available

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
