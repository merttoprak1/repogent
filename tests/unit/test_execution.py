import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from repogent import execution as execution_module
from repogent.domain import (
    CandidateEvidence,
    CheckResult,
    CheckStatus,
    RiskLevel,
    ValidationReport,
)
from repogent.execution import (
    CommandPolicyError,
    CommandSpec,
    DockerExecutor,
    LocalExecutor,
    ValidationPolicy,
)


def test_policy_returns_only_fixed_module_commands(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    commands = ValidationPolicy().commands(tmp_path)
    assert [command.name for command in commands] == ["pytest", "ruff", "mypy", "bandit"]
    assert commands[0].argv == ("python", "-m", "pytest", "-q")
    assert all(
        not any(token in {"sh", "bash", "-c"} for token in command.argv) for command in commands
    )


@pytest.mark.parametrize(
    ("relative_path", "contents"),
    [
        ("quality/regression/test_nested.py", "def test_value(): pass\n"),
        ("checks/unit/value_test.py", "def test_value(): pass\n"),
        ("specs/test_behavior.py", "def test_value(): pass\n"),
    ],
)
def test_policy_requires_pytest_for_bounded_nested_test_discovery(
    tmp_path: Path, relative_path: str, contents: str
) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(contents)

    pytest_command = ValidationPolicy().commands(tmp_path)[0]

    assert pytest_command.required is True


@pytest.mark.parametrize(
    ("config_name", "contents"),
    [
        ("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["specs"]\n'),
        ("pytest.ini", "[pytest]\ntestpaths = specs\n"),
        ("setup.cfg", "[tool:pytest]\ntestpaths = specs\n"),
        ("tox.ini", "[pytest]\ntestpaths = specs\n"),
    ],
)
def test_policy_requires_pytest_when_configuration_declares_testpaths(
    tmp_path: Path, config_name: str, contents: str
) -> None:
    (tmp_path / config_name).write_text(contents)

    assert ValidationPolicy().commands(tmp_path)[0].required is True


def test_policy_discovery_ignores_generated_and_vcs_trees(tmp_path: Path) -> None:
    for relative in (".git/test_hidden.py", ".venv/test_dependency.py", "build/test_output.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_hidden(): pass\n")

    assert ValidationPolicy().commands(tmp_path)[0].required is False


def test_policy_requires_pytest_when_depth_bound_prevents_complete_discovery(
    tmp_path: Path,
) -> None:
    directory = tmp_path
    for index in range(18):
        directory /= f"level_{index}"
        directory.mkdir()

    assert ValidationPolicy().commands(tmp_path)[0].required is True


def test_policy_requires_pytest_when_entry_bound_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("not a test\n")
    monkeypatch.setattr(execution_module, "_DISCOVERY_MAX_ENTRIES", 2)

    assert ValidationPolicy().commands(tmp_path)[0].required is True


@pytest.mark.parametrize(
    "config_name", ["pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"]
)
def test_policy_requires_pytest_when_recognized_config_is_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_name: str
) -> None:
    (tmp_path / config_name).write_text("x" * 20)
    monkeypatch.setattr(execution_module, "_PYTEST_CONFIG_MAX_BYTES", 10)

    assert ValidationPolicy().commands(tmp_path)[0].required is True


def test_policy_requires_pytest_when_recognized_config_is_malformed(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options\n")

    assert ValidationPolicy().commands(tmp_path)[0].required is True


def test_policy_requires_pytest_when_config_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    original_open = execution_module.os.open

    def fail_config_open(path: object, *args: object, **kwargs: object) -> int:
        if Path(str(path)).name == config.name:
            raise PermissionError("config unreadable")
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(execution_module.os, "open", fail_config_open)

    assert ValidationPolicy().commands(tmp_path)[0].required is True


@pytest.mark.parametrize(
    "config_name", ["pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"]
)
def test_policy_requires_pytest_without_following_recognized_config_symlinks(
    tmp_path: Path, config_name: str
) -> None:
    if not Path("/dev/null").exists():
        pytest.skip("requires a POSIX device target")
    (tmp_path / config_name).symlink_to("/dev/null")

    assert ValidationPolicy().commands(tmp_path)[0].required is True


@pytest.mark.parametrize(
    "config_name", ["pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"]
)
def test_policy_requires_pytest_without_opening_recognized_config_fifos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_name: str
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable on this platform")
    config = tmp_path / config_name
    os.mkfifo(config)
    original_read_text = Path.read_text
    read_attempted = False

    def nonblocking_read(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal read_attempted
        if path == config:
            read_attempted = True
            return ""
        return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", nonblocking_read)

    assert ValidationPolicy().commands(tmp_path)[0].required is True
    assert read_attempted is False


def test_policy_requires_pytest_when_config_is_swapped_after_metadata_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path("/dev/null").exists():
        pytest.skip("requires a POSIX device target")
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    original_stat = execution_module.os.stat
    swapped = False

    def swap_after_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal swapped
        metadata = original_stat(path, *args, **kwargs)  # type: ignore[arg-type]
        if not swapped and str(path) in {str(config), config.name}:
            swapped = True
            config.unlink()
            config.symlink_to("/dev/null")
        return metadata

    monkeypatch.setattr(execution_module.os, "stat", swap_after_stat)

    assert ValidationPolicy().commands(tmp_path)[0].required is True
    assert swapped is True


def test_policy_requires_pytest_when_repository_scan_is_inaccessible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied_scandir(_path: Path) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(execution_module.os, "scandir", denied_scandir)

    assert ValidationPolicy().commands(tmp_path)[0].required is True


def test_fail_closed_pytest_result_cannot_become_candidate_eligible(
    tmp_path: Path,
) -> None:
    directory = tmp_path
    for index in range(18):
        directory /= f"level_{index}"
        directory.mkdir()
    pytest_command = ValidationPolicy().commands(tmp_path)[0]
    evidence = CandidateEvidence(
        candidate_id="candidate-1",
        validation=ValidationReport(
            checks=[
                CheckResult(
                    name=pytest_command.name,
                    argv=list(pytest_command.argv),
                    status=CheckStatus.FAILED,
                    required=pytest_command.required,
                )
            ]
        ),
        acceptance_criteria_coverage=1,
        risk_level=RiskLevel.LOW,
        changed_files=1,
        changed_lines=1,
        duration_seconds=0,
        required_failures=["pytest"],
        restored_to_baseline=True,
    )

    assert pytest_command.required is True
    assert evidence.eligible is False


def test_local_executor_runs_allowlisted_command_without_shell(tmp_path: Path) -> None:
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
        timeout_seconds=10,
    )
    executor = LocalExecutor(allowed={"python": command.argv})
    result = executor.run(command, tmp_path)
    assert result.status is CheckStatus.PASSED
    assert result.stdout.strip() == "ok"
    assert result.argv[0] == sys.executable


def test_local_executor_rejects_changed_argv(tmp_path: Path) -> None:
    command = CommandSpec(name="pytest", argv=("python", "-m", "pytest", "--pwn"), required=True)
    with pytest.raises(CommandPolicyError):
        LocalExecutor(allowed={"pytest": ("python", "-m", "pytest", "-q")}).run(command, tmp_path)


def test_local_executor_reports_missing_module_as_unavailable() -> None:
    command = CommandSpec(
        name="optional",
        argv=("python", "-m", "optional"),
        required=False,
        module="repogent_module_that_does_not_exist",
    )
    assert not LocalExecutor(allowed={"optional": command.argv}).available(command)


def test_local_executor_readiness_warns_without_affecting_command_availability() -> None:
    command = CommandSpec("python", ("python", "-c", "print('ok')"), True)
    executor = LocalExecutor(allowed={"python": command.argv})

    assert executor.readiness() == (True, "restricted local execution provides weaker isolation")
    assert executor.available(command) is True


def test_local_executor_returns_timeout_result(tmp_path: Path) -> None:
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "import time; print('before'); time.sleep(2)"),
        required=True,
        timeout_seconds=1,
    )
    result = LocalExecutor(allowed={"python": command.argv}).run(command, tmp_path)
    assert result.status is CheckStatus.TIMED_OUT
    assert result.stdout.strip() == "before"
    assert result.reason == "command timed out"


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
def test_local_timeout_terminates_descendants_that_inherit_output_pipes(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "descendant-survived"
    descendant_code = (
        "import time; from pathlib import Path; "
        f"time.sleep(3); Path({str(marker)!r}).write_text('leaked')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); "
        "print('before', flush=True); time.sleep(30)"
    )
    command = CommandSpec(
        name="python",
        argv=("python", "-c", parent_code),
        required=True,
        timeout_seconds=1,
    )

    started = time.monotonic()
    result = LocalExecutor(allowed={"python": command.argv}).run(command, tmp_path)
    elapsed = time.monotonic() - started
    time.sleep(2.25)

    assert result.status is CheckStatus.TIMED_OUT
    assert elapsed < 2.5
    assert not marker.exists()


@pytest.mark.parametrize("timeout_seconds", [0, -1, 301])
def test_local_executor_rejects_invalid_or_enlarged_timeout(
    tmp_path: Path, timeout_seconds: int
) -> None:
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
        timeout_seconds=timeout_seconds,
    )

    with pytest.raises(CommandPolicyError, match="timeout"):
        LocalExecutor(allowed={"python": command.argv}).run(command, tmp_path)


