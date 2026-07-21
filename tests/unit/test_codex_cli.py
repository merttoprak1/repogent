from __future__ import annotations

import json
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from repogent.codex_cli import CodexCliProvider
from repogent.domain import ProviderCallStatus, RequirementsSpec
from repogent.providers import ProviderError

_SECRET = "sk-proj-secretvalue123456"  # noqa: S105
_MAX_ERROR_LENGTH = 4096


@pytest.fixture
def fake_codex(tmp_path: Path) -> tuple[Path, Path]:
    capture_path = tmp_path / "capture.json"
    calls_path = tmp_path / "calls.jsonl"
    records_path = tmp_path / "records.jsonl"
    behavior_path = tmp_path / "behavior.json"
    behavior_path.write_text("{}", encoding="utf-8")
    executable = tmp_path / "codex"
    executable.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import pathlib
import stat
import sys
import time

capture_path = pathlib.Path({str(capture_path)!r})
calls_path = pathlib.Path({str(calls_path)!r})
records_path = pathlib.Path({str(records_path)!r})
behavior_path = pathlib.Path({str(behavior_path)!r})
behavior = json.loads(behavior_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
with calls_path.open("a", encoding="utf-8") as calls:
    calls.write(json.dumps(args) + "\\n")
with records_path.open("a", encoding="utf-8") as records:
    records.write(
        json.dumps(
            {{
                "argv": args,
                "cwd": os.getcwd(),
                "stdout_mode": oct(os.fstat(1).st_mode & 0o777),
                "stderr_mode": oct(os.fstat(2).st_mode & 0o777),
                "stdout_regular": stat.S_ISREG(os.fstat(1).st_mode),
                "stderr_regular": stat.S_ISREG(os.fstat(2).st_mode),
            }}
        )
        + "\\n"
    )

if args == ["--version"]:
    time.sleep(behavior.get("version_sleep", 0))
    print(behavior.get("version_stdout", "codex-cli 1.2.3"))
    print(behavior.get("version_stderr", ""), file=sys.stderr)
    raise SystemExit(behavior.get("version_exit", 0))
elif args == ["exec", "--help"]:
    flags = "--ephemeral --sandbox --ignore-user-config --ignore-rules "
    flags += "--output-schema --output-last-message -C --model"
    print(behavior.get("help_stdout", flags))
    print(behavior.get("help_stderr", ""), file=sys.stderr)
    raise SystemExit(behavior.get("help_exit", 0))
elif args == ["login", "status"]:
    print(behavior.get("login_stdout", "Logged in"))
    print(behavior.get("login_stderr", ""), file=sys.stderr)
    raise SystemExit(behavior.get("login_exit", 0))
elif args and args[0] == "exec":
    schema_path = pathlib.Path(args[args.index("--output-schema") + 1])
    result_path = pathlib.Path(args[args.index("--output-last-message") + 1])
    workdir = pathlib.Path(args[args.index("-C") + 1])
    time.sleep(behavior.get("exec_sleep", 0))
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
                "records": [
                    json.loads(line) for line in records_path.read_text().splitlines()
                ],
            }},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    print(behavior.get("exec_stdout", ""))
    print(behavior.get("exec_stderr", ""), file=sys.stderr)
    result_mode = behavior.get("result_mode", "valid")
    if result_mode == "valid":
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
    elif result_mode == "invalid_json":
        result_path.write_text("{{not-json", encoding="utf-8")
    elif result_mode == "schema_mismatch":
        result_path.write_text(json.dumps({{"objective": 123}}), encoding="utf-8")
    elif result_mode == "oversized":
        result_path.write_text("x" * behavior["result_size"], encoding="utf-8")
    elif result_mode == "missing":
        result_path.unlink(missing_ok=True)
    raise SystemExit(behavior.get("exec_exit", 0))
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable, capture_path


def _set_behavior(capture_path: Path, **behavior: Any) -> None:
    capture_path.with_name("behavior.json").write_text(
        json.dumps(behavior), encoding="utf-8"
    )


