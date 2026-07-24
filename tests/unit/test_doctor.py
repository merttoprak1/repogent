from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repogent import doctor, preflight
from repogent.doctor import DoctorService
from repogent.domain import ExecutionMode, ProviderReadiness
from repogent.mcp_models import DoctorRequest


class ReadyExecutor:
    def readiness(self) -> tuple[bool, str | None]:
        return True, None

    def available(self, _command: object) -> bool:
        return True


def make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    return repository


def test_deferred_doctor_is_ready_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repository(tmp_path)
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: None)

    report = DoctorService().run(
        DoctorRequest(repository=repository, provider="scripted", executor="deferred")
    )

    assert report.ready is True
    docker = next(item for item in report.executors if item.mode is ExecutionMode.DOCKER)
    assert docker.available is False
    assert docker.remediation == "Install Docker and ensure docker is on PATH"
    assert all(not hasattr(item, "option_digest") for item in report.executors)


def test_explicit_docker_doctor_keeps_fail_closed_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = make_repository(tmp_path)
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: None)

    report = DoctorService().run(
        DoctorRequest(repository=repository, provider="scripted", executor="docker")
    )

    assert report.ready is False


def test_doctor_reports_ready_local_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "LocalExecutor", lambda **_kwargs: ReadyExecutor())

    report = DoctorService().run(
        DoctorRequest(repository=tmp_path, provider="openai", executor="local")
    )

    assert report.ready is True
    assert report.executor == "local"
    assert [check.name for check in report.checks][:2] == ["repository", "python"]


def test_doctor_rejects_regular_file_as_repository(tmp_path: Path) -> None:
    repository = tmp_path / "not-a-repository.py"
    repository.write_text("print('not a directory')\n")

    report = DoctorService().run(
        DoctorRequest(repository=repository, provider="openai", executor="local")
    )

    assert report.ready is False
    assert [check.name for check in report.checks] == ["repository"]
    assert report.checks[0].passed is False
    assert report.checks[0].message == "repository must be a directory"


def test_doctor_never_falls_back_from_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MissingDocker:
        def readiness(self) -> tuple[bool, str]:
            return False, "docker executable is unavailable"

        def available(self, _command: object) -> bool:
            return False

    def local_executor_must_not_be_constructed(**_kwargs: object) -> object:
        raise AssertionError("Docker failure must not fall back to LocalExecutor")

    monkeypatch.setattr(doctor, "DockerExecutor", lambda: MissingDocker())
    monkeypatch.setattr(doctor, "LocalExecutor", local_executor_must_not_be_constructed)

    report = DoctorService().run(
        DoctorRequest(repository=tmp_path, provider="openai", executor="docker")
    )

    assert report.executor == "docker"
    assert report.ready is False
    assert any(check.name == "executor" and not check.passed for check in report.checks)


def test_doctor_reports_missing_validator_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class MissingImage:
        def readiness(self) -> tuple[bool, str]:
            return False, "validator image is unavailable: repogent-validator:py311"

        def available(self, _command: object) -> bool:
            return True

    monkeypatch.setattr(doctor, "DockerExecutor", lambda: MissingImage())

    report = DoctorService().run(DoctorRequest(repository=tmp_path, provider="openai"))

    executor = next(check for check in report.checks if check.name == "executor")
    assert executor.passed is False
    assert executor.remediation == "Build the validator image with make validator-image"


def test_doctor_reports_unavailable_required_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_example.py").write_text("def test_example(): pass\n")

    class MissingPytest(ReadyExecutor):
        def available(self, command: object) -> bool:
            return command.name != "pytest"  # type: ignore[attr-defined]

    monkeypatch.setattr(doctor, "LocalExecutor", lambda **_kwargs: MissingPytest())

    report = DoctorService().run(
        DoctorRequest(repository=tmp_path, provider="openai", executor="local")
    )

    check = next(check for check in report.checks if check.name == "command:pytest")
    assert check.passed is False
    assert check.required is True


@pytest.mark.parametrize(
    ("reason", "message", "remediation"),
    [
        (
            "Codex CLI executable not found",
            "Codex CLI executable not found",
            "Install the Codex CLI and ensure codex is on PATH",
        ),
        (
            "Codex CLI is not authenticated",
            "Codex CLI is not authenticated",
            "Run codex login in your terminal",
        ),
        (
            "Codex CLI readiness check failed",
            "Codex CLI is not ready",
            "Inspect or reinstall the Codex CLI",
        ),
    ],
)
def test_doctor_reports_codex_readiness_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    message: str,
    remediation: str,
) -> None:
    monkeypatch.setattr(doctor, "LocalExecutor", lambda **_kwargs: ReadyExecutor())

    class NotReadyCodex:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def check_ready(self) -> ProviderReadiness:
            return ProviderReadiness(
                provider="codex-cli", model="default", ready=False, reason=reason
            )

    monkeypatch.setattr(doctor, "CodexCliProvider", NotReadyCodex)

    report = DoctorService().run(DoctorRequest(repository=tmp_path, executor="local"))

    check = report.checks[-1]
    assert check.name == "provider"
    assert check.passed is False
    assert check.message == message
    assert check.remediation == remediation


def test_doctor_reports_unsupported_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "python_version_tuple", lambda: ("3", "10", "0"))

    report = DoctorService().run(DoctorRequest(repository=tmp_path, provider="openai"))

    assert report.ready is False
    assert [check.name for check in report.checks] == ["repository", "python"]
    assert report.checks[1].name == "python"
    assert report.checks[1].passed is False


def test_doctor_skips_provider_readiness_after_required_preflight_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_example.py").write_text("def test_example(): pass\n")

    class MissingPytest(ReadyExecutor):
        def available(self, command: object) -> bool:
            return command.name != "pytest"  # type: ignore[attr-defined]

    def provider_must_not_be_constructed(**_kwargs: object) -> object:
        raise AssertionError("provider readiness must not run after failed preflight")

    monkeypatch.setattr(doctor, "LocalExecutor", lambda **_kwargs: MissingPytest())
    monkeypatch.setattr(doctor, "CodexCliProvider", provider_must_not_be_constructed)

    DoctorService().run(DoctorRequest(repository=tmp_path, executor="local"))


def test_doctor_codex_readiness_uses_only_noninteractive_login_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    executable = tmp_path / "codex"
    calls = tmp_path / "codex-calls.txt"
    help_flags = (
        "--ephemeral --sandbox --ignore-user-config --ignore-rules "
        "--output-schema --output-last-message -C --model"
    )
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {str(calls)!r}\n"
        "case \"$*\" in\n"
        "  --version) echo codex-cli-1.2.3 ;;\n"
        f"  'exec --help') echo {help_flags!r} ;;\n"
        "  'login status') echo 'Logged in' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr(doctor, "LocalExecutor", lambda **_kwargs: ReadyExecutor())
    monkeypatch.setattr("repogent.codex_cli.shutil.which", lambda _: str(executable))

    report = DoctorService().run(DoctorRequest(repository=repository, executor="local"))

    assert report.ready is True
    assert calls.read_text().splitlines() == ["--version", "exec --help", "login status"]


def test_doctor_has_no_evidence_or_remediation_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invoked: list[tuple[str, ...]] = []
    monkeypatch.setattr(doctor, "LocalExecutor", lambda **_kwargs: ReadyExecutor())

    def record_git(argv: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        invoked.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(preflight.subprocess, "run", record_git)

    DoctorService().run(DoctorRequest(repository=tmp_path, provider="openai", executor="local"))

    assert not (tmp_path.parent / ".repogent").exists()
    assert all("login" not in argv and "install" not in argv for argv in invoked)
