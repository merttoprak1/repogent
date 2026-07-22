from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import typer

from repogent.agents import RoleSet
from repogent.approvals import Approver
from repogent.artifacts import ArtifactStore
from repogent.codex_cli import CodexCliProvider
from repogent.domain import (
    Budget,
    EventKind,
    ProviderReadiness,
    RunEvent,
    RunManifest,
    RunStage,
    RunStatus,
)
from repogent.events import EventSink
from repogent.execution import DockerExecutor, LocalExecutor, ValidationPolicy
from repogent.patching import PatchApplier, PatchPolicy
from repogent.preflight import Preflight, PreflightReport, configuration_fingerprint
from repogent.providers import ModelProvider, OpenAIProvider, ProviderError, ScriptedProvider
from repogent.reporting import render_report
from repogent.repository import LexicalRetriever, RepositoryInspector
from repogent.validation import ValidationPipeline
from repogent.workflow import Workflow

ProviderName = Literal["openai", "codex-cli", "scripted"]
ExecutorName = Literal["docker", "local"]


@dataclass(frozen=True)
class RunOptions:
    repository: Path
    request: str
    provider: str = "openai"
    model: str | None = None
    script: Path | None = None
    executor: str = "docker"
    output_dir: Path | None = None


@dataclass(frozen=True)
class PreparedRun:
    store: ArtifactStore
    manifest: RunManifest
    workflow: Workflow
    approver: Approver
    preflight: PreflightReport
    provider_readiness: ProviderReadiness | None = None


class RunBuildError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        store: ArtifactStore | None = None,
        manifest: RunManifest | None = None,
    ) -> None:
        super().__init__(message)
        self.store = store
        self.manifest = manifest


class _RunConstructionError(RunBuildError):
    pass


def validate_run_options(options: RunOptions) -> None:
    if options.provider not in {"openai", "codex-cli", "scripted"}:
        raise ValueError("provider must be openai, codex-cli, or scripted")
    if options.provider == "scripted" and options.script is None:
        raise ValueError("--script is required for scripted provider")
    if options.provider != "scripted" and options.script is not None:
        raise ValueError("--script is only supported with --provider scripted")
    if options.executor not in {"docker", "local"}:
        raise ValueError("executor must be docker or local")
    repository = options.repository.resolve(strict=True)
    if repository.parent == repository:
        raise ValueError("filesystem root repositories are unsupported")
    if not repository.is_dir():
        raise ValueError("repository must be a directory")


def build_run(
    options: RunOptions,
    approver_factory: Callable[[str], Approver],
    *,
    events: EventSink | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> PreparedRun:
    validate_run_options(options)
    repository = options.repository.resolve(strict=True)
    evidence_dir = options.output_dir or repository.parent / ".repogent" / "runs"
    store = ArtifactStore.create(evidence_dir, repository, options.request)
    manifest = RunManifest(
        run_id=store.root.name,
        request=options.request,
        events_file="events.jsonl",
    )

    try:
        effective_model = options.model or {
            "openai": "gpt-5.6-sol",
            "codex-cli": "default",
            "scripted": "scripted",
        }[options.provider]
        policy = ValidationPolicy()
        commands = policy.commands(repository)
        manifest = manifest.model_copy(
            update={
                "configuration_fingerprint": configuration_fingerprint(
                    options.provider, effective_model, options.executor, commands
                )
            }
        )
        command_executor = (
            DockerExecutor()
            if options.executor == "docker"
            else LocalExecutor(
                allowed={command.name: command.argv for command in commands}
            )
        )
        preflight = Preflight(command_executor, policy).run(repository)
        store.write_model("preflight", preflight)
    except (KeyboardInterrupt, SystemExit) as error:
        terminal = terminalize_failure(
            store,
            manifest,
            "workflow interrupted by user",
            RunStatus.CANCELLED,
        )
        raise RunBuildError(
            "workflow interrupted by user", store=store, manifest=terminal
        ) from error
    except Exception as error:
        reason = f"repository preflight failed: {error}"
        terminal = terminalize_failure(store, manifest, reason)
        raise RunBuildError(reason, store=store, manifest=terminal) from error

    manifest = manifest.model_copy(
        update={"repository_fingerprint": preflight.repository_fingerprint}
    )
    if not preflight.passed:
        terminal = terminalize_failure(store, manifest, "repository preflight failed")
        raise RunBuildError(
            "repository preflight failed",
            store=store,
            manifest=terminal,
        )

    readiness: ProviderReadiness | None = None
    try:
        model_provider: ModelProvider
        if options.provider == "scripted":
            model_provider = ScriptedProvider.from_json(
                str(cast(Path, options.script))
            )
        elif options.provider == "codex-cli":
            codex_provider = CodexCliProvider(
                model=options.model, target_root=repository
            )
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
    except (KeyboardInterrupt, SystemExit) as error:
        terminal = terminalize_failure(
            store,
            manifest,
            "workflow interrupted by user",
            RunStatus.CANCELLED,
        )
        raise RunBuildError(
            "workflow interrupted by user", store=store, manifest=terminal
        ) from error
    except Exception as error:
        label = {
            "scripted": "scripted provider",
            "codex-cli": "Codex CLI provider",
            "openai": "OpenAI provider",
        }[options.provider]
        reason = f"could not load {label}: {error}"
        terminal = terminalize_failure(store, manifest, reason)
        raise RunBuildError(reason, store=store, manifest=terminal) from error

    try:
        approver = approver_factory(manifest.run_id)
        workflow = Workflow(
            root=repository,
            request=options.request,
            manifest=manifest,
            roles=RoleSet.from_provider(model_provider),
            approver=approver,
            patch_policy=PatchPolicy(),
            patch_applier=PatchApplier(),
            validator=ValidationPipeline(command_executor, policy),
            artifacts=store,
            inspector=RepositoryInspector(),
            retriever=LexicalRetriever(),
            budget=Budget(),
            events=events or store.event_store(),
            cancel_requested=cancel_requested,
        )
    except (KeyboardInterrupt, SystemExit) as error:
        terminal = terminalize_failure(
            store,
            manifest,
            "workflow interrupted by user",
            RunStatus.CANCELLED,
        )
        raise _RunConstructionError(
            "workflow interrupted by user", store=store, manifest=terminal
        ) from error
    except Exception as error:
        terminal = terminalize_failure(store, manifest, str(error))
        raise _RunConstructionError(
            str(error), store=store, manifest=terminal
        ) from error

    return PreparedRun(
        store=store,
        manifest=manifest,
        workflow=workflow,
        approver=approver,
        preflight=preflight,
        provider_readiness=readiness,
    )


def terminalize_failure(
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
            with events_path.open(encoding="utf-8") as event_log:
                for line in event_log:
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
