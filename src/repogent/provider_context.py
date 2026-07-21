from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from repogent.domain import (
    CandidateEvidence,
    CandidateRecord,
    ImplementationPlan,
    RequirementsSpec,
    ValidationReport,
    VersionedModel,
)
from repogent.localization import LocalizationReport
from repogent.repository import FileRecord, RepositoryInventory

MAX_PROVIDER_PAYLOAD_CHARS = 64_000
MAX_PROVIDER_INVENTORY_CHARS = 32_000
MAX_PROVIDER_MODEL_CHARS = 8_000
MAX_PROVIDER_SNIPPETS = 8
MAX_PROVIDER_LOCATIONS = 12
MAX_PROVIDER_SNIPPET_CHARS = 20_000
MAX_PROVIDER_STDIO_CHARS = 2_000
MAX_PROVIDER_DIFF_CHARS = 20_000
_MAX_CONTEXT_STRING_CHARS = 1_000
_MAX_CONTEXT_LIST_ITEMS = 32
_MIN_COMPACTED_STRING_CHARS = 64
_PRESERVED_CONTEXT_FIELDS = {
    "candidate_id",
    "exit_code",
    "generation_reason",
    "passed",
    "reason",
    "required",
    "restored_to_baseline",
    "risk_level",
    "selection_reason",
    "status",
}
_MISSING = object()
ContextPath = tuple[str | int, ...]


class ProviderInventoryFile(VersionedModel):
    path: str
    size: int = Field(ge=0)
    sha256: str
    kind: str
    symbols: list[str] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)


class ProviderInventory(VersionedModel):
    total_files: int = Field(ge=0)
    included_files: int = Field(ge=0)
    truncated: bool
    skipped_count: int = Field(ge=0)
    files: list[ProviderInventoryFile]


