from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from enum import StrEnum
from pathlib import Path

from repogent.domain import VersionedModel


class RepositoryScopeError(RuntimeError):
    pass


class ScopeSource(StrEnum):
    GIT = "git"
    FILESYSTEM = "filesystem"


class RepositoryScope(VersionedModel):
    root: Path
    source: ScopeSource
    paths: tuple[Path, ...]
    skipped: tuple[str, ...] = ()


class RepositoryScopeResolver:
    def __init__(
        self,
        *,
        max_paths: int = 50_000,
        max_output_bytes: int = 4_000_000,
        timeout_seconds: float = 10.0,
    ) -> None:
        if max_paths <= 0 or max_output_bytes <= 0 or timeout_seconds <= 0:
            raise ValueError("repository scope limits must be positive")
        self.max_paths = max_paths
        self.max_output_bytes = max_output_bytes
        self.timeout_seconds = timeout_seconds

    def resolve(self, root: Path) -> RepositoryScope:
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise RepositoryScopeError("repository root must be a directory")
        try:
            is_git_repository = _git_repository(resolved, self.timeout_seconds)
        except (OSError, subprocess.SubprocessError) as error:
            raise RepositoryScopeError("Git repository detection failed") from error
        if not is_git_repository:
            return RepositoryScope(
                root=resolved,
                source=ScopeSource.FILESYSTEM,
                paths=(),
            )
        try:
            output = _list_git_paths(
                resolved,
                self.timeout_seconds,
                self.max_output_bytes,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RepositoryScopeError("Git repository listing failed") from error
        if len(output) > self.max_output_bytes:
            raise RepositoryScopeError("Git repository output bytes limit exceeded")
        if output and not output.endswith(b"\0"):
            raise RepositoryScopeError("Git repository listing was not NUL terminated")
        raw_paths = [value for value in output.split(b"\0") if value]
        if len(raw_paths) > self.max_paths:
            raise RepositoryScopeError("Git repository path count limit exceeded")
        paths = tuple(sorted(_validated_relative_path(value) for value in raw_paths))
        if len(set(paths)) != len(paths):
            raise RepositoryScopeError("Git repository listing contains duplicate paths")
        return RepositoryScope(
            root=resolved,
            source=ScopeSource.GIT,
            paths=paths,
        )


def _git_repository(root: Path, timeout_seconds: float) -> bool:
    executable = _git_executable()
    result = subprocess.run(  # noqa: S603
        (executable, "-C", str(root), "rev-parse", "--is-inside-work-tree"),
        check=False,
        capture_output=True,
        timeout=timeout_seconds,
    )
    if result.returncode == 0:
        if result.stdout.strip() != b"true":
            raise RepositoryScopeError("Git worktree detection returned an invalid result")
        return True
    if b"not a git repository" in result.stderr.lower():
        return False
    raise RepositoryScopeError("Git repository detection failed")


def _list_git_paths(
    root: Path,
    timeout_seconds: float,
    max_output_bytes: int,
) -> bytes:
    executable = _git_executable()
    with tempfile.TemporaryFile() as output:
        subprocess.run(  # noqa: S603
            (
                executable,
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ),
            check=True,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
        output.seek(0)
        content = output.read(max_output_bytes + 1)
    if len(content) > max_output_bytes:
        raise RepositoryScopeError("Git repository output bytes limit exceeded")
    return content


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise RepositoryScopeError("Git executable is unavailable")
    return executable


def _validated_relative_path(value: bytes) -> Path:
    path = Path(os.fsdecode(value))
    if path.is_absolute():
        raise RepositoryScopeError("Git repository paths must be relative")
    depth = 0
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                raise RepositoryScopeError("Git repository path may not escape the root")
        else:
            depth += 1
    if ".." in path.parts:
        raise RepositoryScopeError("Git repository path may not escape the root")
    if len(os.fsencode(path)) > 4_096:
        raise RepositoryScopeError("Git repository path length limit exceeded")
    return path
