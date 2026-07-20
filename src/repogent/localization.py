from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from repogent.domain import ContextSnippet, ValidationReport, VersionedModel
from repogent.repository import TOKEN, FileRecord, RepositoryInventory
from repogent.symbols import PythonSymbolGraph, SymbolEdge, SymbolKind, SymbolNode

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
        incoming, source_paths = _incoming_edges(graph.edges, graph.nodes)
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
                source_paths,
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
        source_paths: dict[str, str],
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
            source_paths.get(edge.source) in test_paths for edge in incoming
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
            last_available_line = min(max(1, len(lines)), location.end_line + 20)
            remaining = self.max_total_chars - total_chars
            included_lines: list[str] = []
            for line in lines[start_line - 1 : last_available_line]:
                separator = "\n" if included_lines else ""
                if len("\n".join(included_lines)) + len(separator) + len(line) > remaining:
                    break
                included_lines.append(line)
            if not included_lines:
                continue
            text = "\n".join(included_lines)
            snippets.append(
                ContextSnippet(
                    path=location.path,
                    start_line=start_line,
                    end_line=start_line + len(included_lines) - 1,
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
    edges: list[SymbolEdge],
    nodes: list[SymbolNode],
    counters: _ResolutionCounters | None = None,
) -> tuple[dict[str, list[SymbolEdge]], dict[str, str]]:
    counters = counters or _ResolutionCounters()
    nodes_by_id = {node.symbol_id: node for node in nodes}
    qualified_nodes = _unique_qualified_nodes(nodes)
    children: dict[str, list[str]] = defaultdict(list)
    imports_by_source: dict[str, list[SymbolEdge]] = defaultdict(list)
    calls_by_source: dict[str, list[SymbolEdge]] = defaultdict(list)
    for edge in edges:
        if edge.kind == "contains":
            if edge.source in nodes_by_id and edge.target in nodes_by_id:
                children[edge.source].append(edge.target)
                counters.contains_edges += 1
        elif edge.kind == "imports" and edge.source in nodes_by_id:
            imports_by_source[edge.source].append(edge)
        elif edge.kind == "calls" and edge.source in nodes_by_id:
            calls_by_source[edge.source].append(edge)

    incoming: dict[str, list[SymbolEdge]] = defaultdict(list)
    binding_stacks: dict[str, list[str]] = defaultdict(list)
    visited: set[str] = set()

    def walk(root_id: str, module_name: str | None) -> None:
        work: list[tuple[bool, str, str | None, list[str]]] = [
            (False, root_id, module_name, [])
        ]
        while work:
            exiting, node_id, current_module, pushed_bindings = work.pop()
            if exiting:
                for binding in reversed(pushed_bindings):
                    binding_stacks[binding].pop()
                continue
            if node_id in visited:
                continue
            node = nodes_by_id[node_id]
            visited.add(node_id)
            counters.nodes += 1
            if node.kind is SymbolKind.MODULE:
                current_module = node.qualified_name

            pushed_bindings = []
            for edge in imports_by_source.get(node_id, []):
                counters.import_edges += 1
                target = qualified_nodes.get(edge.target)
                if target is None:
                    continue
                incoming[target.symbol_id].append(edge)
                if edge.binding is not None:
                    binding_stacks[edge.binding].append(target.qualified_name)
                    pushed_bindings.append(edge.binding)
            for edge in calls_by_source.get(node_id, []):
                counters.call_edges += 1
                target = _resolve_scoped_call(
                    edge.target, binding_stacks, current_module, qualified_nodes
                )
                if target is not None:
                    incoming[target.symbol_id].append(edge)

            work.append((True, node_id, current_module, pushed_bindings))
            for child_id in reversed(children.get(node_id, [])):
                work.append((False, child_id, current_module, []))

    for node in nodes:
        if node.parent_id is None:
            walk(node.symbol_id, node.qualified_name if node.kind is SymbolKind.MODULE else None)
    for node in nodes:
        if node.symbol_id not in visited:
            walk(node.symbol_id, node.qualified_name if node.kind is SymbolKind.MODULE else None)

    return incoming, {symbol_id: node.path for symbol_id, node in nodes_by_id.items()}


def _unique_qualified_nodes(nodes: list[SymbolNode]) -> dict[str, SymbolNode]:
    grouped: dict[str, list[SymbolNode]] = defaultdict(list)
    for node in nodes:
        grouped[node.qualified_name].append(node)
    return {name: matches[0] for name, matches in grouped.items() if len(matches) == 1}


def _resolve_scoped_call(
    target: str,
    binding_stacks: dict[str, list[str]],
    module_name: str | None,
    qualified_nodes: dict[str, SymbolNode],
) -> SymbolNode | None:
    first, *remainder = target.split(".")
    bound_targets = binding_stacks.get(first)
    if bound_targets:
        candidate = ".".join((bound_targets[-1], *remainder))
        return qualified_nodes.get(candidate)
    direct = qualified_nodes.get(target)
    if direct is not None:
        return direct
    return qualified_nodes.get(f"{module_name}.{target}") if module_name else None


@dataclass
class _ResolutionCounters:
    nodes: int = 0
    contains_edges: int = 0
    import_edges: int = 0
    call_edges: int = 0


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
