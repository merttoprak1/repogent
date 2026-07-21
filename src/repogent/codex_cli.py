from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess  # noqa: S404  # nosec B404
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import IO, Any, TypeVar

from pydantic import BaseModel, ValidationError

from repogent.domain import (
    ProviderCallEvidence,
    ProviderCallStatus,
    ProviderReadiness,
    ProviderUsage,
)
from repogent.providers import ProviderError, ProviderResult
from repogent.sanitization import redact_text, sanitize_data

T = TypeVar("T", bound=BaseModel)

DEFAULT_MAX_PROMPT_BYTES = 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
READINESS_TIMEOUT_SECONDS = 10.0
PROCESS_TERMINATION_TIMEOUT_SECONDS = 5.0
MAX_ERROR_CHARACTERS = 4096

_REQUIRED_EXEC_FLAGS = (
    "--ephemeral",
    "--sandbox",
    "--ignore-user-config",
    "--ignore-rules",
    "--output-schema",
    "--output-last-message",
    "-C",
    "--model",
)
_ALLOWED_ENVIRONMENT_KEYS = (
    "HOME",
    "PATH",
    "TMPDIR",
    "CODEX_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
)


class _OutputTooLargeError(RuntimeError):
    pass


class CodexCliProvider:
    def __init__(
        self,
        *,
        executable: str = "codex",
        model: str | None = None,
        secrets: Sequence[str] = (),
        max_prompt_bytes: int = DEFAULT_MAX_PROMPT_BYTES,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self.executable = executable
        self.model = model
        self.secrets = tuple(secrets)
        self.max_prompt_bytes = max_prompt_bytes
        self.max_output_bytes = max_output_bytes
        self._target_root = Path.cwd().resolve()
        self._target_root_pattern = re.compile(
            rf"(?<![A-Za-z0-9_.-]){re.escape(str(self._target_root))}"
            r"(?=$|[/\\\s:;,)=\]}\'\"?#&])"
        )
        self._ready: ProviderReadiness | None = None
        self._readiness_failure_status = ProviderCallStatus.EXECUTION_FAILED
        self._resolved_executable: str | None = None
        self._temporary_parent: Path | None = None
        self._backend_version: str | None = None
        self._version_checked = False
        self._help_checked = False
        self._login_checked = False
        self._invocation = 0

    @property
    def _model_name(self) -> str:
        if self.model is None:
            return "default"
        if self._model_validation_error() is not None:
            return "invalid"
        return self.model

    def check_ready(self) -> ProviderReadiness:
        if self._ready is not None:
            return self._ready

        if model_error := self._model_validation_error():
            return self._not_ready(model_error, ProviderCallStatus.CAPABILITY_MISSING)

        resolved_path: Path
        if self._resolved_executable is None:
            resolved = shutil.which(self.executable)
            if resolved is None:
                return self._not_ready(
                    "Codex CLI executable not found",
                    ProviderCallStatus.EXECUTABLE_MISSING,
                )
            resolved_path = Path(resolved).resolve()
            if self._is_target_root_path(str(resolved_path)):
                return self._not_ready(
                    "Codex CLI executable must be outside the target repository",
                    ProviderCallStatus.CAPABILITY_MISSING,
                )
            self._resolved_executable = str(resolved_path)
        else:
            resolved_path = Path(self._resolved_executable)

        if self._temporary_parent is None:
            temporary_parent = self._select_safe_temporary_parent()
            if temporary_parent is None:
                return self._not_ready(
                    "No safe writable temporary directory is available",
                    ProviderCallStatus.EXECUTION_FAILED,
                )
            self._temporary_parent = temporary_parent
        else:
            temporary_parent = self._temporary_parent

        environment = self._child_environment()
        try:
            with tempfile.TemporaryDirectory(
                prefix="repogent-codex-ready-", dir=temporary_parent
            ) as directory:
                workdir = Path(directory)
                if not self._version_checked:
                    version = self._run_readiness(
                        [str(resolved_path), "--version"], workdir, environment
                    )
                    if version.returncode != 0:
                        return self._not_ready(
                            self._failure_with_diagnostic(
                                "Codex CLI version check failed", version
                            ),
                            ProviderCallStatus.EXECUTION_FAILED,
                        )
                    self._backend_version = self._bounded_redacted_text(
                        version.stdout.strip(), fallback="unknown"
                    )
                    self._version_checked = True

                if not self._help_checked:
                    help_result = self._run_readiness(
                        [str(resolved_path), "exec", "--help"], workdir, environment
                    )
                    help_text = f"{help_result.stdout}\n{help_result.stderr}"
                    missing_flags = [
                        flag
                        for flag in _REQUIRED_EXEC_FLAGS
                        if not self._help_contains_flag(help_text, flag)
                    ]
                    if help_result.returncode != 0:
                        return self._not_ready(
                            self._failure_with_diagnostic(
                                "Codex CLI structured exec help failed", help_result
                            ),
                            ProviderCallStatus.CAPABILITY_MISSING,
                        )
                    if missing_flags:
                        return self._not_ready(
                            "Codex CLI lacks required structured exec flags: "
                            + ", ".join(missing_flags),
                            ProviderCallStatus.CAPABILITY_MISSING,
                        )
                    self._help_checked = True

                if not self._login_checked:
                    login = self._run_readiness(
                        [str(resolved_path), "login", "status"], workdir, environment
                    )
                    if login.returncode != 0:
                        return self._not_ready(
                            self._failure_with_diagnostic(
                                "Codex CLI is not authenticated", login
                            ),
                            ProviderCallStatus.AUTHENTICATION_FAILED,
                        )
                    self._login_checked = True
        except subprocess.TimeoutExpired:
            return self._not_ready(
                "Codex CLI readiness check timed out", ProviderCallStatus.TIMED_OUT
            )
        except _OutputTooLargeError:
            return self._not_ready(
                "Codex CLI readiness diagnostic exceeded the configured limit",
                ProviderCallStatus.OUTPUT_TOO_LARGE,
            )
        except OSError:
            return self._not_ready(
                "Codex CLI readiness check failed",
                ProviderCallStatus.EXECUTION_FAILED,
            )

        readiness = ProviderReadiness(
            provider="codex-cli",
            model=self._model_name,
            ready=True,
            backend_version=self._backend_version,
        )
        self._ready = readiness
        return readiness

    def generate(
        self,
        *,
        role: str,
        system_prompt: str,
        payload: Mapping[str, Any],
        output_type: type[T],
        timeout_seconds: float | None = None,
    ) -> ProviderResult[T]:
        started = time.monotonic()
        self._invocation += 1
        readiness = self.check_ready()
        if not readiness.ready:
            raise self._provider_error(
                role=role,
                status=self._readiness_failure_status,
                started=started,
                readiness=readiness,
                message=readiness.reason or "Codex CLI is not ready",
            )

        prompt = json.dumps(
            {
                "payload": self._redact_target_root_data(
                    sanitize_data(payload, self.secrets)
                ),
                "system_prompt": self._redact_target_root(
                    redact_text(system_prompt, self.secrets)
                ),
            },
            sort_keys=True,
        )
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > self.max_prompt_bytes:
            raise self._provider_error(
                role=role,
                status=ProviderCallStatus.OUTPUT_TOO_LARGE,
                started=started,
                readiness=readiness,
                message="Codex CLI prompt exceeded the configured limit",
            )

        remaining_timeout = self._remaining_timeout(timeout_seconds, started)
        if remaining_timeout is not None and remaining_timeout <= 0:
            raise self._provider_error(
                role=role,
                status=ProviderCallStatus.TIMED_OUT,
                started=started,
                readiness=readiness,
                message="provider timeout exhausted",
            )

        temporary_parent = self._temporary_parent
        if temporary_parent is None:
            raise RuntimeError("successful readiness did not retain a temporary parent")
        with tempfile.TemporaryDirectory(
            prefix="repogent-codex-", dir=temporary_parent
        ) as directory:
            workdir = Path(directory)
            schema_path = self._owner_only_file(workdir, "schema-", ".json")
            result_path = self._owner_only_file(workdir, "result-", ".json")
            prompt_path = self._owner_only_file(workdir, "prompt-", ".json")
            stdout_path = self._owner_only_file(workdir, "stdout-", ".log")
            stderr_path = self._owner_only_file(workdir, "stderr-", ".log")
            schema_bytes = json.dumps(
                output_type.model_json_schema(), sort_keys=True
            ).encode("utf-8")
            if len(schema_bytes) > self.max_output_bytes:
                raise self._provider_error(
                    role=role,
                    status=ProviderCallStatus.OUTPUT_TOO_LARGE,
                    started=started,
                    readiness=readiness,
                    message="Codex CLI output schema exceeded the configured limit",
                )
            schema_path.write_bytes(schema_bytes)
            prompt_path.write_bytes(prompt_bytes)
            resolved = self._resolved_executable
            if resolved is None:
                raise RuntimeError("successful readiness did not retain the executable")
            argv = [
                resolved,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
                "-C",
                str(workdir),
            ]
            if self.model is not None:
                argv.extend(["--model", self.model])
            argv.append("-")
            try:
                exit_code = self._run_exec(
                    argv=argv,
                    workdir=workdir,
                    prompt_path=prompt_path,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    timeout=remaining_timeout,
                )
            except subprocess.TimeoutExpired as error:
                raise self._provider_error(
                    role=role,
                    status=ProviderCallStatus.TIMED_OUT,
                    started=started,
                    readiness=readiness,
                    message="Codex CLI structured exec timed out",
                ) from error
            except OSError as error:
                raise self._provider_error(
                    role=role,
                    status=ProviderCallStatus.EXECUTION_FAILED,
                    started=started,
                    readiness=readiness,
                    message="Codex CLI structured exec failed to start",
                ) from error

            try:
                self._check_bounded_file(stdout_path, "diagnostic")
                self._check_bounded_file(stderr_path, "diagnostic")
            except _OutputTooLargeError as error:
                raise self._provider_error(
                    role=role,
                    status=ProviderCallStatus.OUTPUT_TOO_LARGE,
                    started=started,
                    readiness=readiness,
                    exit_code=exit_code,
                    message=str(error),
                ) from error

            if exit_code != 0:
                raise self._provider_error(
                    role=role,
                    status=ProviderCallStatus.EXECUTION_FAILED,
                    started=started,
                    readiness=readiness,
                    exit_code=exit_code,
                    message=self._diagnostic_excerpt(
                        stdout_path,
                        stderr_path,
                        fallback="Codex CLI structured exec failed",
                    ),
                )

            try:
                raw_output = self._read_bounded_file(result_path, "structured output")
            except _OutputTooLargeError as error:
                raise self._provider_error(
                    role=role,
                    status=ProviderCallStatus.OUTPUT_TOO_LARGE,
                    started=started,
                    readiness=readiness,
                    exit_code=exit_code,
                    message=str(error),
                ) from error
            except OSError as error:
                raise self._provider_error(
                    role=role,
                    status=ProviderCallStatus.INVALID_OUTPUT,
                    started=started,
                    readiness=readiness,
                    exit_code=exit_code,
                    message="Codex CLI structured output was missing",
                ) from error
            if not raw_output:
                raise self._provider_error(
                    role=role,
                    status=ProviderCallStatus.INVALID_OUTPUT,
                    started=started,
                    readiness=readiness,
                    exit_code=exit_code,
                    message="Codex CLI structured output was missing or empty",
                )
            try:
                parsed_output = json.loads(raw_output)
                output = output_type.model_validate(parsed_output)
            except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
                raise self._provider_error(
                    role=role,
                    status=ProviderCallStatus.INVALID_OUTPUT,
                    started=started,
                    readiness=readiness,
                    exit_code=exit_code,
                    message="Codex CLI structured output was invalid",
                ) from error

        latency = time.monotonic() - started
        return ProviderResult(
            output=output,
            usage=ProviderUsage(model=self._model_name, latency_seconds=latency),
            evidence=ProviderCallEvidence(
                provider="codex-cli",
                model=self._model_name,
                role=role,
                invocation=self._invocation,
                status=ProviderCallStatus.COMPLETED,
                backend_version=readiness.backend_version,
                exit_code=exit_code,
                latency_seconds=latency,
                structured_output_valid=True,
            ),
        )

    def _run_readiness(
        self, argv: list[str], workdir: Path, environment: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        stdout_path = self._owner_only_file(workdir, "readiness-stdout-", ".log")
        stderr_path = self._owner_only_file(workdir, "readiness-stderr-", ".log")
        with (
            stdout_path.open("wb") as stdout,
            stderr_path.open("wb") as stderr,
        ):
            returncode = self._spawn_and_wait(
                argv=argv,
                workdir=workdir,
                environment=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=READINESS_TIMEOUT_SECONDS,
            )
        stdout_text = self._read_bounded_file(stdout_path, "readiness diagnostic").decode(
            "utf-8", errors="replace"
        )
        stderr_text = self._read_bounded_file(stderr_path, "readiness diagnostic").decode(
            "utf-8", errors="replace"
        )
        return subprocess.CompletedProcess(
            argv, returncode, stdout=stdout_text, stderr=stderr_text
        )

    def _run_exec(
        self,
        *,
        argv: list[str],
        workdir: Path,
        prompt_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        timeout: float | None,
    ) -> int:
        with (
            prompt_path.open("rb") as prompt,
            stdout_path.open("wb") as stdout,
            stderr_path.open("wb") as stderr,
        ):
            return self._spawn_and_wait(
                argv=argv,
                workdir=workdir,
                environment=self._child_environment(),
                stdin=prompt,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
            )

    def _spawn_and_wait(
        self,
        *,
        argv: list[str],
        workdir: Path,
        environment: Mapping[str, str],
        stdin: int | IO[bytes],
        stdout: IO[bytes],
        stderr: IO[bytes],
        timeout: float | None,
    ) -> int:
        process = subprocess.Popen(  # noqa: S603  # nosec B603
            argv,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=workdir,
            env=dict(environment),
            shell=False,
        )
        try:
            return process.wait(timeout=timeout)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            self._terminate_and_wait(process)
            raise

    @staticmethod
    def _terminate_and_wait(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            process.wait()
            return
        process.terminate()
        try:
            process.wait(timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    @staticmethod
    def _owner_only_file(workdir: Path, prefix: str, suffix: str) -> Path:
        descriptor, raw_path = tempfile.mkstemp(
            dir=workdir, prefix=prefix, suffix=suffix, text=False
        )
        os.close(descriptor)
        path = Path(raw_path)
        path.chmod(0o600)
        return path

    def _check_bounded_file(self, path: Path, label: str) -> None:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"{label} was not a regular file")
        if metadata.st_size > self.max_output_bytes:
            raise _OutputTooLargeError(f"Codex CLI {label} exceeded the configured limit")

    def _read_bounded_file(self, path: Path, label: str) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"{label} was not a regular file")
            if metadata.st_size > self.max_output_bytes:
                raise _OutputTooLargeError(
                    f"Codex CLI {label} exceeded the configured limit"
                )
            content = stream.read(self.max_output_bytes + 1)
        if len(content) > self.max_output_bytes:
            raise _OutputTooLargeError(
                f"Codex CLI {label} exceeded the configured limit"
            )
        return content

    def _diagnostic_excerpt(
        self, stdout_path: Path, stderr_path: Path, *, fallback: str
    ) -> str:
        stderr = self._read_bounded_file(stderr_path, "diagnostic").decode(
            "utf-8", errors="replace"
        )
        stdout = self._read_bounded_file(stdout_path, "diagnostic").decode(
            "utf-8", errors="replace"
        )
        return self._bounded_redacted_text(
            "\n".join(item.strip() for item in (stderr, stdout) if item.strip()),
            fallback=fallback,
        )

    def _failure_with_diagnostic(
        self, prefix: str, completed: subprocess.CompletedProcess[str]
    ) -> str:
        diagnostic = "\n".join(
            item.strip() for item in (completed.stderr, completed.stdout) if item.strip()
        )
        if diagnostic:
            return self._bounded_redacted_text(f"{prefix}: {diagnostic}", fallback=prefix)
        return prefix

    def _bounded_redacted_text(self, text: str, *, fallback: str) -> str:
        placeholder = "\ue000repogent-redacted\ue001"
        protected = text.replace("[REDACTED]", placeholder)
        redacted = self._redact_target_root(
            redact_text(protected, self.secrets)
        )
        for credential_location in self._credential_locations():
            redacted = redacted.replace(credential_location, "[REDACTED]")
        redacted = redacted.replace(placeholder, "[REDACTED]").strip()
        if not redacted:
            redacted = fallback
        return redacted[:MAX_ERROR_CHARACTERS]

    @staticmethod
    def _help_contains_flag(help_text: str, flag: str) -> bool:
        return bool(
            re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(flag)}(?![A-Za-z0-9_-])",
                help_text,
            )
        )

    @staticmethod
    def _credential_locations() -> tuple[str, ...]:
        locations: set[str] = {"~/.codex"}
        if codex_home := os.environ.get("CODEX_HOME"):
            locations.add(codex_home)
        if home := os.environ.get("HOME"):
            locations.add(str(Path(home) / ".codex"))
        return tuple(sorted(locations, key=len, reverse=True))

    def _model_validation_error(self) -> str | None:
        if self.model is None:
            return None
        if not 1 <= len(self.model) <= 200:
            return "Codex CLI model must contain 1 to 200 printable characters"
        if "\x00" in self.model or not self.model.isprintable():
            return "Codex CLI model must contain 1 to 200 printable characters"
        return None

    @staticmethod
    def _remaining_timeout(timeout_seconds: float | None, started: float) -> float | None:
        if timeout_seconds is None:
            return None
        return timeout_seconds - (time.monotonic() - started)

    def _child_environment(self) -> dict[str, str]:
        environment: dict[str, str] = {}
        for key in _ALLOWED_ENVIRONMENT_KEYS:
            value = os.environ.get(key)
            if value is None:
                continue
            if key == "PATH":
                safe_entries = [
                    entry
                    for entry in value.split(os.pathsep)
                    if not self._is_target_root_path(entry)
                ]
                environment[key] = os.pathsep.join(safe_entries)
            elif not self._contains_target_root_path(value):
                environment[key] = value
        return environment

    def _select_safe_temporary_parent(self) -> Path | None:
        candidates: list[Path] = []
        with suppress(OSError):
            candidates.append(Path(tempfile.gettempdir()))
        if os.name == "nt":
            candidates.extend(
                Path(value)
                for key in ("TEMP", "TMP")
                if (value := os.environ.get(key))
            )
            if local_app_data := os.environ.get("LOCALAPPDATA"):
                candidates.append(Path(local_app_data) / "Temp")
            if system_root := os.environ.get("SYSTEMROOT"):
                candidates.append(Path(system_root) / "Temp")
        else:
            candidates.extend(
                (Path("/tmp"), Path("/var/tmp"))  # noqa: S108  # nosec B108
            )

        seen: set[Path] = set()
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve(strict=True)
            except OSError:
                continue
            if resolved in seen or not resolved.is_dir():
                continue
            seen.add(resolved)
            if self._is_target_root_path(str(resolved)):
                continue
            try:
                with tempfile.TemporaryDirectory(
                    prefix="repogent-codex-probe-", dir=resolved
                ) as probe:
                    if self._is_target_root_path(str(Path(probe).resolve())):
                        continue
            except OSError:
                continue
            return resolved
        return None

    def _redact_target_root(self, text: str) -> str:
        return self._target_root_pattern.sub("[REDACTED]", text)

    def _redact_target_root_data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._redact_target_root(value)
        if isinstance(value, Mapping):
            return {
                self._redact_target_root(key) if isinstance(key, str) else key: (
                    self._redact_target_root_data(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, tuple):
            return tuple(self._redact_target_root_data(item) for item in value)
        if isinstance(value, list):
            return [self._redact_target_root_data(item) for item in value]
        return value

    def _contains_target_root_path(self, value: str) -> bool:
        return bool(self._target_root_pattern.search(value)) or self._is_target_root_path(
            value
        )

    def _is_target_root_path(self, value: str) -> bool:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return False
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            resolved = candidate
        return resolved == self._target_root or self._target_root in resolved.parents

    def _not_ready(
        self, reason: str, status: ProviderCallStatus
    ) -> ProviderReadiness:
        self._readiness_failure_status = status
        return ProviderReadiness(
            provider="codex-cli",
            model=self._model_name,
            ready=False,
            reason=self._bounded_redacted_text(
                reason, fallback="Codex CLI is not ready"
            ),
        )

    def _provider_error(
        self,
        *,
        role: str,
        status: ProviderCallStatus,
        started: float,
        readiness: ProviderReadiness,
        message: str,
        exit_code: int | None = None,
    ) -> ProviderError:
        bounded_message = self._bounded_redacted_text(
            message, fallback="Codex CLI provider failed"
        )
        return ProviderError(
            bounded_message,
            retryable=False,
            evidence=self._evidence(
                role=role,
                status=status,
                started=started,
                readiness=readiness,
                exit_code=exit_code,
                error=bounded_message,
            ),
        )

    def _evidence(
        self,
        *,
        role: str,
        status: ProviderCallStatus,
        started: float,
        readiness: ProviderReadiness,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> ProviderCallEvidence:
        return ProviderCallEvidence(
            provider="codex-cli",
            model=self._model_name,
            role=role,
            invocation=self._invocation,
            status=status,
            backend_version=readiness.backend_version,
            exit_code=exit_code,
            latency_seconds=time.monotonic() - started,
            structured_output_valid=False,
            error=(
                self._bounded_redacted_text(error, fallback="Codex CLI provider failed")
                if error is not None
                else None
            ),
        )
