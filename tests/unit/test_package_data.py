import tomllib
from importlib.resources import files
from pathlib import Path

import repogent


def test_package_declares_inline_type_information() -> None:
    assert files(repogent).joinpath("py.typed").is_file()  # noqa: S101


def test_project_declares_mit_license_metadata() -> None:
    project_root = Path(__file__).parents[2]
    metadata = tomllib.loads((project_root / "pyproject.toml").read_text())
    license_text = (project_root / "LICENSE").read_text()

    assert metadata["project"]["license"] == "MIT"
    assert metadata["project"]["license-files"] == ["LICENSE"]
    assert "MIT License" in license_text
    assert "Copyright (c) 2026 Repogent contributors" in license_text
