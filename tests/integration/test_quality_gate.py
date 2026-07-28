import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
MAKE = shutil.which("make")
assert MAKE is not None


def test_make_verify_dry_run_uses_overridden_interpreter() -> None:
    result = subprocess.run(  # noqa: S603
        [MAKE, "-n", "verify", "PYTHON=python3"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "python3 -m pytest" in result.stdout
    assert "python3 -m ruff check ." in result.stdout
    assert "python3 -m ruff format --check ." in result.stdout
    assert "python3 -m mypy src" in result.stdout
    assert "python3 -m bandit -q -r src/repogent" in result.stdout
    assert "python3 -m build" in result.stdout


def test_package_check_executes_all_distribution_contracts() -> None:
    result = subprocess.run(  # noqa: S603
        [MAKE, "-n", "package-check", "PYTHON=python3"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "REPOGENT_PACKAGE_CHECK=1" in result.stdout
    assert "tests/unit/test_package_data.py" in result.stdout
    assert "tests/unit/test_plugin_package.py" in result.stdout
    assert "tests/integration/test_plugin_end_to_end.py" in result.stdout