def test_executors_report_an_unapproved_timeout_as_unavailable(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(
        "repogent.execution.subprocess.run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
    )
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
        timeout_seconds=301,
    )

    assert not LocalExecutor(allowed={"python": command.argv}).available(command)
    assert not DockerExecutor(allowed={"python": command.argv}).available(command)


def test_executor_timeout_ceiling_cannot_be_configured_above_300_seconds() -> None:
    assert "timeout_limits" not in inspect.signature(LocalExecutor).parameters
    assert "timeout_limits" not in inspect.signature(DockerExecutor).parameters


def test_local_executor_boundedly_collects_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Stream:
        def __init__(self, value: bytes) -> None:
            self.value = value
            self.offset = 0
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            assert size > 0
            self.read_sizes.append(size)
            chunk = self.value[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    class Process:
        def __init__(self) -> None:
            self.stdout = Stream(b"a" * 100_000)
            self.stderr = Stream(b"b" * 100_000)
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            pytest.fail("the successful process must not be killed")

    process = Process()
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> Process:
        popen_calls.append((argv, kwargs))
        return process

    monkeypatch.setattr("repogent.execution.subprocess.Popen", fake_popen)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
    )

    result = LocalExecutor(allowed={"python": command.argv}, max_output_chars=7).run(
        command, tmp_path
    )

    assert result.status is CheckStatus.PASSED
    assert result.stdout == "a" * 7
    assert result.stderr == "b" * 7
    assert all(size > 0 for size in process.stdout.read_sizes + process.stderr.read_sizes)
    assert len(popen_calls) == 1
    argv, kwargs = popen_calls[0]
    assert argv == [sys.executable, "-c", "print('ok')"]
    assert kwargs["cwd"] == tmp_path.resolve()
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["text"] is False
    assert "capture_output" not in kwargs


