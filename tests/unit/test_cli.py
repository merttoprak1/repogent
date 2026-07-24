import importlib
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
from openai import OpenAIError
from typer.testing import CliRunner

from repogent import cli, run_builder
from repogent.cli import app
from repogent.domain import EventKind, ProviderReadiness, RunEvent, RunStage, RunStatus
from repogent.executor_selection import FixedExecutorSelector
from repogent.preflight import PreflightReport
from repogent.run_builder import RunOptions

runner = CliRunner()


def test_run_delegates_construction_to_shared_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    evidence = tmp_path / "runs"
    captured: dict[str, object] = {}
    durable_events: list[RunEvent] = []

    class FakeWorkflow:
        def __init__(self, events: object) -> None:
            self.events = events

        def run(self) -> object:
            self.events.emit(
                RunEvent(
                    run_id="run-test",
                    sequence=1,
                    kind=EventKind.WARNING,
                    stage=RunStage.CREATED.value,
                    message="builder event",
                )
            )
            return type(
                "Result", (), {"run_id": "run-test", "status": RunStatus.COMPLETED}
            )()

    class FakeStore:
        root = evidence / "run-test"
        secrets: list[str] = []

        def event_store(self) -> object:
            return type(
                "Sink",
                (),
                {"emit": lambda _self, event: durable_events.append(event)},
            )()

    def fake_build_run(
        options: RunOptions,
        approver_factory: object,
        *,
        events: object,
    ) -> object:
        captured["options"] = options
        captured["approver_factory"] = approver_factory
        captured["events"] = events
        return type(
            "Prepared", (), {"workflow": FakeWorkflow(events), "store": FakeStore()}
        )()

    monkeypatch.setattr(cli, "build_run", fake_build_run, raising=False)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--executor",
            "local",
            "--output-dir",
            str(evidence),
        ],
    )

    assert result.exit_code == 0
    assert captured["options"] == RunOptions(
        repository=target.resolve(),
        request="change",
        executor="local",
        output_dir=evidence,
    )
    assert [event.message for event in durable_events] == ["builder event"]
    assert "[warning] builder event" in result.output


def test_run_without_executor_still_builds_docker_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    evidence = tmp_path / "runs"
    captured: dict[str, object] = {}

    class FakeWorkflow:
        def __init__(self, events: object) -> None:
            self.events = events

        def run(self) -> object:
            return type(
                "Result", (), {"run_id": "run-test", "status": RunStatus.COMPLETED}
            )()

    class FakeStore:
        root = evidence / "run-test"
        secrets: list[str] = []

        def event_store(self) -> object:
            return type("Sink", (), {"emit": lambda _self, _event: None})()

    def fake_build_run(
        options: RunOptions,
        approver_factory: object,
        *,
        events: object,
    ) -> object:
        captured["options"] = options
        return type(
            "Prepared", (), {"workflow": FakeWorkflow(events), "store": FakeStore()}
        )()

    monkeypatch.setattr(cli, "build_run", fake_build_run, raising=False)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--output-dir",
            str(evidence),
        ],
    )

    assert result.exit_code == 0
    options = captured["options"]
    assert isinstance(options, RunOptions)
    assert options.executor == "docker"


def test_explicit_local_cli_never_uses_deferred_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    evidence = tmp_path / "runs"
    captured: dict[str, object] = {}

    class PassingPreflight:
        def run(self, _repository: Path) -> PreflightReport:
            return PreflightReport(
                checks=[], git_commit=None, dirty=False, repository_fingerprint="repository"
            )

    class FakeWorkflow:
        def __init__(self, **kwargs: object) -> None:
            captured["executor_selector"] = kwargs["executor_selector"]

        def run(self) -> object:
            return type(
                "Result", (), {"run_id": "run-test", "status": RunStatus.COMPLETED}
            )()

    monkeypatch.setattr(run_builder, "Preflight", lambda *_args: PassingPreflight())
    monkeypatch.setattr(run_builder, "OpenAIProvider", lambda **_kwargs: object())
    monkeypatch.setattr(run_builder, "Workflow", FakeWorkflow)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--executor",
            "local",
            "--output-dir",
            str(evidence),
        ],
    )

    assert result.exit_code == 0
    assert isinstance(captured["executor_selector"], FixedExecutorSelector)


