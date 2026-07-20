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
        "import package.submodule\n"
        "import package as package_alias\n"
        "from module import name as name_alias\n"
    )

    graph = PythonSymbolGraphBuilder().build(RepositoryInspector().inspect(tmp_path))
    imports = [edge for edge in graph.edges if edge.kind == "imports"]

    assert [(edge.target, edge.alias, edge.binding, edge.binding_target) for edge in imports] == [
        ("package.submodule", None, "package", "package"),
        ("package", "package_alias", "package_alias", "package"),
        ("module.name", "name_alias", "name_alias", "module.name"),
    ]


def test_builder_normalizes_relative_import_targets(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "auth.py").write_text("def login():\n    return True\n")
    (package / "consumer.py").write_text("from .auth import login\n")

    graph = PythonSymbolGraphBuilder().build(RepositoryInspector().inspect(tmp_path))
    relative_import = next(
        edge for edge in graph.edges if edge.source == "pkg/consumer.py:pkg.consumer"
    )

    assert relative_import.target == "pkg.auth.login"
    assert relative_import.alias is None
    assert relative_import.binding == "login"


def test_builder_records_reference_order_and_binding_targets(tmp_path: Path) -> None:
    (tmp_path / "consumer.py").write_text(
        "import pkg.auth\n"
        "import pkg.auth as auth_alias\n"
        "from pkg.auth import login as renamed_login\n"
        "auth_alias.login()\n"
    )

    graph = PythonSymbolGraphBuilder().build(RepositoryInspector().inspect(tmp_path))
    imports = [edge for edge in graph.edges if edge.kind == "imports"]
    call = next(edge for edge in graph.edges if edge.kind == "calls")

    assert [(edge.target, edge.binding, edge.binding_target) for edge in imports] == [
        ("pkg.auth", "pkg", "pkg"),
        ("pkg.auth", "auth_alias", "pkg.auth"),
        ("pkg.auth.login", "renamed_login", "pkg.auth.login"),
    ]
    assert [(edge.line, edge.column) for edge in imports] == [(1, 0), (2, 0), (3, 0)]
    assert (call.line, call.column) == (4, 0)
