from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from repogent.approvals import CliApprover
from repogent.artifacts import ArtifactStoreError
from repogent.domain import RunEvent, RunStatus
from repogent.events import CompositeEventSink, ConsoleEventSink, EventSink
from repogent.localization import PythonLocalizer
from repogent.preflight import PreflightReport
from repogent.repository import RepositoryInspector
from repogent.run_builder import (
    RunBuildError,
    RunOptions,
    build_run,
    terminalize_failure,
    validate_run_options,
)
from repogent.symbols import PythonSymbolGraphBuilder

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
    options = RunOptions(
        repository=repository,
        request=request,
        provider=provider,
        model=model,
        script=script,
        executor=executor,
        output_dir=output_dir,
    )
    try:
        validate_run_options(options)
    except ValueError as error:
        if provider not in {"openai", "codex-cli", "scripted"}:
            raise typer.BadParameter(str(error), param_hint="--provider") from error
        if executor not in {"docker", "local"}:
            raise typer.BadParameter(str(error), param_hint="--executor") from error
        typer.echo(str(error))
        raise typer.Exit(2) from error

    cli_events = _DeferredEventSink()
    try:
        prepared = build_run(
            options,
            lambda _run_id: CliApprover(),
            events=cli_events,
        )
    except (ArtifactStoreError, OSError) as error:
        typer.echo(f"could not create evidence directory: {error}")
        raise typer.Exit(2) from error
    except RunBuildError as error:
        if error.store is None:
            typer.echo(str(error))
            raise typer.Exit(2) from error
        if str(error) == "repository preflight failed":
            _echo_preflight_failures(error.store.root)
        elif (
            error.manifest is not None
            and error.manifest.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
            and not str(error).startswith(
                ("repository preflight failed:", "could not load ")
            )
        ):
            typer.echo(f"Run {error.manifest.run_id}: {error.manifest.status.value}")
        elif error.manifest is None or error.manifest.status is not RunStatus.CANCELLED:
            typer.echo(str(error))
        typer.echo(f"Evidence: {error.store.root}")
        raise typer.Exit(2) from error

    store = prepared.store
    cli_events.bind(
        CompositeEventSink(
            (store.event_store(), ConsoleEventSink(typer.echo, store.secrets))
        )
    )
    try:
        result = prepared.workflow.run()
    except (KeyboardInterrupt, SystemExit):
        result = _terminalize_cli_failure(
            store,
            prepared.workflow.manifest,
            "workflow interrupted by user",
            RunStatus.CANCELLED,
        )
    except Exception as error:
        result = _terminalize_cli_failure(
            store, prepared.workflow.manifest, str(error)
        )
    typer.echo(f"Run {result.run_id}: {result.status.value}")
    typer.echo(f"Evidence: {store.root}")
    if result.status not in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FINDINGS}:
        raise typer.Exit(2)


_terminalize_cli_failure = terminalize_failure


class _DeferredEventSink:
    def __init__(self) -> None:
        self._delegate: EventSink | None = None

    def bind(self, delegate: EventSink) -> None:
        self._delegate = delegate

    def emit(self, event: RunEvent) -> None:
        if self._delegate is None:
            raise RuntimeError("CLI event sink is not bound")
        self._delegate.emit(event)


def _echo_preflight_failures(run_directory: Path) -> None:
    artifacts = sorted(run_directory.glob("preflight-*.json"))
    if not artifacts:
        return
    payload = json.loads(artifacts[-1].read_text())
    payload.pop("passed", None)
    preflight = PreflightReport.model_validate(payload)
    for check in preflight.checks:
        if check.reason and check.status.value != "passed":
            typer.echo(f"{check.name}: {check.reason}")