def test_analyze_prints_inventory_and_ranked_localization(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "auth.py").write_text("def login():\n    return True\n")
    result = runner.invoke(app, ["analyze", str(target), "--request", "change login"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["inventory"]["files"][0]["path"] == "auth.py"
    assert payload["localization"]["snippets"][0]["path"] == "auth.py"
    assert any(node["qualified_name"] == "auth.login" for node in payload["symbol_graph"]["nodes"])


def test_run_requires_script_for_scripted_provider(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--provider",
            "scripted",
        ],
    )
    assert result.exit_code == 2
    assert "--script is required" in result.stdout


def test_run_rejects_output_directory_inside_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    script = tmp_path / "script.json"
    script.write_text("[]")
    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--provider",
            "scripted",
            "--script",
            str(script),
            "--output-dir",
            str(target / ".repogent"),
        ],
    )
    assert result.exit_code == 2
    assert "outside target" in result.stdout


def test_run_rejects_unknown_provider_without_traceback(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--provider",
            "other",
        ],
    )
    assert result.exit_code == 2
    assert "provider must be openai" in result.output
    assert "codex-cli" in result.output
    assert "scripted" in result.output
    assert "Traceback" not in result.output


def test_run_rejects_unknown_executor_without_traceback(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--executor",
            "other",
        ],
    )
    assert result.exit_code == 2
    assert "executor must be docker or local" in result.output
    assert "Traceback" not in result.output


def test_run_rejects_deferred_executor_without_traceback(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--executor",
            "deferred",
        ],
    )
    assert result.exit_code == 2
    assert "executor must be docker or local" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_run_rejects_script_with_openai_provider(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    script = tmp_path / "script.json"
    script.write_text("[]")

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--provider",
            "openai",
            "--script",
            str(script),
        ],
    )

    assert result.exit_code == 2
    assert "--script is only supported with --provider scripted" in result.output
    assert "Traceback" not in result.output


def test_run_rejects_script_with_codex_cli_provider_without_traceback(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    script = tmp_path / "script.json"
    script.write_text("[]")

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--provider",
            "codex-cli",
            "--script",
            str(script),
        ],
    )

    assert result.exit_code == 2
    assert "--script is only supported with --provider scripted" in result.output
    assert "Traceback" not in result.output


def test_run_constructs_ready_codex_after_preflight_and_records_default_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    evidence = tmp_path / "runs"
    preflight_complete = False
    captured: dict[str, object] = {}

    class ReadyCodex:
        def __init__(self, *, model: str | None, target_root: Path) -> None:
            assert preflight_complete is True
            captured["model"] = model
            captured["target_root"] = target_root

        def check_ready(self) -> ProviderReadiness:
            return ProviderReadiness(provider="codex-cli", model="default", ready=True)

    class PassingPreflight:
        def run(self, _repository: Path) -> PreflightReport:
            nonlocal preflight_complete
            preflight_complete = True
            return PreflightReport(
                checks=[], git_commit=None, dirty=False, repository_fingerprint="repository"
            )

    class FakeWorkflow:
        def __init__(self, **kwargs: object) -> None:
            captured["workflow_provider"] = kwargs["roles"]

        def run(self) -> object:
            return type(
                "Result", (), {"run_id": "run-test", "status": RunStatus.COMPLETED}
            )()

    def record_fingerprint(
        provider: str, model: str, executor: str, commands: object
    ) -> str:
        captured["fingerprint"] = (provider, model, executor, commands)
        return "fingerprint"

    monkeypatch.setattr(run_builder, "CodexCliProvider", ReadyCodex)
    monkeypatch.setattr(run_builder, "Preflight", lambda *_args: PassingPreflight())
    monkeypatch.setattr(run_builder, "configuration_fingerprint", record_fingerprint)
    monkeypatch.setattr(run_builder.RoleSet, "from_provider", lambda provider: provider)
    monkeypatch.setattr(run_builder, "Workflow", FakeWorkflow)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            "target",
            "--request",
            "change",
            "--provider",
            "codex-cli",
            "--executor",
            "local",
            "--output-dir",
            str(evidence),
        ],
    )

    assert result.exit_code == 0
    assert captured["model"] is None
    assert captured["target_root"] == target.resolve()
    assert captured["fingerprint"][:3] == ("codex-cli", "default", "local")
    readiness = json.loads((next(evidence.iterdir()) / "provider-readiness-001.json").read_text())
    assert readiness == {
        "schema_version": "1",
        "provider": "codex-cli",
        "model": "default",
        "ready": True,
        "backend_version": None,
        "reason": None,
    }


