import shutil
import subprocess
from pathlib import Path

import pytest

from repogent.domain import CheckStatus
from repogent.execution import CommandSpec, DockerExecutor


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
