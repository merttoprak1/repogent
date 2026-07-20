from pathlib import Path

from repogent.repository import RepositoryInspector, RepositoryInventory
from repogent.symbols import PythonSymbolGraph, PythonSymbolGraphBuilder


def build_fixture(
    root: Path, files: dict[str, str]
) -> tuple[RepositoryInventory, PythonSymbolGraph]:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    inventory = RepositoryInspector().inspect(root)
    return inventory, PythonSymbolGraphBuilder().build(inventory)


def test_localizer_combines_lexical_symbol_import_and_test_signals(tmp_path: Path) -> None:
    from repogent.localization import PythonLocalizer

    inventory, graph = build_fixture(
        tmp_path,
        {
            "auth.py": "from limits import throttle\ndef login():\n    return throttle()\n",
            "tests/test_auth.py": "from auth import login\ndef test_login():\n    assert login()\n",
            "billing.py": "def invoice():\n    return 1\n",
        },
    )
    report = PythonLocalizer(max_snippets=4, max_total_chars=4_000).localize(
        inventory, graph, "fix login throttling"
    )
    assert report.snippets[0].path == "auth.py"
    assert {signal.name for signal in report.locations[0].signals} >= {
        "lexical",
        "symbol",
    }
    assert sum(len(snippet.text) for snippet in report.snippets) <= 4_000


def test_localizer_marks_diffuse_results_ambiguous(tmp_path: Path) -> None:
    from repogent.localization import PythonLocalizer

    inventory, graph = build_fixture(
        tmp_path,
        {"a.py": "def value(): return 1\n", "b.py": "def value(): return 2\n"},
    )
    report = PythonLocalizer().localize(inventory, graph, "change value")
    assert report.ambiguous is True
    assert "top locations are not concentrated" in report.ambiguity_reason