def test_run_terminalizes_not_ready_codex_without_constructing_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    evidence = tmp_path / "runs"

    class NotReadyCodex:
        def __init__(self, *, model: str | None, target_root: Path) -> None:
            assert model is None
            assert target_root == target.resolve()

        def check_ready(self) -> ProviderReadiness:
            return ProviderReadiness(
                provider="codex-cli",
                model="default",
                ready=False,
                reason="Codex CLI is not authenticated; run codex login",
            )

    class PassingPreflight:
        def run(self, _repository: Path) -> PreflightReport:
            return PreflightReport(
                checks=[], git_commit=None, dirty=False, repository_fingerprint="repository"
            )

    def workflow_must_not_be_constructed(**_kwargs: object) -> object:
        raise AssertionError("Workflow must not be constructed when Codex is not ready")

    monkeypatch.setattr(run_builder, "CodexCliProvider", NotReadyCodex)
    monkeypatch.setattr(run_builder, "Preflight", lambda *_args: PassingPreflight())
    monkeypatch.setattr(run_builder, "Workflow", workflow_must_not_be_constructed)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--provider",
            "codex-cli",
            "--executor",
            "local",
            "--output-dir",
            str(evidence),
        ],
    )

    assert result.exit_code == 2
    assert "run codex login" in result.output
    assert "Traceback" not in result.output
    run_directory = next(evidence.iterdir())
    assert (run_directory / "provider-readiness-001.json").exists()
    manifest = json.loads((run_directory / "run.json").read_text())
    assert manifest["status"] == RunStatus.HUMAN_INTERVENTION_REQUIRED.value
    assert "run codex login" in manifest["reason"]
    assert (run_directory / "report.md").exists()
    terminal = json.loads((run_directory / "events.jsonl").read_text().splitlines()[-1])
    assert terminal["kind"] == "terminal"


@pytest.mark.parametrize("contents", ["{", '{"output": "not an array"}'])
def test_run_reports_invalid_scripted_provider_input_without_traceback(
    tmp_path: Path, contents: str
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    script = tmp_path / "script.json"
    script.write_text(contents)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--provider",
            "scripted",
            "--script",
            str(script),
            "--output-dir",
            str(tmp_path / "runs"),
            "--executor",
            "local",
        ],
    )

    assert result.exit_code == 2
    assert "could not load scripted provider" in result.output
    assert "Traceback" not in result.output
    run_directory = next((tmp_path / "runs").iterdir())
    manifest = json.loads((run_directory / "run.json").read_text())
    assert manifest["status"] == RunStatus.HUMAN_INTERVENTION_REQUIRED.value
    assert "could not load scripted provider" in manifest["reason"]
    assert (run_directory / "report.md").exists()
    terminal = json.loads((run_directory / "events.jsonl").read_text().splitlines()[-1])
    assert terminal["kind"] == "terminal"


