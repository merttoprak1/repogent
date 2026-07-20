import json
from pathlib import Path

from typer.testing import CliRunner

from repogent.cli import app

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
