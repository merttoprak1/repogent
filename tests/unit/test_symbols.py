from pathlib import Path

from repogent.repository import RepositoryInspector
from repogent.symbols import PythonSymbolGraphBuilder, SymbolKind


def test_builder_records_qualified_symbols_imports_and_calls(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        "from helpers import normalize\n"
        "class UserService:\n"
        "    def save(self, name: str) -> str:\n"
        "        return normalize(name)\n"
    )
    graph = PythonSymbolGraphBuilder().build(RepositoryInspector().inspect(tmp_path))
    symbols = {node.qualified_name: node for node in graph.nodes}
    assert symbols["service.UserService"].kind is SymbolKind.CLASS
    assert symbols["service.UserService.save"].start_line == 3
    imported = next(
        edge
        for edge in graph.edges
        if edge.kind == "imports" and edge.target == "helpers.normalize"
    )
    assert imported.alias is None
    assert any(edge.kind == "calls" and edge.target == "normalize" for edge in graph.edges)


def test_builder_reports_one_parse_error_without_losing_valid_files(tmp_path: Path) -> None:
    (tmp_path / "good.py").write_text("def valid():\n    return 1\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")
    graph = PythonSymbolGraphBuilder().build(RepositoryInspector().inspect(tmp_path))
    assert [node.qualified_name for node in graph.nodes] == ["good", "good.valid"]
    assert graph.parse_errors == {"bad.py": "invalid syntax"}


def test_builder_adds_module_nodes_and_top_level_containment(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("def initialize():\n    return 1\n")
    (tmp_path / "service.py").write_text(
        "import helpers\n"
        "run()\n"
        "def execute():\n"
        "    return 1\n"
    )

    graph = PythonSymbolGraphBuilder().build(RepositoryInspector().inspect(tmp_path))
    symbols = {node.qualified_name: node for node in graph.nodes}
    package_node = symbols["package"]
    service_node = symbols["service"]

    assert package_node.kind is SymbolKind.MODULE
    assert package_node.start_line == 1
    assert package_node.end_line == 2
    assert symbols["package.initialize"].parent_id == package_node.symbol_id
    assert any(
        edge.source == service_node.symbol_id
        and edge.target == symbols["service.execute"].symbol_id
        and edge.kind == "contains"
        for edge in graph.edges
    )
    assert any(
        edge.source == service_node.symbol_id
        and edge.target == "helpers"
        and edge.kind == "imports"
        for edge in graph.edges
    )
    assert any(
        edge.source == service_node.symbol_id and edge.target == "run" and edge.kind == "calls"
        for edge in graph.edges
    )


def test_builder_preserves_import_aliases(tmp_path: Path) -> None:
    (tmp_path / "aliases.py").write_text(
        "import package as package_alias\n"
        "from module import name as name_alias\n"
    )

    graph = PythonSymbolGraphBuilder().build(RepositoryInspector().inspect(tmp_path))
    imports = [edge for edge in graph.edges if edge.kind == "imports"]

    assert [(edge.target, edge.alias) for edge in imports] == [
        ("module.name", "name_alias"),
        ("package", "package_alias"),
    ]