def test_run_uses_external_default_evidence_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(target)
    captured: dict[str, Path] = {}

    class FakeStore:
        root = tmp_path / "evidence" / "run-test"
        secrets: list[str] = []

        def write_model(self, _name: str, _model: object) -> Path:
            return self.root / "preflight.json"

        def event_store(self) -> object:
            return type("Store", (), {"emit": lambda _self, _event: None})()

    def fake_create(base_dir: Path, *_args: object, **_kwargs: object) -> FakeStore:
        captured["base_dir"] = base_dir
        return FakeStore()

    class FakeWorkflow:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self) -> object:
            return type(
                "FakeResult",
                (),
                {"run_id": "run-test", "status": RunStatus.COMPLETED},
            )()

    monkeypatch.setattr(run_builder.ArtifactStore, "create", fake_create)
    monkeypatch.setattr(
        run_builder,
        "Preflight",
        lambda *_args: type(
            "Ready",
            (),
            {
                "run": lambda _self, _root: type(
                    "Report",
                    (),
                    {
                        "checks": [],
                        "passed": True,
                        "repository_fingerprint": "repository",
                    },
                )()
            },
        )(),
    )
    def fake_openai_provider(*, model: str) -> object:
        captured["model"] = model
        return object()

    def record_fingerprint(
        provider: str, model: str, executor: str, commands: object
    ) -> str:
        captured["fingerprint"] = (provider, model, executor, commands)
        return "fingerprint"

    monkeypatch.setattr(run_builder, "OpenAIProvider", fake_openai_provider)
    monkeypatch.setattr(run_builder, "configuration_fingerprint", record_fingerprint)
    monkeypatch.setattr(run_builder.RoleSet, "from_provider", lambda _provider: object())
    monkeypatch.setattr(run_builder, "Workflow", FakeWorkflow)

    result = runner.invoke(app, ["run", "--repository", ".", "--request", "change"])

    assert result.exit_code == 0
    assert captured["base_dir"] == target.parent / ".repogent" / "runs"
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["fingerprint"][:3] == ("openai", "gpt-5.6-sol", "docker")


def test_run_fingerprints_scripted_provider_with_scripted_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    script = tmp_path / "script.json"
    script.write_text("[]")
    captured: dict[str, object] = {}

    class PassingPreflight:
        def run(self, _repository: Path) -> PreflightReport:
            return PreflightReport(
                checks=[], git_commit=None, dirty=False, repository_fingerprint="repository"
            )

    class FakeWorkflow:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self) -> object:
            return type(
                "Result", (), {"run_id": "run-test", "status": RunStatus.COMPLETED}
            )()

    def record_fingerprint(
        provider: str, model: str, executor: str, commands: object
    ) -> str:
        captured["fingerprint"] = (provider, model, executor, commands)
        return "fingerprint"

    monkeypatch.setattr(run_builder, "Preflight", lambda *_args: PassingPreflight())
    monkeypatch.setattr(run_builder, "configuration_fingerprint", record_fingerprint)
    monkeypatch.setattr(
        run_builder, "RoleSet", type("Roles", (), {"from_provider": lambda _: object()})
    )
    monkeypatch.setattr(run_builder, "Workflow", FakeWorkflow)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--provider",
            "scripted",
            "--script",
            str(script),
            "--executor",
            "local",
        ],
    )

    assert result.exit_code == 0
    assert captured["fingerprint"][:3] == ("scripted", "scripted", "local")


