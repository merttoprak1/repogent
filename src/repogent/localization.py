from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from repogent.domain import ContextSnippet, ValidationReport, VersionedModel
from repogent.repository import TOKEN, FileRecord, RepositoryInventory
from repogent.symbols import PythonSymbolGraph, SymbolEdge, SymbolNode

SignalName = Literal["lexical", "symbol", "import", "call", "test", "failure"]

_SIGNAL_WEIGHTS: dict[SignalName, float] = {
    "lexical": 1.0,
    "symbol": 1.5,
    "import": 0.5,
    "call": 0.5,
    "test": 0.75,
    "failure": 2.0,
}
_TOTAL_SIGNAL_WEIGHT = sum(_SIGNAL_WEIGHTS.values())


class LocalizationSignal(VersionedModel):
    name: SignalName
    score: float = Field(gt=0)
    reason: str


class LocalizedSymbol(VersionedModel):
    symbol_id: str
    path: str
    start_line: int
    end_line: int
    score: float = Field(ge=0)
    signals: list[LocalizationSignal]


class LocalizationReport(VersionedModel):
    locations: list[LocalizedSymbol]
    snippets: list[ContextSnippet]
    ambiguous: bool
    ambiguity_reason: str | None = None


class PythonLocalizer:
    def __init__(self, *, max_snippets: int = 8, max_total_chars: int = 20_000) -> None:
        if max_snippets < 1 or max_total_chars < 1:
            raise ValueError("snippet limits must be positive")
        self.max_snippets = max_snippets
        self.max_total_chars = max_total_chars

    def localize(
        self,
        inventory: RepositoryInventory,
        graph: PythonSymbolGraph,
        request: str,
        acceptance_criteria: Sequence[str] = (),
        failure_evidence: ValidationReport | None = None,
    ) -> LocalizationReport:
        query_tokens = _tokens(" ".join((request, *acceptance_criteria)))
        if not query_tokens:
            return self._report([], inventory)

        records = {record.path: record for record in inventory.files}
        incoming = _incoming_edges(graph.edges, graph.nodes)
        test_paths = {record.path for record in inventory.files if record.kind == "test"}
        failure_tokens = _failure_tokens(failure_evidence)
        locations: list[LocalizedSymbol] = []
        for node in graph.nodes:
            record = records.get(node.path)
            if record is None:
                continue
            signals = self._signals(
                node,
                record,
                query_tokens,
                failure_tokens,
                incoming.get(node.symbol_id, []),
                test_paths,
                graph.nodes,
            )
            if signals:
                locations.append(
                    LocalizedSymbol(
                        symbol_id=node.symbol_id,
                        path=node.path,
                        start_line=node.start_line,
                        end_line=node.end_line,
                        score=sum(signal.score for signal in signals) / _TOTAL_SIGNAL_WEIGHT,
                        signals=signals,
                    )
                )
        locations.sort(key=lambda item: (-item.score, item.path, item.start_line, item.symbol_id))
        return self._report(locations, inventory)

    def _signals(
        self,
        node: SymbolNode,
        record: FileRecord,
        query_tokens: list[str],
        failure_tokens: set[str],
        incoming: list[SymbolEdge],
        test_paths: set[str],
        nodes: list[SymbolNode],
    ) -> list[LocalizationSignal]:
        signals: list[LocalizationSignal] = []
        lexical_matches = _matches(query_tokens, _tokens(" ".join((record.path, record.text))))
        if lexical_matches:
            signals.append(_signal("lexical", lexical_matches))
        symbol_matches = _matches(query_tokens, _tokens(" ".join((node.name, node.qualified_name))))
        if symbol_matches:
            signals.append(_signal("symbol", symbol_matches))
        if any(edge.kind == "imports" for edge in incoming):
            signals.append(_signal("import", ["referenced by import"]))
        if any(edge.kind == "calls" for edge in incoming):
            signals.append(_signal("call", ["referenced by call"]))
        if node.path not in test_paths and any(
            _source_path(edge.source, nodes) in test_paths for edge in incoming
        ):
            signals.append(_signal("test", ["referenced by test"]))
        failure_matches = sorted(
            set(_tokens(" ".join((node.path, node.qualified_name)))) & failure_tokens
        )
        if failure_matches:
            signals.append(_signal("failure", failure_matches))
        return signals

    def _report(
        self, locations: list[LocalizedSymbol], inventory: RepositoryInventory
    ) -> LocalizationReport:
        snippets = self._snippets(locations, inventory)
        ambiguous, reason = _ambiguity(locations)
        return LocalizationReport(
            locations=locations,
            snippets=snippets,
            ambiguous=ambiguous,
            ambiguity_reason=reason,
        )

    def _snippets(
        self, locations: list[LocalizedSymbol], inventory: RepositoryInventory
    ) -> list[ContextSnippet]:
        records = {record.path: record for record in inventory.files}
        snippets: list[ContextSnippet] = []
        total_chars = 0
        for location in locations:
            if len(snippets) == self.max_snippets or total_chars == self.max_total_chars:
                break
            record = records.get(location.path)
            if record is None:
                continue
            lines = record.text.splitlines()
            start_line = max(1, location.start_line - 20)
            end_line = min(max(1, len(lines)), location.end_line + 20)
            text = "\n".join(lines[start_line - 1 : end_line])
            remaining = self.max_total_chars - total_chars
            text = text[:remaining]
            if not text:
                break
            snippets.append(
                ContextSnippet(
                    path=location.path,
                    start_line=start_line,
                    end_line=end_line,
                    text=text,
                    score=location.score,
                    reason="; ".join(signal.reason for signal in location.signals),
                )
            )
            total_chars += len(text)
        return snippets


