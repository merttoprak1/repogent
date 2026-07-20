from __future__ import annotations

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
        return _check_payload(payload)

    def planning(
        self, requirements: RequirementsSpec, localization: LocalizationReport
    ) -> dict[str, object]:
        payload = {
            "requirements": _bounded_model(requirements),
            "localization": self._localization(localization),
        }
        return _check_payload(payload)

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
            "candidate_id": candidate_id,
        }
        if previous is not None:
            payload["previous_candidate"] = {
                "candidate_id": previous.candidate_id,
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
        return _check_payload(payload)

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
                "candidate_id": selected.candidate_id,
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
        return _check_payload(payload)

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
            text = _truncate(snippet.text, remaining)
            if not text:
                break
            snippets.append(
                {
                    "path": _truncate(snippet.path, 512),
                    "start_line": snippet.start_line,
                    "end_line": snippet.end_line,
                    "text": text,
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
        "candidate_id": evidence.candidate_id,
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


def _check_payload(payload: dict[str, object]) -> dict[str, object]:
    size = len(json.dumps(payload, sort_keys=True, default=str))
    if size > MAX_PROVIDER_PAYLOAD_CHARS:
        raise ValueError(
            f"provider context exceeds {MAX_PROVIDER_PAYLOAD_CHARS} characters: {size}"
        )
    return payload