class ProviderContextBuilder:
    """Build deterministic, bounded DTOs for every provider-facing role."""

    def requirements(self, request: str, inventory: RepositoryInventory) -> dict[str, object]:
        payload: dict[str, object] = {
            "request": _truncate(request, _MAX_CONTEXT_STRING_CHARS * 4),
            "repository_inventory": self._inventory(inventory).model_dump(mode="json"),
        }
        return _fit_payload(payload)

    def planning(
        self, requirements: RequirementsSpec, localization: LocalizationReport
    ) -> dict[str, object]:
        payload = {
            "requirements": _bounded_model(requirements),
            "localization": self._localization(localization),
        }
        return _fit_payload(payload)

    def candidate(
        self,
        requirements: RequirementsSpec,
        plan: ImplementationPlan,
        localization: LocalizationReport,
        candidate_id: str,
        *,
        previous: CandidateRecord | None = None,
        previous_evidence: CandidateEvidence | None = None,
        generation_reason: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "requirements": _bounded_model(requirements),
            "plan": _bounded_model(plan),
            "localization": self._localization(localization),
            "candidate_id": _truncate(candidate_id, 256),
        }
        if previous is not None:
            payload["previous_candidate"] = {
                "candidate_id": _truncate(previous.candidate_id, 256),
                "summary": _truncate(previous.proposal.summary, _MAX_CONTEXT_STRING_CHARS),
                "diff": _truncate(previous.proposal.diff, MAX_PROVIDER_DIFF_CHARS),
                "acceptance_criteria_addressed": _bounded_data(
                    previous.proposal.acceptance_criteria_addressed
                ),
                "assumptions": _bounded_data(previous.proposal.assumptions),
                "risks": _bounded_data(previous.proposal.risks),
            }
        if previous_evidence is not None:
            payload["previous_failure"] = _failure_summary(previous_evidence)
        if generation_reason is not None:
            payload["generation_reason"] = _truncate(
                generation_reason, _MAX_CONTEXT_STRING_CHARS
            )
        return _fit_payload(payload)

    def qa(
        self,
        requirements: RequirementsSpec,
        plan: ImplementationPlan,
        selected: CandidateRecord,
        selection_reason: str,
        validation: ValidationReport,
    ) -> dict[str, object]:
        payload = {
            "requirements": _bounded_model(requirements),
            "plan": _bounded_model(plan),
            "acceptance_criteria": _bounded_data(requirements.acceptance_criteria),
            "selected_candidate": {
                "candidate_id": _truncate(selected.candidate_id, 256),
                "summary": _truncate(selected.proposal.summary, _MAX_CONTEXT_STRING_CHARS),
                "diff": _truncate(selected.proposal.diff, MAX_PROVIDER_DIFF_CHARS),
                "acceptance_criteria_addressed": _bounded_data(
                    selected.proposal.acceptance_criteria_addressed
                ),
                "risks": _bounded_data(selected.proposal.risks),
            },
            "selection_reason": _truncate(selection_reason, _MAX_CONTEXT_STRING_CHARS),
            "final_validation": _validation_summary(validation),
        }
        return _fit_payload(payload)

    def _inventory(self, inventory: RepositoryInventory) -> ProviderInventory:
        files: list[ProviderInventoryFile] = []
        for record in inventory.files:
            candidate = [*files, _inventory_file(record)]
            serialized = json.dumps(
                [item.model_dump(mode="json") for item in candidate],
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(serialized) > MAX_PROVIDER_INVENTORY_CHARS:
                break
            files = candidate
        return ProviderInventory(
            total_files=len(inventory.files),
            included_files=len(files),
            truncated=len(files) < len(inventory.files),
            skipped_count=len(inventory.skipped),
            files=files,
        )

    def _localization(self, localization: LocalizationReport) -> dict[str, object]:
        locations = [
            {
                "symbol_id": _truncate(location.symbol_id, 512),
                "path": _truncate(location.path, 512),
                "start_line": location.start_line,
                "end_line": location.end_line,
                "score": location.score,
                "signals": [
                    {
                        "name": signal.name,
                        "score": signal.score,
                        "reason": _truncate(signal.reason, 512),
                    }
                    for signal in location.signals[:8]
                ],
            }
            for location in localization.locations[:MAX_PROVIDER_LOCATIONS]
        ]
        snippets: list[dict[str, object]] = []
        remaining = MAX_PROVIDER_SNIPPET_CHARS
        for snippet in localization.snippets[:MAX_PROVIDER_SNIPPETS]:
            text, included_line_count = _complete_lines(snippet.text, remaining)
            if not text:
                break
            original_line_count = _line_count(snippet.text)
            snippets.append(
                {
                    "path": _truncate(snippet.path, 512),
                    "start_line": snippet.start_line,
                    "end_line": snippet.start_line + included_line_count - 1,
                    "text": text,
                    "text_truncated": included_line_count < original_line_count,
                    "omitted_line_count": original_line_count - included_line_count,
                    "score": snippet.score,
                    "reason": _truncate(snippet.reason, 512),
                }
            )
            remaining -= len(text)
        return {
            "ambiguous": localization.ambiguous,
            "ambiguity_reason": _truncate(localization.ambiguity_reason or "", 1_000),
            "total_locations": len(localization.locations),
            "locations_truncated": len(locations) < len(localization.locations),
            "locations": locations,
            "total_snippets": len(localization.snippets),
            "snippets_truncated": len(snippets) < len(localization.snippets),
            "omitted_snippet_count": len(localization.snippets) - len(snippets),
            "snippets": snippets,
        }


def _inventory_file(record: FileRecord) -> ProviderInventoryFile:
    return ProviderInventoryFile(
        path=_truncate(record.path, 512),
        size=record.size,
        sha256=record.sha256,
        kind=_truncate(record.kind, 64),
        symbols=[_truncate(value, 256) for value in record.symbols[:16]],
        imports=[_truncate(value, 256) for value in record.imports[:16]],
        routes=[_truncate(value, 256) for value in record.routes[:16]],
    )


def _failure_summary(evidence: CandidateEvidence) -> dict[str, object]:
    return {
        "candidate_id": _truncate(evidence.candidate_id, 256),
        "acceptance_criteria_coverage": evidence.acceptance_criteria_coverage,
        "risk_level": evidence.risk_level.value,
        "changed_files": evidence.changed_files,
        "changed_lines": evidence.changed_lines,
        "required_failures": _bounded_data(evidence.required_failures),
        "skipped_checks": _bounded_data(evidence.skipped_checks),
        "restored_to_baseline": evidence.restored_to_baseline,
        "checks": [_check_summary(check) for check in evidence.validation.checks[:16]],
    }


def _validation_summary(validation: ValidationReport) -> dict[str, object]:
    return {
        "passed": validation.passed,
        "checks": [_check_summary(check) for check in validation.checks[:16]],
        "checks_truncated": len(validation.checks) > 16,
    }


def _check_summary(check: Any) -> dict[str, object]:
    return {
        "name": _truncate(check.name, 256),
        "argv": [_truncate(value, 256) for value in check.argv[:16]],
        "status": check.status.value,
        "exit_code": check.exit_code,
        "stdout": _truncate(check.stdout, MAX_PROVIDER_STDIO_CHARS),
        "stderr": _truncate(check.stderr, MAX_PROVIDER_STDIO_CHARS),
        "reason": _truncate(check.reason or "", 1_000),
        "required": check.required,
    }


def _bounded_model(model: VersionedModel) -> object:
    data = _bounded_data(model.model_dump(mode="json"))
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"))
    if len(serialized) <= MAX_PROVIDER_MODEL_CHARS:
        return data
    return {
        "schema_version": model.schema_version,
        "summary": _truncate(serialized, MAX_PROVIDER_MODEL_CHARS - 100),
        "truncated": True,
    }


def _bounded_data(value: object) -> object:
    if isinstance(value, str):
        return _truncate(value, _MAX_CONTEXT_STRING_CHARS)
    if isinstance(value, list):
        return [_bounded_data(item) for item in value[:_MAX_CONTEXT_LIST_ITEMS]]
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_data(item)
            for key, item in list(value.items())[:_MAX_CONTEXT_LIST_ITEMS]
        }
    return value