def test_docker_executor_skips_when_docker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: None)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
    )
    executor = DockerExecutor(allowed={"python": command.argv})

    result = executor.run(command, tmp_path)

    assert not executor.available(command)
    assert result.status is CheckStatus.SKIPPED
    assert result.argv == list(command.argv)
    assert result.reason == "docker executable or validator image unavailable"


def test_docker_executor_readiness_reports_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: None)

    assert DockerExecutor().readiness() == (False, "docker executable is unavailable")


def test_docker_executor_skips_missing_image_without_running_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    docker_calls: list[list[str]] = []

    def fake_bounded_run(argv: list[str], **_: object) -> object:
        docker_calls.append(argv)
        return execution_module._ProcessResult(1, "", "", False)

    monkeypatch.setattr("repogent.execution._run_with_bounded_output", fake_bounded_run)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
    )

    result = DockerExecutor(allowed={"python": command.argv}).run(command, tmp_path)

    assert result.status is CheckStatus.SKIPPED
    assert docker_calls == [
        ["/usr/local/bin/docker", "image", "inspect", "repogent-validator:py311"]
    ]


def test_docker_executor_skips_when_image_preflight_times_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(
        "repogent.execution.subprocess.run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
    )

    def fake_bounded_run(*_args: object, **_kwargs: object) -> object:
        return execution_module._ProcessResult(None, "", "", True)

    monkeypatch.setattr("repogent.execution._run_with_bounded_output", fake_bounded_run)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
    )

    result = DockerExecutor(allowed={"python": command.argv}).run(command, tmp_path)

    assert result.status is CheckStatus.SKIPPED
    assert result.reason == "docker image inspection timed out"


