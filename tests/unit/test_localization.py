from pathlib import Path

from repogent.domain import CheckResult, CheckStatus, ValidationReport
from repogent.repository import RepositoryInspector, RepositoryInventory
from repogent.symbols import (
    PythonSymbolGraph,
    PythonSymbolGraphBuilder,
    SymbolEdge,
    SymbolKind,
    SymbolNode,
)


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


def test_localizer_resolves_imports_and_calls_in_their_source_scope(tmp_path: Path) -> None:
    from repogent.localization import PythonLocalizer

    inventory, graph = build_fixture(
        tmp_path,
        {
            "auth.py": "def login():\n    return True\n",
            "billing.py": "def login():\n    return False\n",
            "unrelated.py": "def logout():\n    return None\n",
            "tests/test_auth.py": (
                "from auth import login\n"
                "import auth as auth_alias\n"
                "import unrelated as unrelated_alias\n"
                "login()\n"
                "auth_alias.login()\n"
                "unrelated_alias.login()\n"
            ),
        },
    )

    report = PythonLocalizer().localize(inventory, graph, "change login")
    signals = {
        location.symbol_id: {signal.name for signal in location.signals}
        for location in report.locations
    }

    assert {"import", "call", "test"} <= signals["auth.py:auth.login"]
    assert not {"import", "call", "test"} & signals["billing.py:billing.login"]


def test_localizer_snippets_keep_complete_lines_with_accurate_end_line(tmp_path: Path) -> None:
    from repogent.localization import PythonLocalizer

    inventory, graph = build_fixture(
        tmp_path,
        {"feature.py": "def target():\n    return True\n"},
    )

    report = PythonLocalizer(max_total_chars=14).localize(inventory, graph, "target")

    assert report.snippets[0].text == "def target():"
    assert report.snippets[0].start_line == 1
    assert report.snippets[0].end_line == 1


def test_localizer_uses_acceptance_criteria_as_query_terms(tmp_path: Path) -> None:
    from repogent.localization import PythonLocalizer

    inventory, graph = build_fixture(tmp_path, {"auth.py": "def login():\n    return True\n"})

    report = PythonLocalizer().localize(
        inventory, graph, "change behavior", acceptance_criteria=["login succeeds"]
    )

    assert report.locations[0].symbol_id == "auth.py:auth.login"


def test_localizer_adds_failure_signal_from_validation_evidence(tmp_path: Path) -> None:
    from repogent.localization import PythonLocalizer

    inventory, graph = build_fixture(tmp_path, {"auth.py": "def login():\n    return True\n"})
    failure_evidence = ValidationReport(
        checks=[
            CheckResult(
                name="pytest",
                argv=["pytest"],
                status=CheckStatus.FAILED,
                stderr="auth login assertion failed",
            )
        ]
    )

    report = PythonLocalizer().localize(
        inventory, graph, "fix behavior", failure_evidence=failure_evidence
    )

    location = next(item for item in report.locations if item.symbol_id == "auth.py:auth.login")
    assert "failure" in {signal.name for signal in location.signals}


def test_ambiguity_uses_exact_score_and_concentration_thresholds() -> None:
    from repogent.localization import LocalizedSymbol, _ambiguity

    def location(score: float) -> LocalizedSymbol:
        return LocalizedSymbol(
            symbol_id=f"module:{score}",
            path="module.py",
            start_line=1,
            end_line=1,
            score=score,
            signals=[],
        )

    assert _ambiguity([location(0.34)])[0] is True
    assert _ambiguity([location(0.35)])[0] is False
    assert _ambiguity([location(0.60), location(0.50)])[0] is False
    assert _ambiguity([location(0.59), location(0.50)])[0] is True


def test_localizer_resolves_dotted_and_relative_import_bindings(tmp_path: Path) -> None:
    from repogent.localization import PythonLocalizer

    inventory, graph = build_fixture(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/auth.py": "def login():\n    return True\n",
            "billing.py": "def login():\n    return False\n",
            "pkg/tests/test_consumer.py": (
                "from ..auth import login\n"
                "import pkg.auth as auth_alias\n"
                "login()\n"
                "auth_alias.login()\n"
            ),
        },
    )

    report = PythonLocalizer().localize(inventory, graph, "change login")
    signals = {
        location.symbol_id: {signal.name for signal in location.signals}
        for location in report.locations
    }

    assert {"import", "call", "test"} <= signals["pkg/auth.py:pkg.auth.login"]
    assert not {"import", "call", "test"} & signals["billing.py:billing.login"]


