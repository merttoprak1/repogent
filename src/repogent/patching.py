from __future__ import annotations

import errno
import os
import stat
import subprocess  # nosec B404  # fixed argv; the model supplies only patch content
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from unidiff import PatchSet  # type: ignore[import-untyped]
from unidiff.errors import UnidiffParseError  # type: ignore[import-untyped]

from repogent.domain import PatchProposal


class PatchPolicyError(ValueError):
    pass


class CheckoutRecoveryError(RuntimeError):
    def __init__(self, touched_paths: tuple[Path, ...], restore_error: BaseException) -> None:
        self.touched_paths = touched_paths
        self.restore_error = restore_error
        paths = ", ".join(path.as_posix() for path in touched_paths)
        super().__init__(
            "checkout recovery could not be proved for "
            f"{paths}: {type(restore_error).__name__}: {restore_error}"
        )


class _MissingParentError(Exception):
    pass


@dataclass(frozen=True)
class PatchLimits:
    max_files: int = 20
    max_changed_lines: int = 1_000
    max_bytes: int = 200_000


@dataclass(frozen=True)
class ValidatedPatch:
    proposal: PatchProposal
    touched_paths: tuple[Path, ...]
    changed_lines: int


class PatchPolicy:
    def __init__(self, limits: PatchLimits | None = None) -> None:
        self.limits = limits or PatchLimits()

    def validate(self, root: Path, proposal: PatchProposal) -> ValidatedPatch:
        repository = root.resolve(strict=True)
        if not repository.is_dir():
            raise PatchPolicyError("repository root must be a directory")
        try:
            encoded = proposal.diff.encode("utf-8")
        except UnicodeEncodeError as error:
            raise PatchPolicyError("patch must be valid UTF-8") from error
        if len(encoded) > self.limits.max_bytes:
            raise PatchPolicyError("patch exceeds byte limit")
        if "GIT binary patch" in proposal.diff or "Binary files " in proposal.diff:
            raise PatchPolicyError("binary patches are forbidden")
        if not proposal.diff.startswith("--- "):
            raise PatchPolicyError("malformed unified diff")
        try:
            patch_set = PatchSet(proposal.diff.splitlines(keepends=True))
        except UnidiffParseError as error:
            raise PatchPolicyError(f"malformed unified diff: {error}") from error
        if not patch_set:
            raise PatchPolicyError("patch contains no files")
        if len(patch_set) > self.limits.max_files:
            raise PatchPolicyError("patch exceeds file limit")

        touched: list[Path] = []
        changed_lines = 0
        for patched_file in patch_set:
            if patched_file.patch_info is not None or not patched_file:
                raise PatchPolicyError(
                    "malformed unified diff: patch metadata and empty hunks are forbidden"
                )
            source = self._relative_path(patched_file.source_file, prefix="a/")
            target = self._relative_path(patched_file.target_file, prefix="b/")
            for path in (source, target):
                if path is not None and any(
                    component in {".git", ".repogent"} for component in path.parts
                ):
                    raise PatchPolicyError(f"protected path: {path}")
            if source is not None and target is not None and source != target:
                raise PatchPolicyError("renames are forbidden")
            relative = source if target is None else target
            if relative is None:
                raise PatchPolicyError("unsafe path: both paths are /dev/null")
            self._validate_path(repository, relative)
            touched.append(Path(*relative.parts))
            changed_lines += patched_file.added + patched_file.removed

        if len(set(touched)) != len(touched):
            raise PatchPolicyError("patch modifies a path more than once")
        if changed_lines > self.limits.max_changed_lines:
            raise PatchPolicyError("patch exceeds changed-line limit")
        return ValidatedPatch(
            proposal=proposal,
            touched_paths=tuple(touched),
            changed_lines=changed_lines,
        )

    @staticmethod
    def _relative_path(raw: str, *, prefix: str) -> PurePosixPath | None:
        if raw == "/dev/null":
            return None
        if not raw.startswith(prefix):
            raise PatchPolicyError(f"unsafe path: {raw}")
        value = raw[len(prefix) :]
        components = value.split("/")
        if (
            not value
            or "\\" in value
            or '"' in value
            or any(component in {"", ".", ".."} for component in components)
        ):
            raise PatchPolicyError(f"unsafe path: {raw}")
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise PatchPolicyError(f"unsafe path: {raw}")
        return path

    @staticmethod
    def _validate_path(repository: Path, relative: PurePosixPath) -> None:
        candidate = repository.joinpath(*relative.parts)
        try:
            parent = candidate.parent.resolve(strict=True)
        except FileNotFoundError:
            parent = candidate.parent.resolve()
        if parent != repository and repository not in parent.parents:
            raise PatchPolicyError(f"path resolves outside repository: {relative}")
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise PatchPolicyError(f"symlink target is forbidden: {relative}")
        if not stat.S_ISREG(metadata.st_mode):
            raise PatchPolicyError(f"path is not a regular file: {relative}")


