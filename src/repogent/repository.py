from __future__ import annotations

import ast
import hashlib
import math
import os
import re
import stat
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from repogent.domain import ContextSnippet, VersionedModel

IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
SENSITIVE_DIRECTORIES = {
    ".aws",
    ".azure",
    ".docker",
    ".gcloud",
    ".gnupg",
    ".kube",
    ".ssh",
}
SENSITIVE_CONFIG_DIRECTORIES = {"aws", "azure", "gcloud", "gh"}
SENSITIVE_FILENAMES = {
    ".envrc",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "application_default_credentials.json",
    "credentials",
    "credentials.json",
    "service-account.json",
    "service_account.json",
}
SENSITIVE_SUFFIXES = {".jks", ".key", ".keystore", ".p12", ".pfx"}
SAFE_ENV_SUFFIXES = {".dist", ".example", ".sample", ".template"}
PRIVATE_KEY_HEADERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")
ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


class RepositoryLimitError(RuntimeError):
    pass


@dataclass
class _InspectionState:
    directory_entries: int = 0
    aggregate_bytes: int = 0


class FileRecord(VersionedModel):
    path: str
    size: int = Field(ge=0)
    sha256: str
    kind: str
    symbols: list[str] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    text: str = ""


class RepositoryInventory(VersionedModel):
    root: str
    files: list[FileRecord]
    skipped: list[str] = Field(default_factory=list)


class RepositoryInspector:
    def __init__(
        self,
        *,
        max_file_bytes: int = 1_000_000,
        max_files: int = 10_000,
        max_total_bytes: int = 50_000_000,
        max_directory_entries: int = 50_000,
        max_depth: int = 20,
        max_elapsed_seconds: float = 30.0,
    ) -> None:
        positive_limits = {
            "max_file_bytes": max_file_bytes,
            "max_files": max_files,
            "max_total_bytes": max_total_bytes,
            "max_directory_entries": max_directory_entries,
            "max_elapsed_seconds": max_elapsed_seconds,
        }
        if any(value <= 0 for value in positive_limits.values()) or max_depth < 0:
            raise ValueError("repository limits must be positive and depth must be non-negative")
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.max_directory_entries = max_directory_entries
        self.max_depth = max_depth
        self.max_elapsed_seconds = max_elapsed_seconds

    def inspect(self, root: Path, *, deadline: float | None = None) -> RepositoryInventory:
        local_deadline = time.monotonic() + self.max_elapsed_seconds
        effective_deadline = (
            min(local_deadline, deadline) if deadline is not None else local_deadline
        )
        self._ensure_deadline(effective_deadline)
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("repository root must be a directory")
        records: list[FileRecord] = []
        skipped: set[str] = set()
        state = _InspectionState()
        try:
            root_fd = os.open(resolved, self._directory_flags)
        except OSError as error:
            raise ValueError("repository root must be a directory") from error
        try:
            self._inspect_directory(
                root_fd, Path(), records, skipped, state, effective_deadline
            )
        finally:
            os.close(root_fd)
        return RepositoryInventory(
            root=str(resolved),
            files=sorted(records, key=lambda item: item.path),
            skipped=sorted(skipped),
        )

    @property
    def _directory_flags(self) -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def _inspect_directory(
        self,
        directory_fd: int,
        relative_directory: Path,
        records: list[FileRecord],
        skipped: set[str],
        state: _InspectionState,
        deadline: float,
    ) -> None:
        self._ensure_deadline(deadline)
        try:
            names: list[str] = []
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    self._ensure_deadline(deadline)
                    state.directory_entries += 1
                    if state.directory_entries > self.max_directory_entries:
                        raise RepositoryLimitError(
                            "repository directory entries limit exceeded"
                        )
                    names.append(entry.name)
        except OSError:
            return
        for name in sorted(names):
            self._ensure_deadline(deadline)
            relative_path = relative_directory / name
            relative = relative_path.as_posix()
            if name in IGNORED_DIRECTORIES or self._is_sensitive_directory(relative_path):
                skipped.add(relative)
                continue
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                skipped.add(relative)
                continue
            if stat.S_ISLNK(metadata.st_mode):
                skipped.add(relative)
            elif stat.S_ISDIR(metadata.st_mode):
                self._inspect_child_directory(
                    directory_fd,
                    name,
                    relative_path,
                    records,
                    skipped,
                    state,
                    deadline,
                )
            elif stat.S_ISREG(metadata.st_mode):
                if self._is_sensitive_file(relative_path):
                    skipped.add(relative)
                    continue
                read_result = self._read_regular_file(directory_fd, name, deadline)
                if read_result is None:
                    skipped.add(relative)
                    continue
                data, size = read_result
                if any(header in data for header in PRIVATE_KEY_HEADERS):
                    skipped.add(relative)
                    continue
                if len(records) >= self.max_files:
                    raise RepositoryLimitError("repository accepted file count limit exceeded")
                if state.aggregate_bytes + size > self.max_total_bytes:
                    raise RepositoryLimitError("repository aggregate bytes limit exceeded")
                state.aggregate_bytes += size
                text = data.decode("utf-8", errors="replace")
                symbols, imports, routes = self._python_metadata(relative_path, text)
                records.append(
                    FileRecord(
                        path=relative,
                        size=size,
                        sha256=hashlib.sha256(data).hexdigest(),
                        kind=self._kind(relative_path),
                        symbols=symbols,
                        imports=imports,
                        routes=routes,
                        text=text,
                    )
                )
            else:
                skipped.add(relative)

    def _inspect_child_directory(
        self,
        directory_fd: int,
        name: str,
        relative_path: Path,
        records: list[FileRecord],
        skipped: set[str],
        state: _InspectionState,
        deadline: float,
    ) -> None:
        if len(relative_path.parts) > self.max_depth:
            raise RepositoryLimitError("repository traversal depth limit exceeded")
        self._ensure_deadline(deadline)
        try:
            child_fd = os.open(name, self._directory_flags, dir_fd=directory_fd)
        except OSError:
            skipped.add(relative_path.as_posix())
            return
        try:
            if stat.S_ISDIR(os.fstat(child_fd).st_mode):
                self._inspect_directory(
                    child_fd, relative_path, records, skipped, state, deadline
                )
            else:
                skipped.add(relative_path.as_posix())
        finally:
            os.close(child_fd)

    def _read_regular_file(
        self, directory_fd: int, name: str, deadline: float
    ) -> tuple[bytes, int] | None:
        self._ensure_deadline(deadline)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            file_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError:
            return None
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > self.max_file_bytes:
                return None
            remaining = metadata.st_size
            chunks: list[bytes] = []
            while remaining:
                self._ensure_deadline(deadline)
                chunk = os.read(file_fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining:
                return None
            return b"".join(chunks), metadata.st_size
        except OSError:
            return None
        finally:
            os.close(file_fd)

    @staticmethod
    def _ensure_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise RepositoryLimitError("repository inspection deadline exceeded")

    @staticmethod
    def _is_sensitive_directory(path: Path) -> bool:
        if path.name.lower() in SENSITIVE_DIRECTORIES:
            return True
        parts = tuple(part.lower() for part in path.parts)
        return (
            len(parts) >= 2
            and parts[-2] == ".config"
            and parts[-1] in SENSITIVE_CONFIG_DIRECTORIES
        )

    @staticmethod
    def _is_sensitive_file(path: Path) -> bool:
        name = path.name.lower()
        if name == ".env" or (
            name.startswith(".env.")
            and not any(name.endswith(suffix) for suffix in SAFE_ENV_SUFFIXES)
        ):
            return True
        if name in SENSITIVE_FILENAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
            return True
        return bool(
            re.fullmatch(r"id_(?:rsa|dsa|ecdsa|ed25519)", name)
        )

    @staticmethod
    def _kind(path: Path) -> str:
        if path.name.startswith("test_") or "tests" in path.parts:
            return "test"
        if path.suffix == ".py":
            return "python"
        if path.name in {"pyproject.toml", "requirements.txt", "Dockerfile"}:
            return "configuration"
        return "text"

    @staticmethod
    def _python_metadata(path: Path, text: str) -> tuple[list[str], list[str], list[str]]:
        if path.suffix != ".py":
            return [], [], []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return [], [], []
        symbols: list[str] = []
        imports: list[str] = []
        routes: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append(node.name)
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call) or not isinstance(
                        decorator.func, ast.Attribute
                    ):
                        continue
                    method = decorator.func.attr.lower()
                    if method not in ROUTE_METHODS or not decorator.args:
                        continue
                    route = decorator.args[0]
                    if isinstance(route, ast.Constant) and isinstance(route.value, str):
                        routes.append(f"{method.upper()} {route.value}")
        return sorted(set(symbols)), sorted(set(imports)), sorted(set(routes))


