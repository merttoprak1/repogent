from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # noqa: S404  # nosec B404
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

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
        self._resolved_executable: str | None = None
        self._invocation = 0

    @property
    def _model_name(self) -> str:
        return self.model or "default"

    def check_ready(self) -> ProviderReadiness:
        if self._ready is not None:
            return self._ready

        resolved = shutil.which(self.executable)
        if resolved is None:
            return self._not_ready(f"Codex CLI executable not found: {self.executable}")

        environment = self._child_environment()
        try:
            with tempfile.TemporaryDirectory(prefix="repogent-codex-ready-") as directory:
                workdir = Path(directory)
                version = self._run_readiness(
                    [resolved, "--version"], workdir, environment
                )
                if version.returncode != 0:
                    return self._not_ready("Codex CLI version check failed")
                backend_version = version.stdout.strip()

                help_result = self._run_readiness(
                    [resolved, "exec", "--help"], workdir, environment
                )
                help_text = f"{help_result.stdout}\n{help_result.stderr}"
                missing_flags = [flag for flag in _REQUIRED_EXEC_FLAGS if flag not in help_text]
                if help_result.returncode != 0 or missing_flags:
                    return self._not_ready("Codex CLI lacks required structured exec flags")

                login = self._run_readiness(
                    [resolved, "login", "status"], workdir, environment
                )
                if login.returncode != 0:
                    return self._not_ready("Codex CLI is not authenticated")
        except (OSError, subprocess.TimeoutExpired) as error:
            return self._not_ready(
                f"Codex CLI readiness check failed: {redact_text(str(error), self.secrets)}"
            )

        readiness = ProviderReadiness(
            provider="codex-cli",
            model=self._model_name,
            ready=True,
            backend_version=backend_version,
        )
        self._resolved_executable = resolved
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
            status = self._readiness_failure_status(readiness)
            evidence = self._evidence(
                role=role,
                status=status,
                started=started,
                readiness=readiness,
                error=readiness.reason,
            )
            raise ProviderError(
                readiness.reason or "Codex CLI is not ready",
                retryable=False,
                evidence=evidence,
            )

        remaining_timeout = None
        if timeout_seconds is not None:
            remaining_timeout = timeout_seconds - (time.monotonic() - started)
            if remaining_timeout <= 0:
                evidence = self._evidence(
                    role=role,
                    status=ProviderCallStatus.TIMED_OUT,
                    started=started,
                    readiness=readiness,
                    error="provider timeout exhausted",
                )
                raise ProviderError(
                    "provider timeout exhausted", retryable=False, evidence=evidence
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

        with tempfile.TemporaryDirectory(prefix="repogent-codex-") as directory:
            workdir = Path(directory)
            schema_path = workdir / "schema.json"
            result_path = workdir / "result.json"
            schema_path.write_text(
                json.dumps(output_type.model_json_schema(), sort_keys=True),
                encoding="utf-8",
            )
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
            completed = subprocess.run(  # noqa: S603  # nosec B603
                argv,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=workdir,
                env=self._child_environment(),
                timeout=remaining_timeout,
                check=False,
                shell=False,
            )
            if completed.returncode != 0:
                message = redact_text(completed.stderr.strip(), self.secrets)
                message = message or "Codex CLI structured exec failed"
                evidence = self._evidence(
                    role=role,
                    status=ProviderCallStatus.EXECUTION_FAILED,
                    started=started,
                    readiness=readiness,
                    exit_code=completed.returncode,
                    error=message,
                )
                raise ProviderError(message, retryable=False, evidence=evidence)
            try:
                raw_output = result_path.read_bytes()
                if len(raw_output) > self.max_output_bytes:
                    raise ValueError("Codex CLI structured output exceeded the configured limit")
                output = output_type.model_validate_json(raw_output)
            except (OSError, ValueError, ValidationError) as error:
                message = redact_text(str(error), self.secrets)
                evidence = self._evidence(
                    role=role,
                    status=ProviderCallStatus.INVALID_OUTPUT,
                    started=started,
                    readiness=readiness,
                    exit_code=completed.returncode,
                    error=message,
                )
                raise ProviderError(message, retryable=False, evidence=evidence) from error

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
                exit_code=completed.returncode,
                latency_seconds=latency,
                structured_output_valid=True,
            ),
        )

    def _run_readiness(
        self, argv: list[str], workdir: Path, environment: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603  # nosec B603
            argv,
            text=True,
            capture_output=True,
            cwd=workdir,
            env=environment,
            timeout=READINESS_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )

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

    def _not_ready(self, reason: str) -> ProviderReadiness:
        return ProviderReadiness(
            provider="codex-cli",
            model=self._model_name,
            ready=False,
            reason=reason,
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
            error=error,
        )

    @staticmethod
    def _readiness_failure_status(readiness: ProviderReadiness) -> ProviderCallStatus:
        reason = readiness.reason or ""
        if "not found" in reason:
            return ProviderCallStatus.EXECUTABLE_MISSING
        if "authenticated" in reason:
            return ProviderCallStatus.AUTHENTICATION_FAILED
        if "flags" in reason:
            return ProviderCallStatus.CAPABILITY_MISSING
        return ProviderCallStatus.EXECUTION_FAILED
