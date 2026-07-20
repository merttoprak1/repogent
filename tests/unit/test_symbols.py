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
    assert any(
        edge.kind == "imports" and edge.target == "helpers.normalize"
        for edge in graph.edges
    )
    assert any(edge.kind == "calls" and edge.target == "normalize" for edge in graph.edges)


def test_builder_reports_one_parse_error_without_losing_valid_files(tmp_path: Path) -> None:
    (tmp_path / "good.py").write_text("def valid():\n    return 1\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")
    graph = PythonSymbolGraphBuilder().build(RepositoryInspector().inspect(tmp_path))
    assert [node.qualified_name for node in graph.nodes] == ["good.valid"]
    assert graph.parse_errors == {"bad.py": "invalid syntax"}