def _truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    marker = "...[truncated]"
    if limit <= len(marker):
        return marker[:limit]
    return value[: limit - len(marker)] + marker


def _complete_lines(value: str, limit: int) -> tuple[str, int]:
    if limit <= 0 or not value:
        return ("", 0)
    included: list[str] = []
    size = 0
    for line in value.splitlines():
        separator_size = 1 if included else 0
        if size + separator_size + len(line) > limit:
            break
        included.append(line)
        size += separator_size + len(line)
    return ("\n".join(included), len(included))


def _line_count(value: str) -> int:
    return len(value.splitlines()) if value else 0


def _serialized_size(value: object) -> int:
    return len(json.dumps(value, sort_keys=True, default=str))


def _fit_payload(payload: dict[str, object]) -> dict[str, object]:
    """Fit a provider payload by shortening values, never serialized JSON."""

    fitted = copy.deepcopy(payload)
    original_size = _serialized_size(fitted)
    local_marker_count = _truncation_marker_count(fitted)
    if original_size <= MAX_PROVIDER_PAYLOAD_CHARS and local_marker_count == 0:
        return fitted
    truncation: dict[str, Any] = {
        "truncated": True,
        "max_chars": MAX_PROVIDER_PAYLOAD_CHARS,
        "original_chars": original_size,
        "local_marker_count": local_marker_count,
        "strings_shortened": 0,
        "characters_omitted": 0,
        "items_omitted": _reported_omission_count(fitted),
        "fallback_projection": False,
    }
    fitted["context_truncation"] = truncation

    while _serialized_size(fitted) > MAX_PROVIDER_PAYLOAD_CHARS:
        strings = _shrinkable_strings(fitted)
        if strings:
            path, string_value = max(strings, key=lambda item: len(item[1]))
            target = max(_MIN_COMPACTED_STRING_CHARS, len(string_value) // 2)
            omitted, omitted_items = _shorten_string(
                fitted, path, string_value, target
            )
            if omitted_items == 0:
                truncation["strings_shortened"] = (
                    int(truncation["strings_shortened"]) + 1
                )
            truncation["items_omitted"] = (
                int(truncation["items_omitted"]) + omitted_items
            )
            truncation["characters_omitted"] = (
                int(truncation["characters_omitted"]) + omitted
            )
            continue
        lists = _shrinkable_lists(fitted)
        if lists:
            path, items = max(lists, key=lambda item: _serialized_size(item[1]))
            omitted_item = items.pop()
            truncation["items_omitted"] = int(truncation["items_omitted"]) + 1
            truncation["characters_omitted"] = int(
                truncation["characters_omitted"]
            ) + _serialized_size(omitted_item)
            _mark_list_truncated(fitted, path)
            continue
        fitted = _critical_projection_payload(fitted, truncation)
        break

    if _serialized_size(fitted) > MAX_PROVIDER_PAYLOAD_CHARS:
        fitted = {
            "context_truncation": {
                **truncation,
                "fallback_projection": True,
                "critical_context_omitted": True,
            }
        }
    return fitted


def _truncation_marker_count(value: object) -> int:
    if isinstance(value, str):
        return int("[truncated]" in value)
    if isinstance(value, list):
        return sum(_truncation_marker_count(item) for item in value)
    if isinstance(value, Mapping):
        count = 0
        for key, item in value.items():
            if (str(key) == "truncated" or str(key).endswith("_truncated")) and item is True:
                count += 1
            count += _truncation_marker_count(item)
        return count
    return 0


def _reported_omission_count(value: object) -> int:
    if isinstance(value, list):
        return sum(_reported_omission_count(item) for item in value)
    if isinstance(value, Mapping):
        count = 0
        for key, item in value.items():
            if (str(key).startswith("omitted_") or str(key).endswith("_omitted")) and isinstance(
                item, int
            ):
                count += item
            count += _reported_omission_count(item)
        return count
    return 0


def _shrinkable_strings(
    value: object, path: ContextPath = ()
) -> list[tuple[ContextPath, str]]:
    candidates: list[tuple[ContextPath, str]] = []
    if isinstance(value, dict):
        for key in sorted(value):
            if key == "context_truncation":
                continue
            item = value[key]
            child_path = (*path, key)
            if isinstance(item, str):
                if key not in _PRESERVED_CONTEXT_FIELDS and len(item) > _MIN_COMPACTED_STRING_CHARS:
                    candidates.append((child_path, item))
            else:
                candidates.extend(_shrinkable_strings(item, child_path))
    elif isinstance(value, list):
        preserved = _path_field(path) in _PRESERVED_CONTEXT_FIELDS
        for index, item in enumerate(value):
            child_path = (*path, index)
            if isinstance(item, str):
                if not preserved and len(item) > _MIN_COMPACTED_STRING_CHARS:
                    candidates.append((child_path, item))
            else:
                candidates.extend(_shrinkable_strings(item, child_path))
    return candidates


def _shrinkable_lists(
    value: object, path: ContextPath = ()
) -> list[tuple[ContextPath, list[object]]]:
    candidates: list[tuple[ContextPath, list[object]]] = []
    if isinstance(value, dict):
        for key in sorted(value):
            if key == "context_truncation":
                continue
            item = value[key]
            child_path = (*path, key)
            if isinstance(item, list):
                if item and key != "checks":
                    candidates.append((child_path, item))
                candidates.extend(_shrinkable_lists(item, child_path))
            elif isinstance(item, dict):
                candidates.extend(_shrinkable_lists(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                candidates.extend(_shrinkable_lists(item, (*path, index)))
    return candidates


def _path_field(path: ContextPath) -> str | None:
    return next((item for item in reversed(path) if isinstance(item, str)), None)


def _resolve_path(root: object, path: ContextPath) -> object:
    current = root
    for item in path:
        if isinstance(item, int) and isinstance(current, list):  # noqa: SIM114
            current = current[item]
        elif isinstance(item, str) and isinstance(current, dict):
            current = current[item]
        else:
            raise TypeError(f"invalid provider context path: {path}")
    return current


def _shorten_string(
    payload: dict[str, object], path: ContextPath, current: str, target: int
) -> tuple[int, int]:
    parent = _resolve_path(payload, path[:-1])
    key = path[-1]
    if key == "text" and "snippets" in path and isinstance(parent, dict):
        text, line_count = _complete_lines(current, target)
        if text:
            omitted_lines = _line_count(current) - line_count
            parent["text"] = text
            parent["end_line"] = int(parent["start_line"]) + line_count - 1
            parent["text_truncated"] = True
            parent["omitted_line_count"] = int(
                parent.get("omitted_line_count", 0)
            ) + omitted_lines
            return (len(current) - len(text), 0)
        snippets = _resolve_path(payload, path[:-2])
        snippet_index = path[-2]
        localization = _resolve_path(payload, path[:-3])
        if (
            isinstance(snippets, list)
            and isinstance(snippet_index, int)
            and isinstance(localization, dict)
        ):
            snippets.pop(snippet_index)
            localization["snippets_truncated"] = True
            localization["omitted_snippet_count"] = int(
                localization.get("omitted_snippet_count", 0)
            ) + 1
            return (len(current), 1)
    replacement = _truncate(current, target)
    if isinstance(key, int) and isinstance(parent, list):  # noqa: SIM114
        parent[key] = replacement
    elif isinstance(key, str) and isinstance(parent, dict):
        parent[key] = replacement
    else:
        raise TypeError(f"invalid provider context string path: {path}")
    return (len(current) - len(replacement), 0)


def _mark_list_truncated(payload: dict[str, object], path: ContextPath) -> None:
    parent = _resolve_path(payload, path[:-1])
    field = path[-1]
    if isinstance(parent, dict) and isinstance(field, str):
        parent[f"{field}_truncated"] = True
        omitted_key = f"omitted_{field}_count"
        parent[omitted_key] = int(parent.get(omitted_key, 0)) + 1


def _critical_projection_payload(
    payload: dict[str, object], truncation: dict[str, Any]
) -> dict[str, object]:
    projected = _critical_projection(payload)
    result = projected if isinstance(projected, dict) else {}
    result["context_truncation"] = {
        **truncation,
        "fallback_projection": True,
    }
    return result


def _critical_projection(value: object, field: str | None = None) -> object:
    if isinstance(value, dict):
        projected: dict[str, object] = {}
        for key in sorted(value):
            if key == "context_truncation":
                continue
            item = value[key]
            if key in _PRESERVED_CONTEXT_FIELDS:
                projected[key] = copy.deepcopy(item)
                continue
            nested = _critical_projection(item, key)
            if nested is not _MISSING:
                projected[key] = nested
        return projected if projected else _MISSING
    if isinstance(value, list):
        projected_items: list[object] = []
        for item in value:
            projected_item = _critical_projection(item, field)
            if projected_item is not _MISSING:
                projected_items.append(projected_item)
        return projected_items if projected_items else _MISSING
    return copy.deepcopy(value) if field in _PRESERVED_CONTEXT_FIELDS else _MISSING
