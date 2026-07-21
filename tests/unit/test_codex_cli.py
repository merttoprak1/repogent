from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from repogent.codex_cli import CodexCliProvider
from repogent.domain import ProviderCallStatus, RequirementsSpec


@pytest.fixture
def fake_codex(tmp_path: Path) -> tuple[Path, Path]:
    capture_path = tmp_path / "capture.json"
    calls_path = tmp_path / "calls.jsonl"
    executable = tmp_path / "codex"
    executable.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys

capture_path = pathlib.Path({str(capture_path)!r})
calls_path = pathlib.Path({str(calls_path)!r})
args = sys.argv[1:]
with calls_path.open("a", encoding="utf-8") as calls:
    calls.write(json.dumps(args) + "\\n")

if args == ["--version"]:
    print("codex-cli 1.2.3")
elif args == ["exec", "--help"]:
    print("--ephemeral --sandbox --ignore-user-config --ignore-rules")
    print("--output-schema --output-last-message -C --model")
elif args == ["login", "status"]:
    print("Logged in")
elif args and args[0] == "exec":
    schema_path = pathlib.Path(args[args.index("--output-schema") + 1])
    result_path = pathlib.Path(args[args.index("--output-last-message") + 1])
    workdir = pathlib.Path(args[args.index("-C") + 1])
    prompt = sys.stdin.read()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    capture_path.write_text(
        json.dumps(
            {{
                "argv": args,
                "cwd": os.getcwd(),
                "workdir": str(workdir),
                "prompt": prompt,
                "schema": schema,
                "environment": dict(os.environ),
                "calls": [json.loads(line) for line in calls_path.read_text().splitlines()],
            }},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    result_path.write_text(
        json.dumps(
            {{
                "objective": "Change the project",
                "functional_requirements": ["Make the requested change"],
                "acceptance_criteria": ["The change is verified"],
            }}
        ),
        encoding="utf-8",
    )
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable, capture_path


def test_generate_uses_isolated_structured_exec(
    fake_codex: tuple[Path, Path],
) -> None:
    executable, capture_path = fake_codex
    target_root = Path.cwd().resolve()
    provider = CodexCliProvider(executable=str(executable))

    readiness = provider.check_ready()
    result = provider.generate(
        role="requirements",
        system_prompt="bounded role",
        payload={"request": "change", "repository_context": []},
        output_type=RequirementsSpec,
        timeout_seconds=5,
    )

    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    argv = capture["argv"]
    assert readiness.ready is True
    assert readiness.backend_version == "codex-cli 1.2.3"
    assert result.output.objective == "Change the project"
    assert result.usage.model == "default"
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.evidence is not None
    assert result.evidence.model == "default"
    assert result.evidence.role == "requirements"
    assert result.evidence.status == ProviderCallStatus.COMPLETED
    assert result.evidence.backend_version == "codex-cli 1.2.3"
    assert result.evidence.structured_output_valid is True
    assert capture["calls"][:3] == [
        ["--version"],
        ["exec", "--help"],
        ["login", "status"],
    ]
    assert len(capture["calls"]) == 4
    assert argv[:7] == [
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-schema",
    ]
    assert argv[-1] == "-"
    assert "--model" not in argv
    assert Path(capture["cwd"]).resolve() == Path(capture["workdir"]).resolve()
    assert Path(capture["workdir"]) != target_root
    assert not Path(capture["workdir"]).exists()
    assert str(target_root) not in json.dumps(capture, sort_keys=True)
    assert {
        "PWD",
        "OLDPWD",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "OPENAI_API_KEY",
    }.isdisjoint(capture["environment"])
    assert json.loads(capture["prompt"]) == {
        "payload": {"repository_context": [], "request": "change"},
        "system_prompt": "bounded role",
    }
    assert capture["schema"]["title"] == "RequirementsSpec"


def test_generate_adds_explicit_model_once(fake_codex: tuple[Path, Path]) -> None:
    executable, capture_path = fake_codex
    provider = CodexCliProvider(executable=str(executable), model="gpt-5.6-sol")

    result = provider.generate(
        role="requirements",
        system_prompt="bounded role",
        payload={"request": "change", "repository_context": []},
        output_type=RequirementsSpec,
        timeout_seconds=5,
    )

    argv = json.loads(capture_path.read_text(encoding="utf-8"))["argv"]
    model_index = argv.index("--model")
    assert argv[model_index : model_index + 2] == ["--model", "gpt-5.6-sol"]
    assert argv.count("--model") == 1
    assert result.usage.model == "gpt-5.6-sol"
    assert result.evidence is not None
    assert result.evidence.model == "gpt-5.6-sol"


def test_generate_removes_target_root_from_prompt_and_environment(
    fake_codex: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable, capture_path = fake_codex
    target_root = Path.cwd().resolve()
    safe_home = tmp_path / "home"
    safe_home.mkdir()
    safe_path_entries = ["/usr/bin", "/bin"]
    monkeypatch.setenv("HOME", str(safe_home))
    monkeypatch.setenv("CODEX_HOME", str(target_root))
    monkeypatch.setenv(
        "PATH",
        ":".join(
            [
                safe_path_entries[0],
                str(target_root),
                str(target_root / "bin"),
                safe_path_entries[1],
            ]
        ),
    )
    provider = CodexCliProvider(executable=str(executable))

    provider.generate(
        role="requirements",
        system_prompt=f"Do not inspect {target_root} or its contents",
        payload={
            "request": f"Change {target_root / 'src' / 'repogent'}",
            "repository_context": [{"path": "src/repogent/codex_cli.py"}],
        },
        output_type=RequirementsSpec,
        timeout_seconds=5,
    )

    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    serialized_capture = json.dumps(capture, sort_keys=True)
    prompt = json.loads(capture["prompt"])
    assert str(target_root) not in serialized_capture
    assert capture["environment"]["HOME"] == str(safe_home)
    assert "CODEX_HOME" not in capture["environment"]
    assert capture["environment"]["PATH"].split(":") == safe_path_entries
    assert prompt["payload"]["repository_context"] == [
        {"path": "src/repogent/codex_cli.py"}
    ]
