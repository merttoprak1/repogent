from repogent.domain import (
    ImplementationPlan,
    QAReview,
    RequirementsSpec,
    RunManifest,
    ValidationReport,
)


def render_report(
    manifest: RunManifest,
    requirements: RequirementsSpec | None,
    plan: ImplementationPlan | None,
    validation: ValidationReport | None,
    review: QAReview | None,
) -> str:
    lines = [
        f"# Repogent run {manifest.run_id}",
        "",
        f"Status: **{manifest.status.value}**",
        f"Stage: `{manifest.stage.value}`",
        f"Request: {manifest.request}",
        f"Repair attempts: {manifest.repair_attempts}",
        f"Reason: {manifest.reason or 'none'}",
        "",
    ]
    if requirements:
        lines.extend(["## Requirements", "", requirements.model_dump_json(indent=2), ""])
    if plan:
        lines.extend(["## Implementation plan", "", plan.model_dump_json(indent=2), ""])
    lines.extend(["## Deterministic validation", ""])
    if validation:
        for check in validation.checks:
            exit_text = f" (exit {check.exit_code})" if check.exit_code is not None else ""
            lines.append(f"- {check.name}: {check.status.value}{exit_text}")
    else:
        lines.append("- Not run")
    lines.extend(["", "## Model-generated QA review", ""])
    lines.append(review.model_dump_json(indent=2) if review else "Not run")
    lines.append("")
    return "\n".join(lines)