def test_localizer_processes_deep_scopes_and_edges_once() -> None:
    from repogent.localization import _incoming_edges, _ResolutionCounters

    depth = 32
    module = SymbolNode(
        symbol_id="tests/test_deep.py:tests.test_deep",
        qualified_name="tests.test_deep",
        name="test_deep",
        kind=SymbolKind.MODULE,
        path="tests/test_deep.py",
        start_line=1,
        end_line=depth + 1,
    )
    auth_module = SymbolNode(
        symbol_id="pkg/auth.py:pkg.auth",
        qualified_name="pkg.auth",
        name="auth",
        kind=SymbolKind.MODULE,
        path="pkg/auth.py",
        start_line=1,
        end_line=2,
    )
    auth_login = SymbolNode(
        symbol_id="pkg/auth.py:pkg.auth.login",
        qualified_name="pkg.auth.login",
        name="login",
        kind=SymbolKind.FUNCTION,
        path="pkg/auth.py",
        start_line=1,
        end_line=2,
        parent_id=auth_module.symbol_id,
    )
    nested = [module]
    contains = [
        SymbolEdge(source=auth_module.symbol_id, target=auth_login.symbol_id, kind="contains")
    ]
    parent = module
    for index in range(depth):
        node = SymbolNode(
            symbol_id=f"scope:{index}",
            qualified_name=f"tests.test_deep.scope_{index}",
            name=f"scope_{index}",
            kind=SymbolKind.FUNCTION,
            path="tests/test_deep.py",
            start_line=index + 1,
            end_line=index + 1,
            parent_id=parent.symbol_id,
        )
        nested.append(node)
        contains.append(SymbolEdge(source=parent.symbol_id, target=node.symbol_id, kind="contains"))
        parent = node
    imports = [
        SymbolEdge(
            source=module.symbol_id,
            target=auth_login.qualified_name,
            kind="imports",
            binding="login",
            binding_target=auth_login.qualified_name,
        )
    ]
    calls = [SymbolEdge(source=node.symbol_id, target="login", kind="calls") for node in nested[1:]]
    counters = _ResolutionCounters()

    incoming, _source_paths = _incoming_edges(
        [*contains, *imports, *calls], [*nested, auth_module, auth_login], counters
    )

    assert counters.nodes == depth + 3
    assert counters.contains_edges == len(contains)
    assert counters.import_edges == len(imports)
    assert counters.call_edges == len(calls)
    assert len([edge for edge in incoming[auth_login.symbol_id] if edge.kind == "calls"]) == depth


def test_localizer_resolves_calls_with_statement_ordered_rebinding(tmp_path: Path) -> None:
    from repogent.localization import _incoming_edges

    _inventory, graph = build_fixture(
        tmp_path,
        {
            "a.py": "def login():\n    return True\n",
            "b.py": "def login():\n    return False\n",
            "consumer.py": (
                "from a import login\n"
                "login()\n"
                "from b import login\n"
                "login()\n"
            ),
        },
    )

    incoming, _source_paths = _incoming_edges(graph.edges, graph.nodes)
    calls_by_symbol = {
        symbol_id: [edge.line for edge in edges if edge.kind == "calls"]
        for symbol_id, edges in incoming.items()
    }

    assert calls_by_symbol["a.py:a.login"] == [2]
    assert calls_by_symbol["b.py:b.login"] == [4]


def test_localizer_does_not_resolve_calls_before_their_import(tmp_path: Path) -> None:
    from repogent.localization import _incoming_edges

    _inventory, graph = build_fixture(
        tmp_path,
        {
            "b.py": "def login():\n    return False\n",
            "consumer.py": "login()\nfrom b import login\n",
        },
    )

    incoming, _source_paths = _incoming_edges(graph.edges, graph.nodes)

    assert not [edge for edge in incoming["b.py:b.login"] if edge.kind == "calls"]


def test_localizer_resolves_unaliased_dotted_imports_from_package_binding(tmp_path: Path) -> None:
    from repogent.localization import PythonLocalizer

    inventory, graph = build_fixture(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/auth.py": "def login():\n    return True\n",
            "billing.py": "def login():\n    return False\n",
            "tests/test_consumer.py": "import pkg.auth\npkg.auth.login()\n",
        },
    )

    report = PythonLocalizer().localize(inventory, graph, "change login")
    signals = {
        location.symbol_id: {signal.name for signal in location.signals}
        for location in report.locations
    }

    assert {"call", "test"} <= signals["pkg/auth.py:pkg.auth.login"]
    assert not {"call", "test"} & signals["billing.py:billing.login"]


def test_localizer_excludes_class_bindings_from_methods_but_keeps_function_bindings(
    tmp_path: Path,
) -> None:
    from repogent.localization import _incoming_edges

    _inventory, graph = build_fixture(
        tmp_path,
        {
            "a.py": "def login():\n    return True\n",
            "consumer.py": (
                "class C:\n"
                "    from a import login\n"
                "    def method(self):\n"
                "        login()\n"
                "\n"
                "def outer():\n"
                "    from a import login\n"
                "    def inner():\n"
                "        login()\n"
            ),
        },
    )

    incoming, _source_paths = _incoming_edges(graph.edges, graph.nodes)
    call_lines = [
        edge.line for edge in incoming["a.py:a.login"] if edge.kind == "calls"
    ]

    assert call_lines == [9]