def test_run_stores_failed_preflight_before_constructing_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    evidence = tmp_path / "runs"

    def provider_must_not_be_constructed(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("provider must not be constructed after failed preflight")

    monkeypatch.setattr(run_builder, "OpenAIProvider", provider_must_not_be_constructed)
    monkeypatch.setattr(
        run_builder,
        "DockerExecutor",
        lambda: type(
            "Unavailable",
            (),
            {
                "readiness": lambda _self: (False, "image unavailable"),
                "available": lambda _self, _command: False,
            },
        )(),
    )

    result = runner.invoke(
        app,
        ["run", "--repository", str(target), "--request", "change", "--output-dir", str(evidence)],
    )

    run_directory = next(evidence.iterdir())
    assert result.exit_code == 2
    assert "executor: image unavailable" in result.output
    assert (run_directory / "preflight-001.json").exists()
    assert (run_directory / "run.json").exists()
    assert (run_directory / "report.md").exists()
    assert (run_directory / "events.jsonl").exists()


def test_run_blocks_required_missing_pytest_before_provider_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    nested = target / "quality" / "regression" / "value_test.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("def test_value(): pass\n")
    evidence = tmp_path / "runs"

    class MissingPytestExecutor:
        def readiness(self) -> tuple[bool, str | None]:
            return True, None

        def available(self, command: object) -> bool:
            return getattr(command, "name", "") != "pytest"

    def provider_must_not_be_constructed(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("provider construction must be blocked by preflight")

    monkeypatch.setattr(
        run_builder, "LocalExecutor", lambda **_kwargs: MissingPytestExecutor()
    )
    monkeypatch.setattr(run_builder, "OpenAIProvider", provider_must_not_be_constructed)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--executor",
            "local",
            "--output-dir",
            str(evidence),
        ],
    )

    assert result.exit_code == 2
    assert "command:pytest: required validation command unavailable" in result.output
    manifest = json.loads((next(evidence.iterdir()) / "run.json").read_text())
    assert manifest["reason"] == "repository preflight failed"


def test_run_terminalizes_keyboard_interrupt_after_store_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    evidence = tmp_path / "runs"

    def interrupt_policy(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(run_builder.ValidationPolicy, "commands", interrupt_policy)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--executor",
            "local",
            "--output-dir",
            str(evidence),
        ],
    )

    assert result.exit_code == 2
    run_directory = next(evidence.iterdir())
    manifest = json.loads((run_directory / "run.json").read_text())
    assert manifest["status"] == RunStatus.CANCELLED.value
    assert manifest["reason"] == "workflow interrupted by user"
    assert (run_directory / "report.md").exists()
    assert json.loads((run_directory / "events.jsonl").read_text())["kind"] == "terminal"


def test_run_terminalizes_unexpected_openai_initialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    evidence = tmp_path / "runs"

    def fail_provider(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("provider configuration invalid")

    monkeypatch.setattr(run_builder, "OpenAIProvider", fail_provider)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--executor",
            "local",
            "--output-dir",
            str(evidence),
        ],
    )

    assert result.exit_code == 2
    manifest = json.loads((next(evidence.iterdir()) / "run.json").read_text())
    assert manifest["status"] == RunStatus.HUMAN_INTERVENTION_REQUIRED.value
    assert "provider configuration invalid" in manifest["reason"]


@pytest.mark.parametrize(
    "message",
    [
        "workflow construction failed",
        "repository preflight failed",
        "repository preflight failed: collision",
        "could not load collision",
    ],
)
def test_run_reports_workflow_construction_failure_like_pre_refactor_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    evidence = tmp_path / "runs"

    class PassingPreflight:
        def run(self, _repository: Path) -> PreflightReport:
            return PreflightReport(
                checks=[], git_commit=None, dirty=False, repository_fingerprint="repository"
            )

    def fail_workflow_construction(**_kwargs: object) -> object:
        raise RuntimeError(message)

    monkeypatch.setattr(run_builder, "Preflight", lambda *_args: PassingPreflight())
    monkeypatch.setattr(run_builder, "OpenAIProvider", lambda **_kwargs: object())
    monkeypatch.setattr(run_builder, "Workflow", fail_workflow_construction)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--executor",
            "local",
            "--output-dir",
            str(evidence),
        ],
    )

    run_directory = next(evidence.iterdir())
    assert result.exit_code == 2
    assert result.output == (
        f"Run {run_directory.name}: human_intervention_required\n"
        f"Evidence: {run_directory}\n"
    )
    manifest = json.loads((run_directory / "run.json").read_text())
    assert manifest["reason"] == message


