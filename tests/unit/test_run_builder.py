import json
from pathlib import Path

import pytest

from repogent.approvals import FakeApprover
from repogent.domain import Decision, ProviderReadiness, RunStatus
from repogent.preflight import PreflightReport
from repogent.run_builder import (
    RunBuildError,
    RunOptions,
    build_run,
    validate_run_options,
)


def _passing_preflight() -> PreflightReport:
    return PreflightReport(
        checks=[], git_commit=None, dirty=False, repository_fingerprint="repository"
    )


@pytest.mark.parametrize("provider", ["other", "", "OPENAI"])
def test_validate_run_options_rejects_invalid_provider(
    tmp_path: Path, provider: str
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ValueError, match="provider must be openai"):
        validate_run_options(
            RunOptions(repository=target, request="change", provider=provider)
        )


def test_validate_run_options_requires_script_for_scripted_provider(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(ValueError, match="--script is required"):
        validate_run_options(
            RunOptions(repository=target, request="change", provider="scripted")
        )


def test_validate_run_options_rejects_filesystem_root() -> None:
    with pytest.raises(ValueError, match="filesystem root"):
        validate_run_options(RunOptions(repository=Path("/"), request="change"))


def test_build_run_rejects_evidence_inside_repository(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    script = tmp_path / "script.json"
    script.write_text("[]")

    with pytest.raises(ValueError, match="outside target"):
        build_run(
            RunOptions(
                repository=target,
                request="change",
                provider="scripted",
                script=script,
                executor="local",
                output_dir=target / ".repogent",
            ),
            lambda _run_id: FakeApprover([Decision.REJECTED]),
        )


def test_build_run_keeps_preflight_before_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repogent import run_builder

    target = tmp_path / "target"
    target.mkdir()
    order: list[str] = []

    class ReadyCodex:
        def __init__(self, *, model: str | None, target_root: Path) -> None:
            assert model is None
            assert target_root == target.resolve()
            order.append("provider")

        def check_ready(self) -> ProviderReadiness:
            return ProviderReadiness(provider="codex-cli", model="default", ready=True)

    class PassingPreflight:
        def run(self, _repository: Path) -> PreflightReport:
            order.append("preflight")
            return _passing_preflight()

    monkeypatch.setattr(run_builder, "Preflight", lambda *_args: PassingPreflight())
    monkeypatch.setattr(run_builder, "CodexCliProvider", ReadyCodex)

    prepared = build_run(
        RunOptions(
            repository=target,
            request="change",
            provider="codex-cli",
            executor="local",
            output_dir=tmp_path / "runs",
        ),
        lambda _run_id: FakeApprover([Decision.REJECTED]),
    )

    assert order == ["preflight", "provider"]
    assert prepared.workflow.root == target.resolve()


def test_build_run_does_not_fallback_when_docker_preflight_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repogent import run_builder

    target = tmp_path / "target"
    target.mkdir()
    provider_constructed = False

    class UnavailableDocker:
        def readiness(self) -> tuple[bool, str]:
            return False, "docker unavailable"

        def available(self, _command: object) -> bool:
            return False

    def provider_must_not_be_constructed(*_args: object, **_kwargs: object) -> object:
        nonlocal provider_constructed
        provider_constructed = True
        return object()

    monkeypatch.setattr(run_builder, "DockerExecutor", UnavailableDocker)
    monkeypatch.setattr(run_builder, "OpenAIProvider", provider_must_not_be_constructed)

    with pytest.raises(RunBuildError, match="repository preflight failed") as caught:
        build_run(
            RunOptions(
                repository=target,
                request="change",
                output_dir=tmp_path / "runs",
            ),
            lambda _run_id: FakeApprover([Decision.REJECTED]),
        )

    assert provider_constructed is False
    assert caught.value.manifest is not None
    assert caught.value.manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED


def test_build_run_constructs_scripted_workflow(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    script = tmp_path / "script.json"
    script.write_text("[]")
    approver = FakeApprover([Decision.REJECTED])

    prepared = build_run(
        RunOptions(
            repository=target,
            request="change",
            provider="scripted",
            script=script,
            executor="local",
            output_dir=tmp_path / "runs",
        ),
        lambda _run_id: approver,
    )

    assert prepared.approver is approver
    assert prepared.workflow.approver is approver
    assert prepared.workflow.root == target.resolve()
    assert prepared.preflight.passed
    assert prepared.provider_readiness is None


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(130)])
def test_build_run_terminalizes_construction_interrupt_as_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: BaseException,
) -> None:
    from repogent import run_builder

    target = tmp_path / "target"
    target.mkdir()

    def interrupted_commands(_self: object, _repository: Path) -> object:
        raise interrupt

    monkeypatch.setattr(run_builder.ValidationPolicy, "commands", interrupted_commands)

    with pytest.raises(RunBuildError, match="workflow interrupted by user") as caught:
        build_run(
            RunOptions(
                repository=target,
                request="change",
                executor="local",
                output_dir=tmp_path / "runs",
            ),
            lambda _run_id: FakeApprover([Decision.REJECTED]),
        )

    assert caught.value.manifest is not None
    assert caught.value.manifest.status is RunStatus.CANCELLED
    run_directory = next((tmp_path / "runs").iterdir())
    terminal_event = json.loads((run_directory / "events.jsonl").read_text())
    assert terminal_event["data"]["status"] == RunStatus.CANCELLED.value


def test_build_run_passes_cancellation_predicate_to_workflow(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    script = tmp_path / "script.json"
    script.write_text("[]")

    def cancel_requested() -> bool:
        return True

    prepared = build_run(
        RunOptions(
            repository=target,
            request="change",
            provider="scripted",
            script=script,
            executor="local",
            output_dir=tmp_path / "runs",
        ),
        lambda _run_id: FakeApprover([Decision.REJECTED]),
        cancel_requested=cancel_requested,
    )

    assert prepared.workflow.cancel_requested is cancel_requested
