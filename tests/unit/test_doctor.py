from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repogent import doctor, preflight
from repogent.doctor import DoctorService
from repogent.domain import ProviderReadiness
from repogent.mcp_models import DoctorRequest


class ReadyExecutor:
    def readiness(self) -> tuple[bool, str | None]:
        return True, None

    def available(self, _command: object) -> bool:
        return True


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
    ("reason", "remediation"),
    [
        ("Codex CLI executable not found", "Install the Codex CLI and ensure codex is on PATH"),
        ("Codex CLI is not authenticated", "Run codex login in your terminal"),
    ],
)
def test_doctor_reports_codex_readiness_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str, remediation: str
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
    assert check.remediation == remediation


def test_doctor_reports_unsupported_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.platform, "python_version_tuple", lambda: ("3", "10", "0"))

    report = DoctorService().run(DoctorRequest(repository=tmp_path, provider="openai"))

    assert report.ready is False
    assert report.checks == [
        report.checks[0],
        report.checks[1],
    ]
    assert report.checks[1].name == "python"
    assert report.checks[1].passed is False


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