def test_run_rejects_explicit_default_path_inside_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(target)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            ".",
            "--request",
            "change",
            "--output-dir",
            ".repogent/runs",
        ],
    )

    assert result.exit_code == 2
    assert "outside target" in result.output


def test_run_reports_openai_provider_load_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    def fail_to_load_openai_provider(*_args: object, **_kwargs: object) -> object:
        raise OpenAIError("missing credentials")

    monkeypatch.setattr(run_builder, "OpenAIProvider", fail_to_load_openai_provider)

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--executor",
            "local",
            "--output-dir",
            str(tmp_path / "runs"),
        ],
    )

    assert result.exit_code == 2
    assert "could not load OpenAI provider" in result.output
    assert "Traceback" not in result.output
    run_directory = next((tmp_path / "runs").iterdir())
    assert (run_directory / "run.json").exists()
    assert (run_directory / "report.md").exists()
    assert (run_directory / "events.jsonl").exists()


def test_run_reports_file_output_directory_error_without_traceback(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    script = tmp_path / "script.json"
    script.write_text("[]")
    output_file = tmp_path / "output-file"
    output_file.write_text("not a directory")

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "change",
            "--provider",
            "scripted",
            "--script",
            str(script),
            "--output-dir",
            str(output_file),
        ],
    )

    assert result.exit_code == 2
    assert "could not create evidence directory" in result.output
    assert "Traceback" not in result.output


def test_run_rejects_filesystem_root_before_creating_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "script.json"
    script.write_text("[]")

    def artifact_creation_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("artifact creation must not run for filesystem root")

    monkeypatch.setattr(
        run_builder.ArtifactStore, "create", artifact_creation_must_not_run
    )

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            "/",
            "--request",
            "change",
            "--provider",
            "scripted",
            "--script",
            str(script),
        ],
    )

    assert result.exit_code == 2
    assert "filesystem root" in result.output
    assert "Traceback" not in result.output


def test_documented_scripted_demo_completes(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(Path("examples/fastapi_demo"), target)
    evidence = tmp_path / "runs"

    result = runner.invoke(
        app,
        [
            "run",
            "--repository",
            str(target),
            "--request",
            "Add a health endpoint",
            "--provider",
            "scripted",
            "--script",
            "examples/scripted_run.json",
            "--executor",
            "local",
            "--output-dir",
            str(evidence),
        ],
        input="y\ny\ny\n",
    )

    assert result.exit_code == 0
    assert "completed" in result.output
    assert '@app.get("/health")' in (target / "app.py").read_text()
    assert "[stage] workflow stage changed (patch_previewed)" in result.output
    assert "[stage] workflow stage changed (executor_selected)" in result.output
    assert "[stage] workflow stage changed (validating)" in result.output
    assert "[stage] workflow stage changed (validated)" in result.output
    assert "[terminal] workflow finished (completed)" in result.output
    run_directory = next(evidence.iterdir())
    report = (run_directory / "report.md").read_text()
    assert "Verification: REDUCED ISOLATION" in report
    assert "Execution mode: local" in report


def test_mcp_stdio_dispatches_to_lazy_server_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []
    fake_module = ModuleType("repogent.mcp_server")
    fake_module.serve_stdio = lambda: calls.append(None)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "repogent.mcp_server", fake_module)

    result = runner.invoke(app, ["mcp", "--stdio"])

    assert result.exit_code == 0
    assert calls == [None]


def test_mcp_without_stdio_exits_two() -> None:
    result = runner.invoke(app, ["mcp"])

    assert result.exit_code == 2
    assert "only --stdio is supported" in result.output


def test_cli_import_does_not_eagerly_load_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "repogent.mcp_server", raising=False)

    importlib.reload(cli)

    assert "repogent.mcp_server" not in sys.modules
