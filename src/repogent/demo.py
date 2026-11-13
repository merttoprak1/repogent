from __future__ import annotations

import shutil
from importlib.util import find_spec
from pathlib import Path


class DemoError(RuntimeError):
    pass


def require_demo_dependencies() -> None:
    missing = [module for module in ("fastapi", "httpx", "pytest") if find_spec(module) is None]
    if missing:
        raise DemoError(
            f"Missing demo dependencies: {', '.join(missing)}. "
            "Install repogent[demo] in this environment."
        )


def bundled_fastapi_demo() -> Path:
    packaged = Path(__file__).resolve().parent / "_demo" / "fastapi_demo"
    if (packaged / "app.py").is_file():
        return packaged
    repository = Path(__file__).resolve().parents[2] / "examples" / "fastapi_demo"
    if (repository / "app.py").is_file():
        return repository
    raise DemoError("bundled FastAPI demo fixture is unavailable")


def bundled_scripted_run() -> Path:
    packaged = Path(__file__).resolve().parent / "_demo" / "scripted_run.json"
    if packaged.is_file():
        return packaged
    script = Path(__file__).resolve().parents[2] / "examples" / "scripted_run.json"
    if script.is_file():
        return script
    raise DemoError("bundled scripted replay is unavailable")


def copy_demo_repository(destination: Path) -> Path:
    source = bundled_fastapi_demo()
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return destination
