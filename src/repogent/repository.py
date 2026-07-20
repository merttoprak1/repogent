from __future__ import annotations

import ast
import hashlib
import math
import os
import re
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
        for directory, names, filenames in os.walk(resolved, followlinks=False):
            current = Path(directory)
            retained: list[str] = []
            for name in names:
                path = current / name
                relative = path.relative_to(resolved).as_posix()
                if name in IGNORED_DIRECTORIES or path.is_symlink():
                    skipped.add(relative)
                else:
                    retained.append(name)
            names[:] = retained
            for name in filenames:
                path = current / name
                relative = path.relative_to(resolved).as_posix()
                if path.is_symlink() or not path.is_file():
                    skipped.add(relative)
                    continue
                size = path.stat().st_size
                if size > self.max_file_bytes:
                    skipped.add(relative)
                    continue
                data = path.read_bytes()
                text = data.decode("utf-8", errors="replace")
                symbols, imports, routes = self._python_metadata(path, text)
                records.append(
                    FileRecord(
                        path=relative,
                        size=size,
                        sha256=hashlib.sha256(data).hexdigest(),
                        kind=self._kind(path),
                        symbols=symbols,
                        imports=imports,
                        routes=routes,
                        text=text,
                    )
                )
        return RepositoryInventory(
            root=str(resolved),
            files=sorted(records, key=lambda item: item.path),
            skipped=sorted(skipped),
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