class LexicalRetriever:
    def retrieve(
        self,
        inventory: RepositoryInventory,
        request: str,
        *,
        limit: int = 8,
    ) -> list[ContextSnippet]:
        if limit < 1:
            raise ValueError("limit must be positive")
        query = self._tokens(request)
        documents = [
            self._tokens(
                " ".join([item.path, *item.symbols, *item.imports, *item.routes, item.text])
            )
            for item in inventory.files
        ]
        if not query or not documents:
            return []
        average_length = sum(len(document) for document in documents) / len(documents)
        document_frequency = Counter(
            token for token in set(query) for document in documents if token in document
        )
        scored: list[tuple[float, FileRecord, list[str]]] = []
        for record, document in zip(inventory.files, documents, strict=True):
            frequencies = Counter(document)
            matches = sorted(set(query) & set(document))
            score = 0.0
            for token in set(query):
                frequency = frequencies[token]
                if not frequency:
                    continue
                inverse = math.log(1 + (len(documents) - document_frequency[token] + 0.5) / (
                    document_frequency[token] + 0.5
                ))
                denominator = frequency + 1.5 * (
                    1 - 0.75 + 0.75 * len(document) / max(average_length, 1)
                )
                score += inverse * (frequency * 2.5) / denominator
            if score > 0:
                scored.append((score, record, matches))
        scored.sort(key=lambda item: (-item[0], item[1].path))
        return [
            ContextSnippet(
                path=record.path,
                start_line=1,
                end_line=max(1, min(len(record.text.splitlines()), 200)),
                text="\n".join(record.text.splitlines()[:200])[:20_000],
                score=score,
                reason=f"matched terms: {', '.join(matches)}",
            )
            for score, record, matches in scored[:limit]
        ]

    @staticmethod
    def _tokens(value: str) -> list[str]:
        return [
            part.lower()
            for token in TOKEN.findall(value)
            for part in token.split("_")
            if part
        ]
