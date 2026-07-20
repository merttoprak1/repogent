from __future__ import annotations

import ast
import hashlib
import math
import os
import re
import stat
from collections import Counter
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
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")
ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


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
    def __init__(self, *, max_file_bytes: int = 1_000_000) -> None:
        self.max_file_bytes = max_file_bytes

    def inspect(self, root: Path) -> RepositoryInventory:
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("repository root must be a directory")
        records: list[FileRecord] = []
        skipped: set[str] = set()
        try:
            root_fd = os.open(resolved, self._directory_flags)
        except OSError as error:
            raise ValueError("repository root must be a directory") from error
        try:
            self._inspect_directory(root_fd, Path(), records, skipped)
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
    ) -> None:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError:
            return
        for name in names:
            relative_path = relative_directory / name
            relative = relative_path.as_posix()
            if name in IGNORED_DIRECTORIES:
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
                    directory_fd, name, relative_path, records, skipped
                )
            elif stat.S_ISREG(metadata.st_mode):
                read_result = self._read_regular_file(directory_fd, name)
                if read_result is None:
                    skipped.add(relative)
                    continue
                data, size = read_result
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
    ) -> None:
        try:
            child_fd = os.open(name, self._directory_flags, dir_fd=directory_fd)
        except OSError:
            skipped.add(relative_path.as_posix())
            return
        try:
            if stat.S_ISDIR(os.fstat(child_fd).st_mode):
                self._inspect_directory(child_fd, relative_path, records, skipped)
            else:
                skipped.add(relative_path.as_posix())
        finally:
            os.close(child_fd)

    def _read_regular_file(self, directory_fd: int, name: str) -> tuple[bytes, int] | None:
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
