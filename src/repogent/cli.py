from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, cast

import typer

from repogent.agents import RoleSet
from repogent.approvals import CliApprover
from repogent.artifacts import ArtifactStore, ArtifactStoreError
from repogent.codex_cli import CodexCliProvider
from repogent.domain import Budget, EventKind, RunEvent, RunManifest, RunStage, RunStatus
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
    model: Annotated[str | None, typer.Option("--model")] = None,
    script: Annotated[Path | None, typer.Option("--script", exists=True, dir_okay=False)] = None,
    executor: Annotated[str, typer.Option("--executor")] = "docker",
    output_dir: Annotated[Path | None, typer.Option("--output-dir")] = None,
) -> None:
    """Run the approval-gated workflow and retain evidence outside the repository."""
    if provider not in {"openai", "codex-cli", "scripted"}:
        raise typer.BadParameter(
            "provider must be openai, codex-cli, or scripted", param_hint="--provider"
        )
    if provider == "scripted" and script is None:
        typer.echo("--script is required for scripted provider")
        raise typer.Exit(2)
    if provider != "scripted" and script is not None:
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

    manifest = RunManifest(
        run_id=store.root.name,
        request=request,
        events_file="events.jsonl",
    )
    effective_model = model or {
        "openai": "gpt-5.6-sol",
        "codex-cli": "default",
        "scripted": "scripted",
    }[provider]
    try:
        policy = ValidationPolicy()
        commands = policy.commands(repository)
        manifest = manifest.model_copy(
            update={
                "configuration_fingerprint": configuration_fingerprint(
                    provider, effective_model, executor, commands
                )
            }
        )
        command_executor = (
            DockerExecutor()
            if executor == "docker"
            else LocalExecutor(
                allowed={command.name: command.argv for command in commands}
            )
        )
        preflight = Preflight(command_executor, policy).run(repository)
        store.write_model("preflight", preflight)
    except KeyboardInterrupt:
        _terminalize_cli_failure(
            store, manifest, "workflow interrupted by user", RunStatus.CANCELLED
        )
        typer.echo(f"Evidence: {store.root}")
        raise typer.Exit(2) from None
    except Exception as error:
        _terminalize_cli_failure(store, manifest, f"repository preflight failed: {error}")
        typer.echo(f"repository preflight failed: {error}")
        typer.echo(f"Evidence: {store.root}")
        raise typer.Exit(2) from error
    for check in preflight.checks:
        if check.reason and check.status.value != "passed":
            typer.echo(f"{check.name}: {check.reason}")
    manifest = manifest.model_copy(
        update={"repository_fingerprint": preflight.repository_fingerprint}
    )
    if not preflight.passed:
        _terminalize_cli_failure(
            store,
            manifest,
            "repository preflight failed",
        )
        typer.echo(f"Evidence: {store.root}")
        raise typer.Exit(2)

    try:
        model_provider: ModelProvider
        if provider == "scripted":
            model_provider = ScriptedProvider.from_json(str(script))
        elif provider == "codex-cli":
            codex_provider = CodexCliProvider(model=model)
            readiness = codex_provider.check_ready()
            store.write_model("provider-readiness", readiness)
            if not readiness.ready:
                reason = readiness.reason or "Codex CLI is not ready"
                if "codex login" not in reason.lower():
                    reason += "; run `codex login` to authenticate"
                raise ProviderError(reason, retryable=False)
            model_provider = cast(ModelProvider, codex_provider)
        else:
            model_provider = OpenAIProvider(model=effective_model)
    except KeyboardInterrupt:
        _terminalize_cli_failure(
            store, manifest, "workflow interrupted by user", RunStatus.CANCELLED
        )
        typer.echo(f"Evidence: {store.root}")
        raise typer.Exit(2) from None
    except Exception as error:
        label = {
            "scripted": "scripted provider",
            "codex-cli": "Codex CLI provider",
            "openai": "OpenAI provider",
        }[provider]
        reason = f"could not load {label}: {error}"
        _terminalize_cli_failure(store, manifest, reason)
        typer.echo(reason)
        typer.echo(f"Evidence: {store.root}")
        raise typer.Exit(2) from error
    workflow: Workflow | None = None
    try:
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
    except KeyboardInterrupt:
        result = _terminalize_cli_failure(
            store,
            workflow.manifest if workflow is not None else manifest,
            "workflow interrupted by user",
            RunStatus.CANCELLED,
        )
    except Exception as error:
        result = _terminalize_cli_failure(
            store, workflow.manifest if workflow is not None else manifest, str(error)
        )
    typer.echo(f"Run {result.run_id}: {result.status.value}")
    typer.echo(f"Evidence: {store.root}")
    if result.status not in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FINDINGS}:
        raise typer.Exit(2)


def _terminalize_cli_failure(
    store: ArtifactStore,
    manifest: RunManifest,
    reason: str,
    status: RunStatus = RunStatus.HUMAN_INTERVENTION_REQUIRED,
) -> RunManifest:
    terminal = manifest.model_copy(
        update={"status": status, "stage": RunStage.FINISHED, "reason": reason}
    )
    store.update_manifest(terminal)
    store.write_final("report.md", render_report(terminal, None, None, None, None))
    try:
        sequence = 1
        events_path = store.root / "events.jsonl"
        if events_path.exists():
            last_line = ""
            with events_path.open(encoding="utf-8") as events:
                for line in events:
                    if line.strip():
                        last_line = line
            if last_line:
                sequence = int(json.loads(last_line)["sequence"]) + 1
        store.event_store().emit(
            RunEvent(
                run_id=terminal.run_id,
                sequence=sequence,
                kind=EventKind.TERMINAL,
                stage=RunStage.FINISHED.value,
                message="workflow finished",
                data={"status": terminal.status.value, "reason": terminal.reason},
            )
        )
    except (Exception, KeyboardInterrupt, SystemExit):
        typer.echo("warning: terminal event could not be written")
    return terminal
