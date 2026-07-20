from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from openai import OpenAIError

from repogent.agents import RoleSet
from repogent.approvals import CliApprover
from repogent.artifacts import ArtifactStore, ArtifactStoreError
from repogent.domain import Budget, RunManifest, RunStage, RunStatus
from repogent.events import CompositeEventSink, ConsoleEventSink
from repogent.execution import DockerExecutor, LocalExecutor, ValidationPolicy
from repogent.localization import PythonLocalizer
from repogent.patching import PatchApplier, PatchPolicy
from repogent.preflight import Preflight, configuration_fingerprint
from repogent.providers import ModelProvider, OpenAIProvider, ProviderError, ScriptedProvider
from repogent.reporting import render_report
from repogent.repository import LexicalRetriever, RepositoryInspector
from repogent.symbols import PythonSymbolGraphBuilder
from repogent.validation import ValidationPipeline
from repogent.workflow import Workflow

app = typer.Typer(no_args_is_help=True)


@app.command()
def analyze(
    repository: Annotated[Path, typer.Argument(exists=True, file_okay=False, resolve_path=True)],
    request: Annotated[
        str, typer.Option("--request", help="Task used to rank relevant files")
    ] = "",
) -> None:
    """Print a read-only repository inventory, symbol graph, and localization as JSON."""
    inventory = RepositoryInspector().inspect(repository)
    graph = PythonSymbolGraphBuilder().build(inventory)
    localization = PythonLocalizer().localize(inventory, graph, request) if request else None
    typer.echo(
        json.dumps(
            {
                "inventory": inventory.model_dump(),
                "symbol_graph": graph.model_dump(),
                "localization": localization.model_dump() if localization else None,
            },
            indent=2,
        )
    )


@app.command("run")
def run_command(
    repository: Annotated[
        Path, typer.Option("--repository", exists=True, file_okay=False, resolve_path=True)
    ],
    request: Annotated[str, typer.Option("--request")],
    provider: Annotated[str, typer.Option("--provider")] = "openai",
    model: Annotated[str, typer.Option("--model")] = "gpt-5.6-sol",
    script: Annotated[Path | None, typer.Option("--script", exists=True, dir_okay=False)] = None,
    executor: Annotated[str, typer.Option("--executor")] = "docker",
    output_dir: Annotated[Path | None, typer.Option("--output-dir")] = None,
) -> None:
    """Run the approval-gated workflow and retain evidence outside the repository."""
    if provider not in {"openai", "scripted"}:
        raise typer.BadParameter("provider must be openai or scripted", param_hint="--provider")
    if provider == "scripted" and script is None:
        typer.echo("--script is required for scripted provider")
        raise typer.Exit(2)
    if provider == "openai" and script is not None:
        typer.echo("--script is only supported with --provider scripted")
        raise typer.Exit(2)
    if executor not in {"docker", "local"}:
        raise typer.BadParameter("executor must be docker or local", param_hint="--executor")
    if repository.parent == repository:
        typer.echo("filesystem root repositories are unsupported")
        raise typer.Exit(2)

    evidence_dir = output_dir or repository.parent / ".repogent" / "runs"
    try:
        store = ArtifactStore.create(evidence_dir, repository, request)
    except (ArtifactStoreError, OSError) as error:
        typer.echo(f"could not create evidence directory: {error}")
        raise typer.Exit(2) from error

    policy = ValidationPolicy()
    command_executor = (
        DockerExecutor()
        if executor == "docker"
        else LocalExecutor(
            allowed={command.name: command.argv for command in policy.commands(repository)}
        )
    )
    preflight = Preflight(command_executor, policy).run(repository)
    store.write_model("preflight", preflight)
    for check in preflight.checks:
        if check.reason and check.status.value != "passed":
            typer.echo(f"{check.name}: {check.reason}")
    manifest = RunManifest(
        run_id=store.root.name,
        request=request,
        repository_fingerprint=preflight.repository_fingerprint,
        configuration_fingerprint=configuration_fingerprint(
            provider, model, executor, policy.commands(repository)
        ),
    )
    if not preflight.passed:
        manifest = manifest.model_copy(
            update={
                "status": RunStatus.HUMAN_INTERVENTION_REQUIRED,
                "stage": RunStage.FINISHED,
                "reason": "repository preflight failed",
            }
        )
        store.update_manifest(manifest)
        store.write_final("report.md", render_report(manifest, None, None, None, None))
        typer.echo(f"Evidence: {store.root}")
        raise typer.Exit(2)

    model_provider: ModelProvider
    if provider == "scripted":
        try:
            model_provider = ScriptedProvider.from_json(str(script))
        except (OSError, UnicodeError, json.JSONDecodeError, ProviderError) as error:
            typer.echo(f"could not load scripted provider: {error}")
            raise typer.Exit(2) from error
    else:
        try:
            model_provider = OpenAIProvider(model=model)
        except OpenAIError as error:
            typer.echo(f"could not load OpenAI provider: {error}")
            raise typer.Exit(2) from error
    workflow = Workflow(
        root=repository,
        request=request,
        manifest=manifest,
        roles=RoleSet.from_provider(model_provider),
        approver=CliApprover(),
        patch_policy=PatchPolicy(),
        patch_applier=PatchApplier(),
        validator=ValidationPipeline(command_executor, policy),
        artifacts=store,
        inspector=RepositoryInspector(),
        retriever=LexicalRetriever(),
        budget=Budget(),
        events=CompositeEventSink(
            (store.event_store(), ConsoleEventSink(typer.echo, store.secrets))
        ),
    )
    result = workflow.run()
    typer.echo(f"Run {result.run_id}: {result.status.value}")
    typer.echo(f"Evidence: {store.root}")
    if result.status not in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FINDINGS}:
        raise typer.Exit(2)
