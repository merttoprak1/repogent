from __future__ import annotations

import ast
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field

from repogent.domain import VersionedModel
from repogent.repository import RepositoryInventory


class SymbolKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


class SymbolNode(VersionedModel):
    symbol_id: str
    qualified_name: str
    name: str
    kind: SymbolKind
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    parent_id: str | None = None
    decorators: list[str] = Field(default_factory=list)


class SymbolEdge(VersionedModel):
    source: str
    target: str
    kind: Literal["contains", "imports", "calls"]
    alias: str | None = None
    binding: str | None = None
    binding_target: str | None = None
    line: int = Field(default=1, ge=1)
    column: int = Field(default=0, ge=0)


class PythonSymbolGraph(VersionedModel):
    nodes: list[SymbolNode]
    edges: list[SymbolEdge]
    parse_errors: dict[str, str] = Field(default_factory=dict)


class _PythonSymbolVisitor(ast.NodeVisitor):
    def __init__(self, path: str, text: str, source_roots: tuple[str, ...]) -> None:
        self.path = path
        self.module_name = _module_name(path, source_roots)
        self.package_name = _package_name(path, source_roots)
        module_id = f"{path}:{self.module_name}"
        self.nodes = [
            SymbolNode(
                symbol_id=module_id,
                qualified_name=self.module_name,
                name=self.module_name.rsplit(".", maxsplit=1)[-1],
                kind=SymbolKind.MODULE,
                path=path,
                start_line=1,
                end_line=max(1, len(text.splitlines())),
            )
        ]
        self.edges: list[SymbolEdge] = []
        self._qualified_names: list[str] = []
        self._symbol_ids: list[str] = [module_id]
        self._kinds: list[SymbolKind] = []

    @property
    def _source(self) -> str:
        return self._symbol_ids[-1]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_symbol(node, SymbolKind.CLASS)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = self._function_kind()
        self._visit_symbol(node, kind)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        kind = self._function_kind()
        self._visit_symbol(node, kind)

    def visit_Import(self, node: ast.Import) -> None:
        for imported in node.names:
            self.edges.append(
                SymbolEdge(
                    source=self._source,
                    target=imported.name,
                    kind="imports",
                    alias=imported.asname,
                    binding=imported.asname or imported.name.split(".", maxsplit=1)[0],
                    binding_target=(
                        imported.name
                        if imported.asname is not None
                        else imported.name.split(".")[0]
                    ),
                    line=node.lineno,
                    column=node.col_offset,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _imported_module(self.package_name, node.level, node.module)
        for imported in node.names:
            target = f"{module}.{imported.name}" if module else imported.name
            self.edges.append(
                SymbolEdge(
                    source=self._source,
                    target=target,
                    kind="imports",
                    alias=imported.asname,
                    binding=imported.asname or imported.name,
                    binding_target=target,
                    line=node.lineno,
                    column=node.col_offset,
                )
            )

    def visit_Call(self, node: ast.Call) -> None:
        target = _expression_name(node.func)
        if target is not None:
            self.edges.append(
                SymbolEdge(
                    source=self._source,
                    target=target,
                    kind="calls",
                    line=node.lineno,
                    column=node.col_offset,
                )
            )
        self.generic_visit(node)

    def _visit_symbol(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        kind: SymbolKind,
    ) -> None:
        qualified_name = ".".join((self.module_name, *self._qualified_names, node.name))
        parent_id = self._symbol_ids[-1]
        symbol_id = f"{self.path}:{qualified_name}"
        self.nodes.append(
            SymbolNode(
                symbol_id=symbol_id,
                qualified_name=qualified_name,
                name=node.name,
                kind=kind,
                path=self.path,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                parent_id=parent_id,
                decorators=[
                    _expression_name(decorator) or ast.unparse(decorator)
                    for decorator in node.decorator_list
                ],
            )
        )
        self.edges.append(
            SymbolEdge(
                source=parent_id,
                target=symbol_id,
                kind="contains",
                line=node.lineno,
                column=node.col_offset,
            )
        )
        self._qualified_names.append(node.name)
        self._symbol_ids.append(symbol_id)
        self._kinds.append(kind)
        self.generic_visit(node)
        self._kinds.pop()
        self._symbol_ids.pop()
        self._qualified_names.pop()

    def _function_kind(self) -> SymbolKind:
        if self._kinds and self._kinds[-1] is SymbolKind.CLASS:
            return SymbolKind.METHOD
        return SymbolKind.FUNCTION


class PythonSymbolGraphBuilder:
    def build(self, inventory: RepositoryInventory) -> PythonSymbolGraph:
        nodes: list[SymbolNode] = []
        edges: list[SymbolEdge] = []
        parse_errors: dict[str, str] = {}
        source_roots = _source_roots(inventory)
        for record in inventory.files:
            if not record.path.endswith(".py"):
                continue
            try:
                tree = ast.parse(record.text, filename=record.path)
            except SyntaxError as error:
                parse_errors[record.path] = error.msg
                continue
            visitor = _PythonSymbolVisitor(record.path, record.text, source_roots)
            visitor.visit(tree)
            nodes.extend(visitor.nodes)
            edges.extend(visitor.edges)
        return PythonSymbolGraph(
            nodes=sorted(nodes, key=lambda node: (node.path, node.start_line, node.qualified_name)),
            edges=sorted(
                edges,
                key=lambda edge: (
                    edge.source,
                    edge.line,
                    edge.column,
                    edge.kind,
                    edge.target,
                    edge.alias or "",
                    edge.binding or "",
                    edge.binding_target or "",
                ),
            ),
            parse_errors=dict(sorted(parse_errors.items())),
        )


def _expression_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expression_name(node.value)
        return f"{parent}.{node.attr}" if parent is not None else None
    return None


def _module_name(path: str, source_roots: tuple[str, ...] = ()) -> str:
    parts = list(Path(_strip_source_root(path, source_roots)).with_suffix("").parts)
    if parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    return ".".join(parts)


def _package_name(path: str, source_roots: tuple[str, ...] = ()) -> str:
    parts = list(Path(_strip_source_root(path, source_roots)).with_suffix("").parts)
    return ".".join(parts[:-1])


def _strip_source_root(path: str, source_roots: tuple[str, ...]) -> str:
    for root in sorted(source_roots, key=lambda value: (-len(Path(value).parts), value)):
        prefix = f"{root.rstrip('/')}/"
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _source_roots(inventory: RepositoryInventory) -> tuple[str, ...]:
    roots = {
        "src"
        for record in inventory.files
        if record.path.startswith("src/") and record.path.endswith(".py")
    }
    pyproject = next(
        (record for record in inventory.files if record.path == "pyproject.toml"), None
    )
    if pyproject is not None:
        try:
            payload = tomllib.loads(pyproject.text)
        except tomllib.TOMLDecodeError:
            payload = {}
        tool = payload.get("tool", {}) if isinstance(payload, dict) else {}
        setuptools = tool.get("setuptools", {}) if isinstance(tool, dict) else {}
        package_dir = (
            setuptools.get("package-dir", {}) if isinstance(setuptools, dict) else {}
        )
        if isinstance(package_dir, dict):
            roots.update(
                value.strip("/")
                for value in package_dir.values()
                if isinstance(value, str) and value.strip("/")
            )
    return tuple(sorted(roots))


def _imported_module(package: str, level: int, module: str | None) -> str:
    if level == 0:
        return module or ""
    package_parts = package.split(".") if package else []
    base = package_parts[: max(0, len(package_parts) - level + 1)]
    module_parts = module.split(".") if module else []
    return ".".join((*base, *module_parts))