def _signal(name: SignalName, matches: list[str]) -> LocalizationSignal:
    return LocalizationSignal(
        name=name,
        score=_SIGNAL_WEIGHTS[name],
        reason=f"{name} signal: {', '.join(matches)}",
    )


def _tokens(value: str) -> list[str]:
    return [part.lower() for token in TOKEN.findall(value) for part in token.split("_") if part]


def _matches(query_tokens: list[str], document_tokens: list[str]) -> list[str]:
    return sorted(set(query_tokens) & set(document_tokens))


def _incoming_edges(
    edges: list[SymbolEdge], nodes: list[SymbolNode]
) -> dict[str, list[SymbolEdge]]:
    incoming: dict[str, list[SymbolEdge]] = defaultdict(list)
    for edge in edges:
        for node in nodes:
            if _edge_targets_node(edge, node):
                incoming[node.symbol_id].append(edge)
    return incoming


def _edge_targets_node(edge: SymbolEdge, node: SymbolNode) -> bool:
    return edge.target in {node.symbol_id, node.qualified_name, node.name} or edge.target.endswith(
        f".{node.qualified_name}"
    ) or edge.target.endswith(f".{node.name}") or edge.alias == node.name


def _source_path(symbol_id: str, nodes: list[SymbolNode]) -> str | None:
    for node in nodes:
        if node.symbol_id == symbol_id:
            return node.path
    return None


def _failure_tokens(failure_evidence: ValidationReport | None) -> set[str]:
    if failure_evidence is None:
        return set()
    values = [
        value
        for check in failure_evidence.checks
        for value in (check.name, *check.argv, check.stdout, check.stderr, check.reason or "")
    ]
    return set(_tokens(" ".join(values)))


def _ambiguity(locations: list[LocalizedSymbol]) -> tuple[bool, str | None]:
    if not locations:
        return True, "no locations matched the request"
    top_score = locations[0].score
    if top_score < 0.35:
        return True, "top location score is below 0.35"
    if len(locations) > 1 and top_score < 1.20 * locations[1].score:
        return True, (
            "top locations are not concentrated "
            "(top score is less than 1.20 times second score)"
        )
    return False, None
