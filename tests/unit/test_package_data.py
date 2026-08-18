import os
import subprocess
import sys
import tomllib
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest

import repogent

PROJECT_ROOT = Path(__file__).parents[2]


def _built_wheel() -> Path:
    if os.environ.get("REPOGENT_PACKAGE_CHECK") != "1":
        pytest.skip("wheel contract runs through make package-check")

    wheels = sorted((PROJECT_ROOT / "dist").glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one built wheel, found {wheels}"
    return wheels[0]


def test_runtime_version_matches_project_metadata() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())

    assert repogent.__version__ == project["project"]["version"]


def test_package_declares_inline_type_information() -> None:
    assert files(repogent).joinpath("py.typed").is_file()  # noqa: S101


def test_project_declares_mit_license_metadata() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    license_text = (PROJECT_ROOT / "LICENSE").read_text()

    assert metadata["project"]["license"] == "MIT"
    assert metadata["project"]["license-files"] == ["LICENSE"]
    assert "MIT License" in license_text
    assert "Copyright (c) 2026 Repogent contributors" in license_text


def test_built_wheel_contains_runtime_and_license_contract() -> None:
    with zipfile.ZipFile(_built_wheel()) as archive:
        names = archive.namelist()

    assert "repogent/mcp_server.py" in names
    assert "repogent/py.typed" in names
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
    assert not any(".superpowers" in name for name in names)


def test_built_wheel_installs_in_isolated_environment(tmp_path: Path) -> None:
    environment = tmp_path / "wheel-smoke"
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "venv", str(environment)],
        check=True,
        cwd=PROJECT_ROOT,
    )
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(  # noqa: S603
        [
            str(interpreter),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            str(_built_wheel()),
        ],
        check=True,
        cwd=PROJECT_ROOT,
    )
    subprocess.run(  # noqa: S603
        [
            str(interpreter),
            "-c",
            (
                "from importlib.resources import files; import repogent; "
                "assert files(repogent).joinpath('py.typed').is_file()"
            ),
        ],
        check=True,
        cwd=PROJECT_ROOT,
    )
