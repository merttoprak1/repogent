from collections.abc import Sequence
from decimal import Decimal

from repogent.domain import (
    CandidateEvidence,
    CandidateRecord,
    CandidateSelection,
    ImplementationPlan,
    QAReview,
    RequirementsSpec,
    RunManifest,
    ValidationReport,
)
from repogent.localization import LocalizationReport


def render_report(
    manifest: RunManifest,
    requirements: RequirementsSpec | None,
    plan: ImplementationPlan | None,
    validation: ValidationReport | None,
    review: QAReview | None,
    *,
    localization: LocalizationReport | None = None,
    candidates: Sequence[tuple[CandidateRecord, CandidateEvidence]] = (),
    selection: CandidateSelection | None = None,
) -> str:
    lines = [
        f"# Repogent run {manifest.run_id}",
        "",
        f"Status: **{manifest.status.value}**",
        f"Stage: `{manifest.stage.value}`",
        f"Request: {_markdown_text(manifest.request)}",
        f"Repair attempts: {manifest.repair_attempts}",
        f"Reason: {_markdown_text(manifest.reason or 'none')}",
        "",
    ]
    if requirements:
        lines.extend(["## Requirements", "", requirements.model_dump_json(indent=2), ""])
    if plan:
        lines.extend(["## Implementation plan", "", plan.model_dump_json(indent=2), ""])
    lines.extend(_render_localization(localization))
    lines.extend(_render_candidates(candidates, selection))
    lines.extend(_render_selection(selection))
    lines.extend(["## Deterministic validation", ""])
    if validation:
        for check in validation.checks:
            exit_text = f" (exit {check.exit_code})" if check.exit_code is not None else ""
            required = "required" if check.required else "optional"
            lines.append(
                f"- {_markdown_text(check.name)}: {check.status.value}{exit_text} ({required})"
            )
    else:
        lines.append("- Not run")
    lines.extend(["", "## Model-generated QA review", ""])
    lines.append(review.model_dump_json(indent=2) if review else "Not run")
    lines.extend(_render_cost_and_duration(manifest, candidates))
    lines.extend(_render_recovery(candidates))
    return "\n".join(lines)


def _render_localization(localization: LocalizationReport | None) -> list[str]:
    lines = ["## Localization", ""]
    if localization is None:
        return [*lines, "Not run", ""]
    lines.append(f"- Locations: {len(localization.locations)}")
    lines.append(f"- Ambiguous: {'yes' if localization.ambiguous else 'no'}")
    if localization.ambiguity_reason:
        lines.append(f"- Ambiguity reason: {_markdown_text(localization.ambiguity_reason)}")
    if localization.locations:
        lines.append(
            "- Top locations: "
            + ", ".join(
                _markdown_text(f"{location.path}:{location.start_line}")
                for location in localization.locations[:3]
            )
        )
    return [*lines, ""]


def _render_candidates(
    candidates: Sequence[tuple[CandidateRecord, CandidateEvidence]],
    selection: CandidateSelection | None,
) -> list[str]:
    lines = ["## Candidate comparison", ""]
    if not candidates:
        return [*lines, "No candidates were evaluated.", ""]
    lines.extend(
        [
            (
                "| Candidate | Eligible | Required failures | Skipped checks | "
                "Size (files/lines) | Coverage | Cost | Selected |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    selected_id = selection.selected_candidate_id if selection else None
    for candidate, evidence in sorted(candidates, key=lambda item: item[0].candidate_id):
        failures = ", ".join(evidence.required_failures) or "none"
        skipped = ", ".join(evidence.skipped_checks) or "none"
        marker = "selected" if candidate.candidate_id == selected_id else "rejected"
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_text(candidate.candidate_id),
                    "yes" if evidence.eligible else "no",
                    _markdown_text(failures),
                    _markdown_text(skipped),
                    f"{evidence.changed_files}/{evidence.changed_lines}",
                    str(evidence.acceptance_criteria_coverage),
                    f"${candidate.usage.estimated_cost_usd}",
                    marker,
                )
            )
            + " |"
        )
    return [*lines, ""]


def _render_selection(selection: CandidateSelection | None) -> list[str]:
    lines = ["## Selection", ""]
    if selection is None:
        return [*lines, "Not reached.", ""]
    selected = selection.selected_candidate_id or "none"
    eligible = _markdown_text(
        ", ".join(selection.eligible_candidate_ids) or "none"
    )
    lines.extend(
        [
            f"- Selected candidate: {_markdown_text(selected)}",
            f"- Eligible candidates: {eligible}",
            f"- Ambiguous: {'yes' if selection.ambiguous else 'no'}",
            f"- Reason: {_markdown_text(selection.reason)}",
            "",
        ]
    )
    return lines


def _render_cost_and_duration(
    manifest: RunManifest,
    candidates: Sequence[tuple[CandidateRecord, CandidateEvidence]],
) -> list[str]:
    candidate_cost = sum(
        (candidate.usage.estimated_cost_usd for candidate, _ in candidates), Decimal("0")
    )
    candidate_duration = sum(evidence.duration_seconds for _, evidence in candidates)
    validation_duration = sum(
        check.duration_seconds for _, evidence in candidates for check in evidence.validation.checks
    )
    return [
        "",
        "## Cost and duration",
        "",
        f"- Total model cost: ${manifest.estimated_cost_usd}",
        f"- Candidate generation cost: ${candidate_cost}",
        f"- Candidate evaluation duration: {candidate_duration}s",
        f"- Candidate validation duration: {validation_duration}s",
        "",
    ]


def _render_recovery(
    candidates: Sequence[tuple[CandidateRecord, CandidateEvidence]],
) -> list[str]:
    lines = ["## Recovery", ""]
    if not candidates:
        return [*lines, "No candidate evaluation recovery was required.", ""]
    for candidate, evidence in sorted(candidates, key=lambda item: item[0].candidate_id):
        state = "restored" if evidence.restored_to_baseline else "not restored"
        lines.append(f"- {_markdown_text(candidate.candidate_id)}: {state}")
    lines.append("")
    return lines


def _markdown_text(value: object) -> str:
    return " ".join(str(value).split()).replace("\\", "\\\\").replace("|", "\\|")