def _assert_provider_error(
    captured: pytest.ExceptionInfo[ProviderError],
    expected_status: ProviderCallStatus,
    *,
    forbidden: tuple[str, ...] = (_SECRET,),
) -> None:
    error = captured.value
    rendered = str(error)
    assert error.retryable is False
    assert len(rendered) <= _MAX_ERROR_LENGTH
    assert all(value not in rendered for value in forbidden)
    assert error.evidence is not None
    assert error.evidence.status is expected_status
    assert error.evidence.structured_output_valid is False
    assert error.evidence.error == rendered


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
    assert all(
        record["stdout_regular"]
        and record["stderr_regular"]
        and record["stdout_mode"] == "0o600"
        and record["stderr_mode"] == "0o600"
        for record in capture["records"]
    )
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


def test_check_ready_rejects_executable_inside_target_root_without_invoking_it(
    fake_codex: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, capture_path = fake_codex
    calls_path = capture_path.with_name("calls.jsonl")
    monkeypatch.chdir(executable.parent)
    provider = CodexCliProvider(executable=str(executable))

    readiness = provider.check_ready()

    assert readiness.ready is False
    assert readiness.reason == "Codex CLI executable must be outside the target repository"
    assert not calls_path.exists()


def test_generate_ignores_target_root_tempdir_configuration(
    fake_codex: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable, capture_path = fake_codex
    target_root = tmp_path / "target"
    unsafe_temp_parent = target_root / "tmp"
    unsafe_temp_parent.mkdir(parents=True)
    monkeypatch.chdir(target_root)
    monkeypatch.setenv("TMPDIR", str(unsafe_temp_parent))
    monkeypatch.setattr(tempfile, "tempdir", str(unsafe_temp_parent))
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
    assert readiness.ready is True
    assert result.output.objective == "Change the project"
    assert str(target_root.resolve()) not in json.dumps(capture, sort_keys=True)
    assert not Path(capture["workdir"]).is_relative_to(target_root)
    assert not Path(capture["cwd"]).is_relative_to(target_root)
    assert all(
        not Path(record["cwd"]).is_relative_to(target_root)
        for record in capture["records"]
    )
    schema_path = Path(capture["argv"][capture["argv"].index("--output-schema") + 1])
    result_path = Path(
        capture["argv"][capture["argv"].index("--output-last-message") + 1]
    )
    assert not schema_path.is_relative_to(target_root)
    assert not result_path.is_relative_to(target_root)


def _generate(
    provider: CodexCliProvider,
    *,
    timeout_seconds: float | None = 5,
    system_prompt: str = "bounded role",
    payload: dict[str, Any] | None = None,
) -> None:
    provider.generate(
        role="requirements",
        system_prompt=system_prompt,
        payload=payload or {"request": "change", "repository_context": []},
        output_type=RequirementsSpec,
        timeout_seconds=timeout_seconds,
    )


def test_generate_classifies_missing_executable(tmp_path: Path) -> None:
    provider = CodexCliProvider(executable=str(tmp_path / "missing-codex"))

    with pytest.raises(ProviderError) as captured:
        _generate(provider)

    _assert_provider_error(captured, ProviderCallStatus.EXECUTABLE_MISSING)


@pytest.mark.parametrize(
    ("behavior", "expected_status"),
    [
        ({"help_stdout": "--ephemeral"}, ProviderCallStatus.CAPABILITY_MISSING),
        (
            {
                "help_stdout": "--ephemeral --sandbox --ignore-user-config "
                "--ignore-rules --output-schema --output-last-message -C "
                "--model-extra"
            },
            ProviderCallStatus.CAPABILITY_MISSING,
        ),
        (
            {"login_exit": 1, "login_stderr": f"token={_SECRET}"},
            ProviderCallStatus.AUTHENTICATION_FAILED,
        ),
        (
            {"version_exit": 1, "version_stderr": f"token={_SECRET}"},
            ProviderCallStatus.EXECUTION_FAILED,
        ),
    ],
)
def test_generate_classifies_readiness_failures(
    fake_codex: tuple[Path, Path],
    behavior: dict[str, Any],
    expected_status: ProviderCallStatus,
) -> None:
    executable, capture_path = fake_codex
    _set_behavior(capture_path, **behavior)
    provider = CodexCliProvider(executable=str(executable))

    with pytest.raises(ProviderError) as captured:
        _generate(provider)

    _assert_provider_error(captured, expected_status)


def test_check_ready_caches_each_successful_probe(
    fake_codex: tuple[Path, Path],
) -> None:
    executable, capture_path = fake_codex
    calls_path = capture_path.with_name("calls.jsonl")
    _set_behavior(capture_path, login_exit=1)
    provider = CodexCliProvider(executable=str(executable))

    assert provider.check_ready().ready is False
    _set_behavior(capture_path)
    assert provider.check_ready().ready is True

    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert calls == [
        ["--version"],
        ["exec", "--help"],
        ["login", "status"],
        ["login", "status"],
    ]


def test_generate_classifies_readiness_timeout(
    fake_codex: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, capture_path = fake_codex
    _set_behavior(capture_path, version_sleep=1)
    monkeypatch.setattr("repogent.codex_cli.READINESS_TIMEOUT_SECONDS", 0.01)
    provider = CodexCliProvider(executable=str(executable))

    with pytest.raises(ProviderError) as captured:
        _generate(provider)

    _assert_provider_error(captured, ProviderCallStatus.TIMED_OUT)


@pytest.mark.parametrize("model", ["", "x" * 201, "bad\x00model", "bad\nmodel"])
def test_generate_rejects_invalid_explicit_model_before_preflight(
    fake_codex: tuple[Path, Path], model: str
) -> None:
    executable, capture_path = fake_codex
    calls_path = capture_path.with_name("calls.jsonl")
    provider = CodexCliProvider(executable=str(executable), model=model)

    with pytest.raises(ProviderError) as captured:
        _generate(provider)

    _assert_provider_error(captured, ProviderCallStatus.CAPABILITY_MISSING)
    assert captured.value.evidence is not None
    assert captured.value.evidence.model == "invalid"
    assert not calls_path.exists()


def test_generate_rejects_oversized_prompt_before_exec(
    fake_codex: tuple[Path, Path],
) -> None:
    executable, capture_path = fake_codex
    provider = CodexCliProvider(executable=str(executable), max_prompt_bytes=64)

    with pytest.raises(ProviderError) as captured:
        _generate(provider, payload={"request": "x" * 200})

    _assert_provider_error(captured, ProviderCallStatus.OUTPUT_TOO_LARGE)
    calls = [
        json.loads(line)
        for line in capture_path.with_name("calls.jsonl").read_text().splitlines()
    ]
    assert calls == [["--version"], ["exec", "--help"], ["login", "status"]]


def test_generate_classifies_nonzero_exit_and_redacts_bounded_diagnostics(
    fake_codex: tuple[Path, Path],
) -> None:
    executable, capture_path = fake_codex
    target_root = str(Path.cwd().resolve())
    credential_path = str(Path.home() / ".codex" / "auth.json")
    stderr = (
        f"token={_SECRET} root={target_root} credentials={credential_path} "
        + ("failure " * 1000)
    )
    _set_behavior(capture_path, exec_exit=7, exec_stderr=stderr)
    provider = CodexCliProvider(executable=str(executable))

    with pytest.raises(ProviderError) as captured:
        _generate(provider)

    _assert_provider_error(
        captured,
        ProviderCallStatus.EXECUTION_FAILED,
        forbidden=(_SECRET, target_root, credential_path),
    )
    assert "[REDACTED]" in str(captured.value)
    assert captured.value.evidence is not None
    assert captured.value.evidence.exit_code == 7


@pytest.mark.parametrize(
    ("behavior", "max_output_bytes", "expected_status"),
    [
        ({"result_mode": "missing"}, 4096, ProviderCallStatus.INVALID_OUTPUT),
        (
            {"result_mode": "oversized", "result_size": 4097},
            4096,
            ProviderCallStatus.OUTPUT_TOO_LARGE,
        ),
        ({"result_mode": "invalid_json"}, 4096, ProviderCallStatus.INVALID_OUTPUT),
        ({"result_mode": "schema_mismatch"}, 4096, ProviderCallStatus.INVALID_OUTPUT),
    ],
)
def test_generate_classifies_structured_output_failures(
    fake_codex: tuple[Path, Path],
    behavior: dict[str, Any],
    max_output_bytes: int,
    expected_status: ProviderCallStatus,
) -> None:
    executable, capture_path = fake_codex
    _set_behavior(capture_path, **behavior)
    provider = CodexCliProvider(
        executable=str(executable), max_output_bytes=max_output_bytes
    )

    with pytest.raises(ProviderError) as captured:
        _generate(provider)

    _assert_provider_error(captured, expected_status)


def test_generate_rejects_oversized_diagnostics_without_reading_them(
    fake_codex: tuple[Path, Path],
) -> None:
    executable, capture_path = fake_codex
    _set_behavior(capture_path, exec_exit=9, exec_stderr=_SECRET * 300)
    provider = CodexCliProvider(executable=str(executable), max_output_bytes=4096)

    with pytest.raises(ProviderError) as captured:
        _generate(provider)

    _assert_provider_error(captured, ProviderCallStatus.OUTPUT_TOO_LARGE)
    assert "diagnostic" in str(captured.value).lower()


def test_generate_timeout_terminates_child_and_removes_temporary_directory(
    fake_codex: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, capture_path = fake_codex
    provider = CodexCliProvider(executable=str(executable))
    assert provider.check_ready().ready is True
    _set_behavior(capture_path, exec_sleep=60)
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[Any]] = []
    directories: list[Path] = []
    real_temporary_directory = tempfile.TemporaryDirectory

    def track_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def track_directory(*args: Any, **kwargs: Any) -> tempfile.TemporaryDirectory[str]:
        directory = real_temporary_directory(*args, **kwargs)
        directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(subprocess, "Popen", track_popen)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", track_directory)

    with pytest.raises(ProviderError) as captured:
        _generate(provider, timeout_seconds=0.05)

    _assert_provider_error(captured, ProviderCallStatus.TIMED_OUT)
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert directories and all(not directory.exists() for directory in directories)


def test_generate_keyboard_interrupt_terminates_child_cleans_up_and_reraises(
    fake_codex: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, capture_path = fake_codex
    provider = CodexCliProvider(executable=str(executable))
    assert provider.check_ready().ready is True
    _set_behavior(capture_path, exec_sleep=60)
    directories: list[Path] = []
    real_temporary_directory = tempfile.TemporaryDirectory

    class InterruptingProcess:
        def __init__(self) -> None:
            self.args: list[str] = []
            self.returncode: int | None = None
            self.terminated = False
            self.wait_calls = 0

        def __enter__(self) -> InterruptingProcess:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def communicate(self, *args: Any, **kwargs: Any) -> tuple[str, str]:
            raise KeyboardInterrupt

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise KeyboardInterrupt
            self.returncode = -15
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def poll(self) -> int | None:
            return self.returncode

    process = InterruptingProcess()

    def track_directory(*args: Any, **kwargs: Any) -> tempfile.TemporaryDirectory[str]:
        directory = real_temporary_directory(*args, **kwargs)
        directories.append(Path(directory.name))
        return directory

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", track_directory)

    with pytest.raises(KeyboardInterrupt):
        _generate(provider)

    assert process.terminated is True
    assert process.wait_calls >= 2
    assert directories and all(not directory.exists() for directory in directories)
