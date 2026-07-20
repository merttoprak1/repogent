from pathlib import Path

from repogent.domain import CheckResult, CheckStatus, ValidationReport
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