def test_docker_image_preflight_is_capped_by_command_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    timeouts: list[int] = []

    def fake_bounded_run(
        _argv: list[str], *, timeout_seconds: int, **_kwargs: object
    ) -> object:
        timeouts.append(timeout_seconds)
        return execution_module._ProcessResult(0, "", "", False)

    monkeypatch.setattr("repogent.execution._run_with_bounded_output", fake_bounded_run)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
        timeout_seconds=2,
    )

    assert DockerExecutor(allowed={"python": command.argv}).available(command) is True
    assert timeouts == [2]


def test_docker_command_availability_probes_module_inside_existing_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    calls: list[list[str]] = []

    def fake_bounded_run(argv: list[str], **_kwargs: object) -> object:
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return execution_module._ProcessResult(0, "", "", False)
        return execution_module._ProcessResult(1, "", "module missing", False)

    monkeypatch.setattr("repogent.execution._run_with_bounded_output", fake_bounded_run)
    command = CommandSpec(
        "pytest", ("python", "-m", "pytest", "-q"), True, module="pytest"
    )
    executor = DockerExecutor(allowed={command.name: command.argv})

    assert executor.readiness() == (True, None)
    assert executor.available(command) is False
    probe = calls[1]
    assert probe[:3] == ["/usr/local/bin/docker", "run", "--rm"]
    assert "--network" in probe and probe[probe.index("--network") + 1] == "none"
    assert "--read-only" in probe
    assert "--mount" not in probe
    assert probe[-1] == "pytest"


def test_docker_command_availability_accepts_present_module_and_caches_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    calls: list[list[str]] = []

    def fake_bounded_run(argv: list[str], **_kwargs: object) -> object:
        calls.append(argv)
        return execution_module._ProcessResult(0, "", "", False)

    monkeypatch.setattr("repogent.execution._run_with_bounded_output", fake_bounded_run)
    command = CommandSpec(
        "pytest", ("python", "-m", "pytest", "-q"), True, module="pytest"
    )
    executor = DockerExecutor(allowed={command.name: command.argv})

    assert executor.available(command) is True
    assert executor.available(command) is True
    assert len(calls) == 1


def test_docker_timeout_force_removes_the_internal_container_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(
        "repogent.execution.subprocess.run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
    )
    calls: list[tuple[list[str], int]] = []

    def fake_bounded_run(
        argv: list[str], *, timeout_seconds: int, **_kwargs: object
    ) -> object:
        calls.append((argv, timeout_seconds))
        if "run" in argv:
            return execution_module._ProcessResult(None, "partial", "", True)
        return execution_module._ProcessResult(0, "", "", False)

    monkeypatch.setattr("repogent.execution._run_with_bounded_output", fake_bounded_run)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
        timeout_seconds=10,
    )

    result = DockerExecutor(allowed={"python": command.argv}).run(command, tmp_path)

    assert result.status is CheckStatus.TIMED_OUT
    assert len(calls) == 3
    inspect_argv, inspect_timeout = calls[0]
    run_argv, run_timeout = calls[1]
    cleanup_argv, cleanup_timeout = calls[2]
    assert inspect_argv == [
        "/usr/local/bin/docker",
        "image",
        "inspect",
        "repogent-validator:py311",
    ]
    container_name = run_argv[run_argv.index("--name") + 1]
    assert container_name.startswith("repogent-validator-")
    assert cleanup_argv == [
        "/usr/local/bin/docker",
        "rm",
        "--force",
        container_name,
    ]
    assert inspect_timeout == 5
    assert run_timeout == 10
    assert cleanup_timeout == 5


def test_docker_executor_never_pulls_an_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Stream:
        def read(self, size: int = -1) -> bytes:
            assert size > 0
            return b""

    class Process:
        stdout = Stream()
        stderr = Stream()
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            pytest.fail("the successful process must not be killed")

    monkeypatch.setattr("repogent.execution.shutil.which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(
        "repogent.execution.subprocess.run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
    )
    docker_run_argv: list[str] = []

    def fake_popen(argv: list[str], **_: object) -> Process:
        docker_run_argv.extend(argv)
        return Process()

    monkeypatch.setattr("repogent.execution.subprocess.Popen", fake_popen)
    command = CommandSpec(
        name="python",
        argv=("python", "-c", "print('ok')"),
        required=True,
    )

    result = DockerExecutor(allowed={"python": command.argv}).run(command, tmp_path)

    assert result.status is CheckStatus.PASSED
    assert "--pull=never" in docker_run_argv
