import json
from pathlib import Path

import pytest

from repogent.approvals import FakeApprover
from repogent.domain import Decision, ExecutionMode, IsolationLevel, ProviderReadiness, RunStatus
from repogent.executor_selection import ExecutorSelectionError, PreparedExecutor
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


def test_validate_run_options_rejects_regular_file_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository.py"
    repository.write_text("value = 1\n")

    with pytest.raises(ValueError, match="repository must be a directory"):
        validate_run_options(RunOptions(repository=repository, request="change"))


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

    class Registry:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def prepare(
            self, _repository: Path, mode: ExecutionMode, _policy: object
        ) -> PreparedExecutor:
            order.append("preflight")
            return PreparedExecutor(
                mode=mode,
                isolation_level=IsolationLevel.REDUCED_ISOLATION,
                preflight=_passing_preflight(),
                validator=object(),  # type: ignore[arg-type]
            )

    monkeypatch.setattr(run_builder, "ExecutorRegistry", Registry)
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


def test_build_run_prepares_selected_executor_with_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repogent import run_builder

    target = tmp_path / "target"
    target.mkdir()
    script = tmp_path / "script.json"
    script.write_text("[]")
    calls: list[tuple[Path, ExecutionMode]] = []

    class Registry:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def prepare(
            self, root: Path, mode: ExecutionMode, _policy: object
        ) -> PreparedExecutor:
            calls.append((root, mode))
            return PreparedExecutor(
                mode=mode,
                isolation_level=IsolationLevel.REDUCED_ISOLATION,
                preflight=_passing_preflight(),
                validator=object(),  # type: ignore[arg-type]
            )

    monkeypatch.setattr(run_builder, "ExecutorRegistry", Registry)

    build_run(
        RunOptions(
            repository=target,
            request="change",
            provider="scripted",
            script=script,
            executor="local",
            output_dir=tmp_path / "runs",
        ),
        lambda _run_id: FakeApprover([Decision.REJECTED]),
    )

    assert calls == [(target.resolve(), ExecutionMode.LOCAL)]


def test_build_run_does_not_fallback_when_docker_preflight_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repogent import run_builder

    target = tmp_path / "target"
    target.mkdir()
    provider_constructed = False

    def provider_must_not_be_constructed(*_args: object, **_kwargs: object) -> object:
        nonlocal provider_constructed
        provider_constructed = True
        return object()

    class Registry:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def prepare(
            self, _root: Path, mode: ExecutionMode, _policy: object
        ) -> PreparedExecutor:
            assert mode is ExecutionMode.DOCKER
            raise ExecutorSelectionError("selected executor is unavailable")

    monkeypatch.setattr(run_builder, "ExecutorRegistry", Registry)
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


@pytest.mark.parametrize("phase", ["preflight", "provider", "construction"])
@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_build_run_terminalizes_interrupt_as_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    interrupt_type: type[BaseException],
) -> None:
    from repogent import run_builder

    target = tmp_path / "target"
    target.mkdir()

    def interrupt() -> None:
        raise interrupt_type()

    class Registry:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def prepare(
            self, _repository: Path, mode: ExecutionMode, _policy: object
        ) -> PreparedExecutor:
            return PreparedExecutor(
                mode=mode,
                isolation_level=IsolationLevel.REDUCED_ISOLATION,
                preflight=_passing_preflight(),
                validator=object(),  # type: ignore[arg-type]
            )

    def interrupted_commands(_self: object, _repository: Path) -> object:
        interrupt()

    def interrupted_provider(*_args: object, **_kwargs: object) -> object:
        interrupt()

    def interrupted_approver(_run_id: str) -> FakeApprover:
        interrupt()
        raise AssertionError("unreachable")

    def default_approver(_run_id: str) -> FakeApprover:
        return FakeApprover([Decision.REJECTED])

    approver_factory = default_approver
    if phase == "preflight":
        monkeypatch.setattr(run_builder.ValidationPolicy, "commands", interrupted_commands)
    else:
        monkeypatch.setattr(run_builder, "ExecutorRegistry", Registry)
    if phase == "provider":
        monkeypatch.setattr(run_builder, "OpenAIProvider", interrupted_provider)
    elif phase == "construction":
        monkeypatch.setattr(run_builder, "OpenAIProvider", lambda **_kwargs: object())
        approver_factory = interrupted_approver

    with pytest.raises(RunBuildError, match="workflow interrupted by user") as caught:
        build_run(
            RunOptions(
                repository=target,
                request="change",
                executor="local",
                output_dir=tmp_path / "runs",
            ),
            approver_factory,
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
