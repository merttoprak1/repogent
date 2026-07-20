import json
from pathlib import Path

import pytest
from openai import OpenAIError
from typer.testing import CliRunner

from repogent import cli
from repogent.cli import app
from repogent.domain import RunStatus

runner = CliRunner()


def test_analyze_prints_inventory_and_ranked_context(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "auth.py").write_text("def login():\n    return True\n")
    result = runner.invoke(app, ["analyze", str(target), "--request", "change login"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["inventory"]["files"][0]["path"] == "auth.py"
    assert payload["context"][0]["path"] == "auth.py"


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
            "--output-dir",
            str(tmp_path / "runs"),
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
    assert "provider must be openai or scripted" in result.output
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
        ],
    )

    assert result.exit_code == 2
    assert "could not load scripted provider" in result.output
    assert "Traceback" not in result.output


def test_run_uses_external_default_evidence_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.chdir(target)
    captured: dict[str, Path] = {}

    class FakeStore:
        root = tmp_path / "evidence" / "run-test"

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

    monkeypatch.setattr(cli.ArtifactStore, "create", fake_create)
    monkeypatch.setattr(cli, "OpenAIProvider", lambda *, model: object())
    monkeypatch.setattr(cli.RoleSet, "from_provider", lambda _provider: object())
    monkeypatch.setattr(cli, "Workflow", FakeWorkflow)

    result = runner.invoke(app, ["run", "--repository", ".", "--request", "change"])

    assert result.exit_code == 0
    assert captured["base_dir"] == target.parent / ".repogent" / "runs"


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

    class FakeStore:
        root = tmp_path / "evidence" / "run-test"

    def fail_to_load_openai_provider(*_args: object, **_kwargs: object) -> object:
        raise OpenAIError("missing credentials")

    monkeypatch.setattr(cli.ArtifactStore, "create", lambda *_args: FakeStore())
    monkeypatch.setattr(cli, "OpenAIProvider", fail_to_load_openai_provider)

    result = runner.invoke(
        app,
        ["run", "--repository", str(target), "--request", "change"],
    )

    assert result.exit_code == 2
    assert "could not load OpenAI provider" in result.output
    assert "Traceback" not in result.output
