import subprocess
from pathlib import Path

import pytest

from repogent.repository_scope import (
    RepositoryScopeError,
    RepositoryScopeResolver,
    ScopeSource,
)


def _run_git(repository: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603
        ("git", "-C", str(repository), *arguments),  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    _run_git(repository, "init", "--quiet")
    _run_git(repository, "config", "user.email", "tests@example.com")
    _run_git(repository, "config", "user.name", "Repogent Tests")
    return repository


def test_git_scope_selects_tracked_and_nonignored_untracked(tmp_path: Path) -> None:
    repository = _init_git_repository(tmp_path)
    (repository / "tracked.py").write_text("VALUE = 1\n")
    _run_git(repository, "add", "tracked.py")
    _run_git(repository, "commit", "--quiet", "-m", "initial")
    (repository / "new.py").write_text("VALUE = 2\n")
    (repository / ".gitignore").write_text(".worktrees/\n.superpowers/\n")
    (repository / ".worktrees").mkdir()
    (repository / ".worktrees" / "large.bin").write_bytes(b"x" * 128)

    scope = RepositoryScopeResolver(max_output_bytes=8_192).resolve(repository)

    assert scope.source is ScopeSource.GIT
    assert scope.paths == (Path(".gitignore"), Path("new.py"), Path("tracked.py"))


def test_non_git_directory_uses_filesystem_scope(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n")

    scope = RepositoryScopeResolver().resolve(tmp_path)

    assert scope.source is ScopeSource.FILESYSTEM
    assert scope.paths == ()


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("/absolute.py", "relative"),
        ("../escape.py", "escape"),
        ("nested/../../escape.py", "escape"),
    ],
)
def test_git_scope_rejects_paths_outside_repository(
    tmp_path: Path,
    name: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = subprocess.CompletedProcess(
        args=("git",),
        returncode=0,
        stdout=name.encode() + b"\0",
        stderr=b"",
    )
    monkeypatch.setattr(
        "repogent.repository_scope._git_repository",
        lambda _root, _timeout: True,
    )
    monkeypatch.setattr(
        "repogent.repository_scope._list_git_paths",
        lambda _root, _timeout, _max_output: result.stdout,
    )

    with pytest.raises(RepositoryScopeError, match=message):
        RepositoryScopeResolver().resolve(tmp_path)


def test_git_scope_rejects_output_over_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "repogent.repository_scope._git_repository",
        lambda _root, _timeout: True,
    )
    monkeypatch.setattr(
        "repogent.repository_scope._list_git_paths",
        lambda _root, _timeout, _max_output: b"long-name.py\0",
    )

    with pytest.raises(RepositoryScopeError, match="output bytes"):
        RepositoryScopeResolver(max_output_bytes=4).resolve(tmp_path)


def test_git_scope_rejects_more_than_configured_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "repogent.repository_scope._git_repository",
        lambda _root, _timeout: True,
    )
    monkeypatch.setattr(
        "repogent.repository_scope._list_git_paths",
        lambda _root, _timeout, _max_output: b"a.py\0b.py\0",
    )

    with pytest.raises(RepositoryScopeError, match="path count"):
        RepositoryScopeResolver(max_paths=1).resolve(tmp_path)


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"unterminated.py", "terminated"),
        (b"same.py\0same.py\0", "duplicate"),
    ],
)
def test_git_scope_rejects_malformed_listing(
    tmp_path: Path,
    output: bytes,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "repogent.repository_scope._git_repository",
        lambda _root, _timeout: True,
    )
    monkeypatch.setattr(
        "repogent.repository_scope._list_git_paths",
        lambda _root, _timeout, _max_output: output,
    )

    with pytest.raises(RepositoryScopeError, match=message):
        RepositoryScopeResolver().resolve(tmp_path)


def test_git_scope_fails_closed_when_listing_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "repogent.repository_scope._git_repository",
        lambda _root, _timeout: True,
    )

    def fail_listing(_root: Path, _timeout: float, _max_output: int) -> bytes:
        raise subprocess.CalledProcessError(128, ("git", "ls-files"))

    monkeypatch.setattr("repogent.repository_scope._list_git_paths", fail_listing)

    with pytest.raises(RepositoryScopeError, match="listing failed"):
        RepositoryScopeResolver().resolve(tmp_path)
