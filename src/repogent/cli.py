from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from openai import OpenAIError

from repogent.agents import RoleSet
from repogent.approvals import CliApprover
from repogent.artifacts import ArtifactStore, ArtifactStoreError
from repogent.domain import Budget, RunManifest, RunStatus
from repogent.execution import DockerExecutor, LocalExecutor, ValidationPolicy
from repogent.patching import PatchApplier, PatchPolicy
from repogent.providers import ModelProvider, OpenAIProvider, ProviderError, ScriptedProvider
from repogent.repository import LexicalRetriever, RepositoryInspector
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
    """Print a read-only repository inventory and request-ranked context as JSON."""
    inventory = RepositoryInspector().inspect(repository)
    context = LexicalRetriever().retrieve(inventory, request) if request else []
    typer.echo(
        json.dumps(
            {
                "inventory": inventory.model_dump(),
                "context": [item.model_dump() for item in context],
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
    policy = ValidationPolicy()
    command_executor = (
        DockerExecutor()
        if executor == "docker"
        else LocalExecutor(
            allowed={command.name: command.argv for command in policy.commands(repository)}
        )
    )
    manifest = RunManifest(run_id=store.root.name, request=request)
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
    )
    result = workflow.run()
    typer.echo(f"Run {result.run_id}: {result.status.value}")
    typer.echo(f"Evidence: {store.root}")
    if result.status not in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FINDINGS}:
        raise typer.Exit(2)
