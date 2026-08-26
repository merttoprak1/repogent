from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from repogent import __version__
from repogent.approvals import CliApprover, ReplayApprover
from repogent.artifacts import ArtifactStoreError
from repogent.demo import DemoError, bundled_scripted_run, copy_demo_repository
from repogent.doctor import DoctorService
from repogent.domain import RunEvent, RunStatus
from repogent.events import CompositeEventSink, ConsoleEventSink, EventSink
from repogent.localization import PythonLocalizer
from repogent.mcp_models import DoctorReport, DoctorRequest
from repogent.preflight import PreflightReport
from repogent.providers import KNOWN_PROVIDERS
from repogent.repository import RepositoryInspector
from repogent.run_builder import (
    RunBuildError,
    RunOptions,
    _RunConstructionError,
    build_run,
    terminalize_failure,
    validate_run_options,
)
from repogent.run_reports import ReportReadError, read_report_markdown
from repogent.symbols import PythonSymbolGraphBuilder


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"repogent {__version__}")
        raise typer.Exit()


app = typer.Typer(no_args_is_help=True)


@app.callback()
def _cli(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the Repogent version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Approval-gated, evidence-backed Python repository changes."""


@app.command("demo")
def demo_command(
    output_dir: Annotated[Path | None, typer.Option("--output-dir")] = None,
) -> None:
    """Run a labeled scripted replay against a disposable copy of the bundled fixture."""
    typer.echo("REPLAY: this demo uses checked-in scripted artifacts. It is not a live model call.")
    typer.echo(
        "It copies a bundled fixture to a disposable directory and auto-approves "
        "those recorded artifacts."
    )
    try:
        work = Path(tempfile.mkdtemp(prefix="repogent-demo-"))
        repository = copy_demo_repository(work / "repository")
        script = bundled_scripted_run()
    except DemoError as error:
        typer.echo(str(error))
        raise typer.Exit(2) from error
    evidence = output_dir or work / "runs"
    options = RunOptions(
        repository=repository,
        request="Add a health endpoint",
        provider="scripted",
        script=script,
        executor="local",
        output_dir=evidence,
    )
    cli_events = _DeferredEventSink()
    try:
        prepared = build_run(
            options,
            lambda _run_id: ReplayApprover(),
            events=cli_events,
        )
    except (ArtifactStoreError, OSError) as error:
        typer.echo(f"could not create evidence directory: {error}")
        raise typer.Exit(2) from error
    except RunBuildError as error:
        typer.echo(str(error))
        if error.store is not None:
            typer.echo(f"Evidence: {error.store.root}")
        raise typer.Exit(2) from error

    store = prepared.store
    cli_events.bind(
        CompositeEventSink((store.event_store(), ConsoleEventSink(typer.echo, store.secrets)))
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
        result = _terminalize_cli_failure(store, prepared.workflow.manifest, str(error))
    typer.echo(f"Run {result.run_id}: {result.status.value}")
    typer.echo(f"Evidence: {store.root}")
    if result.status not in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FINDINGS}:
        raise typer.Exit(2)


@app.command("report")
def report_command(
    run_directory: Annotated[Path, typer.Argument(exists=True, file_okay=False, resolve_path=True)],
) -> None:
    """Print the markdown report from a run evidence directory."""
    try:
        typer.echo(read_report_markdown(run_directory), nl=False)
    except ReportReadError as error:
        typer.echo(str(error))
        raise typer.Exit(2) from error


@app.command("mcp")
def mcp_command(
    stdio: Annotated[bool, typer.Option("--stdio")] = False,
) -> None:
    """Serve Repogent's local MCP tools over stdio."""
    if not stdio:
        raise typer.BadParameter("only --stdio is supported")
    from repogent.mcp_server import serve_stdio

    serve_stdio()


@app.command("doctor")
def doctor_command(
    repository: Annotated[Path, typer.Argument(exists=True, file_okay=False, resolve_path=True)],
    provider: Annotated[str, typer.Option("--provider")] = "codex-cli",
    model: Annotated[str | None, typer.Option("--model")] = None,
    executor: Annotated[str, typer.Option("--executor")] = "deferred",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Diagnose repository, provider, and executor readiness without editing files."""
    try:
        request = DoctorRequest(
            repository=repository,
            provider=provider,
            model=model,
            executor=executor,
        )
    except ValidationError as error:
        typer.echo(error.errors()[0]["msg"] if error.errors() else str(error))
        raise typer.Exit(2) from error

    report = DoctorService().run(request)
    if as_json:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        typer.echo(_render_doctor_report(report))
    if not report.ready:
        raise typer.Exit(2)


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
    provider: Annotated[str, typer.Option("--provider")] = "codex-cli",
    model: Annotated[str | None, typer.Option("--model")] = None,
    script: Annotated[Path | None, typer.Option("--script", exists=True, dir_okay=False)] = None,
    executor: Annotated[str, typer.Option("--executor")] = "docker",
    output_dir: Annotated[Path | None, typer.Option("--output-dir")] = None,
) -> None:
    """Run the approval-gated workflow and retain evidence outside the repository."""
    if executor == "deferred":
        # `deferred` is an internal-only executor value used by the session/plugin
        # path, which always supplies an executor_selector_factory. The CLI has no
        # way to prompt for executor selection, so reject it as an invalid choice
        # instead of letting build_run raise an uncaught ValueError.
        raise typer.BadParameter("executor must be docker or local", param_hint="--executor")
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
        if provider not in KNOWN_PROVIDERS:
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
        if isinstance(error, _RunConstructionError) and error.manifest is not None:
            typer.echo(f"Run {error.manifest.run_id}: {error.manifest.status.value}")
        elif str(error) == "repository preflight failed":
            _echo_preflight_failures(error.store.root)
        elif error.manifest is None or error.manifest.status is not RunStatus.CANCELLED:
            typer.echo(str(error))
        typer.echo(f"Evidence: {error.store.root}")
        raise typer.Exit(2) from error

    store = prepared.store
    cli_events.bind(
        CompositeEventSink((store.event_store(), ConsoleEventSink(typer.echo, store.secrets)))
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
        result = _terminalize_cli_failure(store, prepared.workflow.manifest, str(error))
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


def _render_doctor_report(report: DoctorReport) -> str:
    if report.ready and report.degraded:
        status = "READY (degraded)"
    elif report.ready:
        status = "READY"
    else:
        status = "BLOCKED"
    lines = [
        status,
        "",
        f"repository: {report.repository}",
        f"provider: {report.provider}",
        f"executor: {report.executor}",
        "",
        "checks:",
    ]
    for check in report.checks:
        mark = "ok" if check.passed else "fail"
        lines.append(f"  [{mark}] {check.name}: {check.message}")
        if check.remediation:
            lines.append(f"         {check.remediation}")
    if report.executors:
        lines.extend(["", "executors:"])
        for option in report.executors:
            availability = "available" if option.available else "unavailable"
            lines.append(f"  {option.mode.value}: {availability} ({option.isolation_level.value})")
            if option.remediation:
                lines.append(f"         {option.remediation}")
    if report.degraded_reasons:
        lines.extend(["", "degraded:"])
        lines.extend(f"  {reason}" for reason in report.degraded_reasons)
    lines.extend(["", f"next: {_doctor_next_action(report)}"])
    return "\n".join(lines)


def _doctor_next_action(report: DoctorReport) -> str:
    failed = [check for check in report.checks if check.required and not check.passed]
    if failed:
        return failed[0].remediation or "Inspect the failed checks above."
    if report.executor == "deferred":
        return (
            "Install the Codex plugin and start a verified change; "
            "choose Docker or local when selecting an executor."
        )
    return "Start a verified change with the selected executor."


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
