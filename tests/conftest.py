from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Keep the developer's real Codex configuration out of the test run.

    Codex CLI readiness resolves directory trust from the Codex configuration.
    Without this fixture, tests would read whatever `~/.codex/config.toml`
    exists on the machine running them, so the same test could pass in CI and
    fail on a contributor's laptop. Every test starts from a CODEX_HOME that
    holds no configuration; tests that exercise trust set their own.
    """
    codex_home = tmp_path_factory.mktemp("codex-home-absent") / "unset"
    return codex_home


@pytest.fixture(autouse=True)
def _apply_isolated_codex_home(isolated_codex_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(isolated_codex_home))