@dataclass(frozen=True)
class Snapshot:
    existed: bool
    content: bytes
    mode: int | None


class PatchApplier:
    def apply(self, root: Path, patch: ValidatedPatch) -> None:
        repository = root.resolve(strict=True)
        if not repository.is_dir():
            raise PatchPolicyError("repository root must be a directory")
        validated = PatchPolicy().validate(repository, patch.proposal)
        snapshots, missing_directories = self.snapshot(repository, validated)
        try:
            self._git_apply(repository, validated.proposal.diff, check=True)
            self._git_apply(repository, validated.proposal.diff, check=False)
        except (Exception, KeyboardInterrupt, SystemExit) as apply_error:
            try:
                self.restore(repository, snapshots, missing_directories)
            except (Exception, KeyboardInterrupt, SystemExit) as restore_error:
                raise CheckoutRecoveryError(
                    validated.touched_paths, restore_error
                ) from apply_error
            raise

    def transaction(self, root: Path, patch: ValidatedPatch) -> PatchTransaction:
        return PatchTransaction(self, root, patch)

    def snapshot(
        self, root: Path, patch: ValidatedPatch
    ) -> tuple[dict[Path, Snapshot], set[Path]]:
        repository = root.resolve(strict=True)
        if not repository.is_dir():
            raise PatchPolicyError("repository root must be a directory")
        validated = PatchPolicy().validate(repository, patch.proposal)
        snapshots = {
            relative: self._snapshot(repository, relative) for relative in validated.touched_paths
        }
        missing_directories = {
            directory
            for relative in validated.touched_paths
            for directory in self._missing_parent_directories(repository, relative)
        }
        return snapshots, missing_directories

    def restore(
        self,
        root: Path,
        snapshots: dict[Path, Snapshot],
        missing_directories: set[Path],
    ) -> None:
        repository = root.resolve(strict=True)
        errors = self._restore(repository, snapshots, missing_directories)
        if errors:
            details = "; ".join(str(error) for error in errors)
            raise RuntimeError(f"patch restoration failed: {details}")

    @staticmethod
    def _git_apply(root: Path, diff: str, *, check: bool) -> None:
        argv = ["git", "apply", "--whitespace=nowarn"]
        if check:
            argv.append("--check")
        result = subprocess.run(  # noqa: S603  # nosec B603
            argv,
            cwd=root,
            input=diff,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"git apply failed: {result.stderr.strip()}")

    @classmethod
    def _snapshot(cls, root: Path, relative: Path) -> Snapshot:
        try:
            parent_fd, name = cls._open_parent(root, relative, create=False)
        except _MissingParentError:
            return Snapshot(existed=False, content=b"", mode=None)
        try:
            try:
                metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return Snapshot(existed=False, content=b"", mode=None)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise PatchPolicyError(f"unsafe touched path: {relative}")
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                current = os.fstat(descriptor)
                if not stat.S_ISREG(current.st_mode):
                    raise PatchPolicyError(f"unsafe touched path: {relative}")
                content = cls._read_all(descriptor, current.st_size)
            finally:
                os.close(descriptor)
            return Snapshot(existed=True, content=content, mode=stat.S_IMODE(current.st_mode))
        finally:
            os.close(parent_fd)

    @classmethod
    def _restore(
        cls,
        root: Path,
        snapshots: dict[Path, Snapshot],
        missing_directories: set[Path],
    ) -> list[Exception]:
        errors: list[Exception] = []
        for relative, snapshot in snapshots.items():
            try:
                cls._restore_one(root, relative, snapshot)
            except Exception as error:
                errors.append(error)
        directories = sorted(
            missing_directories,
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            try:
                cls._remove_empty_directory(root, directory)
            except Exception as error:
                errors.append(error)
        return errors

    @classmethod
    def _restore_one(cls, root: Path, relative: Path, snapshot: Snapshot) -> None:
        try:
            parent_fd, name = cls._open_parent(root, relative, create=snapshot.existed)
        except _MissingParentError:
            return
        try:
            if not snapshot.existed:
                try:
                    os.unlink(name, dir_fd=parent_fd)
                except FileNotFoundError:
                    return
                return
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                stat.S_IMODE(snapshot.mode or 0o600),
                dir_fd=parent_fd,
            )
            try:
                cls._write_all(descriptor, snapshot.content)
                if snapshot.mode is not None:
                    os.fchmod(descriptor, snapshot.mode)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)

    @classmethod
    def _missing_parent_directories(cls, root: Path, relative: Path) -> tuple[Path, ...]:
        descriptor = os.open(root, cls._directory_flags())
        try:
            for index, component in enumerate(relative.parts[:-1]):
                try:
                    child = os.open(component, cls._directory_flags(), dir_fd=descriptor)
                except FileNotFoundError:
                    return tuple(
                        Path(*relative.parts[:depth])
                        for depth in range(index + 1, len(relative.parts))
                    )
                os.close(descriptor)
                descriptor = child
            return ()
        finally:
            os.close(descriptor)

    @classmethod
    def _remove_empty_directory(cls, root: Path, directory: Path) -> None:
        try:
            parent_fd, name = cls._open_parent(root, directory, create=False)
        except _MissingParentError:
            return
        try:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError as error:
                if error.errno in {errno.EEXIST, errno.ENOENT, errno.ENOTEMPTY}:
                    return
                raise
        finally:
            os.close(parent_fd)

    @classmethod
    def _open_parent(cls, root: Path, relative: Path, *, create: bool) -> tuple[int, str]:
        descriptor = os.open(root, cls._directory_flags())
        try:
            for component in relative.parts[:-1]:
                try:
                    child = os.open(component, cls._directory_flags(), dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise _MissingParentError from None
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    child = os.open(component, cls._directory_flags(), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor, relative.name
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    @staticmethod
    def _read_all(descriptor: int, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise OSError("failed to read complete snapshot")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise OSError("failed to restore complete snapshot")
            remaining = remaining[written:]


class PatchTransaction:
    def __init__(self, applier: PatchApplier, root: Path, patch: ValidatedPatch) -> None:
        self.applier = applier
        self.root = root.resolve(strict=True)
        self.patch = patch
        self._snapshots: dict[Path, Snapshot] = {}
        self._missing_directories: set[Path] = set()
        self._committed = False

    def __enter__(self) -> PatchTransaction:
        self._snapshots, self._missing_directories = self.applier.snapshot(self.root, self.patch)
        self.applier.apply(self.root, self.patch)
        return self

    def commit(self) -> None:
        self._committed = True

    def __exit__(self, *_error: object) -> None:
        if not self._committed:
            self.applier.restore(self.root, self._snapshots, self._missing_directories)
