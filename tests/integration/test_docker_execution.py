import shutil
import subprocess
from pathlib import Path

import pytest

from repogent.domain import CheckStatus
from repogent.execution import CommandSpec, DockerExecutor, ValidationPolicy


def docker_image_exists() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    result = subprocess.run(  # noqa: S603  # nosec B603
        [docker, "image", "inspect", "repogent-validator:py311"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.docker
@pytest.mark.skipif(not docker_image_exists(), reason="validator image unavailable")
def test_docker_executor_has_no_network_and_runs_in_workspace(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    result = DockerExecutor().run(
        CommandSpec(name="pytest", argv=("python", "-m", "pytest", "-q"), required=True),
        tmp_path,
    )
    assert result.status is CheckStatus.PASSED


@pytest.mark.docker
@pytest.mark.skipif(not docker_image_exists(), reason="validator image unavailable")
@pytest.mark.parametrize(
    ("tool", "config", "source", "expected", "diagnostic"),
    [
        ("ruff", "", "value = 1\n", CheckStatus.PASSED, "All checks passed!"),
        (
            "ruff",
            '[tool.ruff]\ncache-dir = ".blocked-cache"\n',
            "value = 1\n",
            CheckStatus.PASSED,
            "All checks passed!",
        ),
        (
            "ruff",
            "",
            "import os\n",
            CheckStatus.FAILED,
            "F401",
        ),
        ("mypy", "", "value: int = 1\n", CheckStatus.PASSED, "Success:"),
        (
            "mypy",
            '[tool.mypy]\ncache_dir = ".blocked-cache"\n',
            "value: int = 1\n",
            CheckStatus.PASSED,
            "Success:",
        ),
        (
            "mypy",
            "",
            'value: int = "wrong"\n',
            CheckStatus.FAILED,
            "[assignment]",
        ),
    ],
)
def test_optional_tools_run_on_read_only_checkout(
    tmp_path: Path,
    tool: str,
    config: str,
    source: str,
    expected: CheckStatus,
    diagnostic: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(config)
    (tmp_path / "example.py").write_text(source)
    command = next(item for item in ValidationPolicy().commands(tmp_path) if item.name == tool)

    result = DockerExecutor().run(command, tmp_path)

    assert result.status is expected, result.stderr
    assert result.exit_code == (0 if expected is CheckStatus.PASSED else 1)
    assert diagnostic in result.stdout
    assert "Read-only file system" not in result.stderr
    assert sorted(path.name for path in tmp_path.iterdir()) == ["example.py", "pyproject.toml"]
