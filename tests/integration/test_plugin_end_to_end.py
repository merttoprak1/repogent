from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from repogent.approval_gate import approval_digest
from repogent.domain import (
    ApprovalKind,
    CheckoutState,
    CheckStatus,
    Decision,
    ExecutionMode,
    FinalValidationStatus,
    IsolationLevel,
    RunStatus,
    TrustLabel,
    VerificationStatus,
)
from repogent.mcp_models import (
    ExecutionDecision,
    ExecutorOption,
    PendingExecutionChoice,
    RunDecision,
    RunSnapshot,
    RunStart,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def _stdio_session(
    *, env: dict[str, str] | None = None
) -> AsyncIterator[ClientSession]:
    # The MCP stdio client's own default (env=None) inherits only a narrow
    # allowlist (mcp.client.stdio.DEFAULT_INHERITED_ENV_VARS), which excludes
    # PYTHONPATH. Without it, the spawned subprocess resolves `repogent` via
    # whatever is otherwise importable (e.g. an unrelated editable install),
    # not the source tree under test. Default to the current process's full
    # environment so the subprocess reliably runs this worktree's code.
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "repogent.mcp_server"],
        env=dict(os.environ) if env is None else env,
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


def _copy_demo(tmp_path: Path, name: str = "target") -> Path:
    target = tmp_path / name
    shutil.copytree(Path("examples/fastapi_demo"), target)
    return target


def _start_request(
    target: Path,
    output_dir: Path,
    *,
    executor: str = "local",
    script: Path | None = None,
) -> RunStart:
    return RunStart(
        repository=target,
        request='Add a health endpoint that returns {"status": "ok"}',
        provider="scripted",
        script=script or Path("examples/scripted_run.json").resolve(),
        executor=executor,
        output_dir=output_dir,
    )


async def _snapshot_call(
    session: ClientSession, tool: str, arguments: dict[str, object]
) -> RunSnapshot:
    result = await session.call_tool(tool, arguments)
    assert result.isError is False, result.content
    assert result.structuredContent is not None
    return RunSnapshot.model_validate(result.structuredContent)


def _approval(snapshot: RunSnapshot, *, digest: str | None = None) -> dict[str, object]:
    pending = snapshot.pending_approval
    assert pending is not None
    decision = RunDecision(
        run_id=snapshot.run_id,
        kind=pending.kind,
        digest=digest or pending.digest,
        decision=Decision.APPROVED,
    )
    return {"decision": decision.model_dump(mode="json")}


def _execution_option(
    pending: PendingExecutionChoice, mode: ExecutionMode
) -> ExecutorOption:
    return next(item for item in pending.options if item.mode is mode)


def _execution_decision(
    snapshot: RunSnapshot,
    option: ExecutorOption,
    *,
    preview_digest: str | None = None,
    decision: Decision = Decision.APPROVED,
) -> dict[str, object]:
    pending = snapshot.pending_execution
    assert pending is not None
    execution_decision = ExecutionDecision(
        run_id=snapshot.run_id,
        preview_digest=preview_digest or pending.preview_digest,
        mode=option.mode,
        option_digest=option.option_digest,
        decision=decision,
    )
    return {"decision": execution_decision.model_dump(mode="json")}


def _manifest(snapshot: RunSnapshot) -> dict[str, object]:
    return json.loads(Path(snapshot.evidence_path, "run.json").read_text())


async def _select_local_until_patch_pending(
    session: ClientSession, snapshot: RunSnapshot
) -> RunSnapshot:
    # Candidate generation can produce more than one proposal (e.g. an
    # ambiguous-localization expansion gathers an alternative candidate for
    # comparison), and each generated candidate requires its own bounded
    # executor selection before the workflow can finalize a patch decision.
    while snapshot.pending_execution is not None:
        local = _execution_option(snapshot.pending_execution, ExecutionMode.LOCAL)
        snapshot = await _snapshot_call(
            session, "select_executor", _execution_decision(snapshot, local)
        )
    return snapshot


@pytest.mark.anyio
async def test_stdio_plugin_run_crosses_three_digest_gates_and_applies_exact_patch(
    tmp_path: Path,
) -> None:
    target = _copy_demo(tmp_path)
    expected_diff = json.loads(Path("examples/scripted_run.json").read_text())[2]["diff"]

    async with _stdio_session() as session:
        snapshot = await _snapshot_call(
            session,
            "start_run",
            {
                "request": _start_request(
                    target, tmp_path / "runs", executor="deferred"
                ).model_dump(mode="json")
            },
        )
        assert snapshot.pending_approval is not None
        assert snapshot.pending_approval.kind is ApprovalKind.REQUIREMENTS

        snapshot = await _snapshot_call(
            session, "approve_requirements", _approval(snapshot)
        )
        assert snapshot.pending_approval is not None
        assert snapshot.pending_approval.kind is ApprovalKind.PLAN

        snapshot = await _snapshot_call(session, "approve_plan", _approval(snapshot))
        assert snapshot.pending_approval is None
        assert snapshot.pending_execution is not None
        assert snapshot.verification_status is VerificationStatus.UNVALIDATED
        assert snapshot.trust_label is TrustLabel.UNVALIDATED
        # The unvalidated preview digest must differ from the final patch digest
        # the operator later approves; capture it to prove they are distinct.
        preview_digest = snapshot.pending_execution.preview_digest

        snapshot = await _select_local_until_patch_pending(session, snapshot)
        assert snapshot.pending_execution is None
        assert snapshot.execution_mode is ExecutionMode.LOCAL
        assert snapshot.isolation_level is IsolationLevel.REDUCED_ISOLATION
        assert snapshot.trust_label is TrustLabel.REDUCED_ISOLATION
        pending = snapshot.pending_approval
        assert pending is not None
        assert pending.kind is ApprovalKind.PATCH
        assert pending.digest != preview_digest
        assert isinstance(pending.artifact, dict)
        patch_artifact = pending.artifact
        selected = patch_artifact["selected_candidate"]
        assert selected["proposal"]["diff"] == expected_diff
        selected_summary = next(
            item for item in patch_artifact["candidates"] if item["selected"]
        )
        checks = selected_summary["checks"]
        assert checks == [
            {"name": "pytest", "status": CheckStatus.PASSED.value, "required": True},
            {"name": "ruff", "status": CheckStatus.PASSED.value, "required": False},
            {"name": "mypy", "status": CheckStatus.PASSED.value, "required": False},
            {"name": "bandit", "status": CheckStatus.PASSED.value, "required": False},
        ]
        assert selected_summary["skipped_checks"] == []
        serialized_summary = json.dumps(selected_summary)
        assert all(
            raw_field not in serialized_summary
            for raw_field in ('"argv"', '"stdout"', '"stderr"')
        )

        snapshot = await _snapshot_call(session, "approve_patch", _approval(snapshot))

    assert snapshot.status is RunStatus.COMPLETED
    assert snapshot.checkout_state is CheckoutState.APPLIED
    assert snapshot.final_validation_status is FinalValidationStatus.PASSED
    assert snapshot.trust_label is TrustLabel.REDUCED_ISOLATION
    assert '@app.get("/health")' in (target / "app.py").read_text()
    assert Path(snapshot.evidence_path, "report.md").is_file()
    manifest = _manifest(snapshot)
    assert manifest["checkout_state"] == CheckoutState.APPLIED.value
    assert manifest["final_validation_status"] == FinalValidationStatus.PASSED.value
    # Evidence must retain the preview, executor, and trust records for the run.
    assert manifest["preview_digest"] == preview_digest
    assert manifest["execution_mode"] == ExecutionMode.LOCAL.value
    assert manifest["isolation_level"] == IsolationLevel.REDUCED_ISOLATION.value
    assert manifest["verification_status"] == VerificationStatus.PASSED.value
    report_text = Path(snapshot.evidence_path, "report.md").read_text()
    assert TrustLabel.REDUCED_ISOLATION.value in report_text
    assert f"Execution mode: {ExecutionMode.LOCAL.value}" in report_text
    assert preview_digest in report_text


@pytest.mark.anyio
async def test_stdio_success_redacts_requirements_and_plan_before_digest_binding(
    tmp_path: Path,
) -> None:
    target = _copy_demo(tmp_path)
    scripted = json.loads(Path("examples/scripted_run.json").read_text())
    scripted[0]["objective"] = "keep token=sk-proj-1234567890abcdef private"
    scripted[0]["assumptions"] = ["password=correct-horse-battery-staple"]
    scripted[1]["security_considerations"] = [
        "password=do-not-show must stay private"
    ]
    script = tmp_path / "secret-script.json"
    script.write_text(json.dumps(scripted))

    async with _stdio_session() as session:
        requirements = await _snapshot_call(
            session,
            "start_run",
            {
                "request": _start_request(
                    target, tmp_path / "runs", script=script
                ).model_dump(mode="json")
            },
        )
        pending = requirements.pending_approval
        assert pending is not None
        serialized = json.dumps(pending.artifact)
        assert "sk-proj-1234567890abcdef" not in serialized
        assert "correct-horse-battery-staple" not in serialized
        assert pending.digest == approval_digest(
            ApprovalKind.REQUIREMENTS, json.dumps(pending.artifact)
        )

        plan = await _snapshot_call(
            session, "approve_requirements", _approval(requirements)
        )
        pending = plan.pending_approval
        assert pending is not None
        serialized = json.dumps(pending.artifact)
        assert "do-not-show" not in serialized
        assert pending.digest == approval_digest(
            ApprovalKind.PLAN, json.dumps(pending.artifact)
        )
        await _snapshot_call(session, "cancel_run", {"run_id": plan.run_id})


@pytest.mark.anyio
async def test_stale_patch_digest_cannot_mutate_checkout(tmp_path: Path) -> None:
    target = _copy_demo(tmp_path)
    async with _stdio_session() as session:
        snapshot = await _snapshot_call(
            session,
            "start_run",
            {"request": _start_request(target, tmp_path / "runs").model_dump(mode="json")},
        )
        snapshot = await _snapshot_call(
            session, "approve_requirements", _approval(snapshot)
        )
        snapshot = await _snapshot_call(session, "approve_plan", _approval(snapshot))

        stale = await session.call_tool(
            "approve_patch", _approval(snapshot, digest="0" * 64)
        )
        assert stale.isError is True
        observed = await _snapshot_call(session, "get_run", {"run_id": snapshot.run_id})
        assert observed.checkout_state is CheckoutState.NOT_APPLIED
        assert _manifest(observed)["checkout_state"] == CheckoutState.NOT_APPLIED.value
        assert '@app.get("/health")' not in (target / "app.py").read_text()
        await _snapshot_call(session, "cancel_run", {"run_id": snapshot.run_id})


@pytest.mark.anyio
async def test_second_run_on_same_root_is_locked_without_mutation(tmp_path: Path) -> None:
    target = _copy_demo(tmp_path)
    request = _start_request(target, tmp_path / "runs").model_dump(mode="json")
    async with _stdio_session() as session:
        first = await _snapshot_call(session, "start_run", {"request": request})
        second = await session.call_tool("start_run", {"request": request})
        assert second.isError is True
        observed = await _snapshot_call(session, "get_run", {"run_id": first.run_id})
        assert observed.checkout_state is CheckoutState.NOT_APPLIED
        assert _manifest(observed)["checkout_state"] == CheckoutState.NOT_APPLIED.value
        await _snapshot_call(session, "cancel_run", {"run_id": first.run_id})


@pytest.mark.anyio
async def test_cancel_before_patch_records_not_applied_checkout(tmp_path: Path) -> None:
    target = _copy_demo(tmp_path)
    async with _stdio_session() as session:
        started = await _snapshot_call(
            session,
            "start_run",
            {
                "request": _start_request(
                    target, tmp_path / "runs"
                ).model_dump(mode="json")
            },
        )
        cancelled = await _snapshot_call(
            session, "cancel_run", {"run_id": started.run_id}
        )

    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.checkout_state is CheckoutState.NOT_APPLIED
    assert _manifest(cancelled)["checkout_state"] == CheckoutState.NOT_APPLIED.value
    assert '@app.get("/health")' not in (target / "app.py").read_text()


@pytest.mark.anyio
async def test_missing_docker_fails_closed_without_local_fallback(tmp_path: Path) -> None:
    target = _copy_demo(tmp_path)
    output_dir = tmp_path / "runs"
    server_env = dict(os.environ)
    server_env["PATH"] = str(tmp_path / "empty-bin")

    async with _stdio_session(env=server_env) as session:
        result = await session.call_tool(
            "start_run",
            {
                "request": _start_request(
                    target, output_dir, executor="docker"
                ).model_dump(mode="json")
            },
        )

    assert result.isError is True
    run_directories = list(output_dir.glob("run-*"))
    assert len(run_directories) == 1
    manifest = json.loads((run_directories[0] / "run.json").read_text())
    preflight = json.loads(next(run_directories[0].glob("preflight-*.json")).read_text())
    executor_check = next(check for check in preflight["checks"] if check["name"] == "executor")
    assert executor_check["status"] == "failed"
    assert manifest["status"] == RunStatus.HUMAN_INTERVENTION_REQUIRED.value
    assert manifest["checkout_state"] == CheckoutState.NOT_APPLIED.value
    assert '@app.get("/health")' not in (target / "app.py").read_text()


@pytest.mark.anyio
async def test_deferred_run_reaches_preview_when_docker_is_absent(
    tmp_path: Path,
) -> None:
    target = _copy_demo(tmp_path)
    output_dir = tmp_path / "runs"
    server_env = dict(os.environ)
    server_env["PATH"] = str(tmp_path / "empty-bin")

    async with _stdio_session(env=server_env) as session:
        snapshot = await _snapshot_call(
            session,
            "start_run",
            {
                "request": _start_request(
                    target, output_dir, executor="deferred"
                ).model_dump(mode="json")
            },
        )
        snapshot = await _snapshot_call(
            session, "approve_requirements", _approval(snapshot)
        )
        snapshot = await _snapshot_call(session, "approve_plan", _approval(snapshot))

        pending = snapshot.pending_execution
        assert pending is not None
        docker_option = _execution_option(pending, ExecutionMode.DOCKER)
        local_option = _execution_option(pending, ExecutionMode.LOCAL)
        assert docker_option.available is False
        assert local_option.available is True

        await _snapshot_call(session, "cancel_run", {"run_id": snapshot.run_id})

    assert '@app.get("/health")' not in (target / "app.py").read_text()


@pytest.mark.anyio
async def test_stale_execution_preview_digest_cannot_select_executor(
    tmp_path: Path,
) -> None:
    target = _copy_demo(tmp_path)
    async with _stdio_session() as session:
        snapshot = await _snapshot_call(
            session,
            "start_run",
            {
                "request": _start_request(
                    target, tmp_path / "runs", executor="deferred"
                ).model_dump(mode="json")
            },
        )
        snapshot = await _snapshot_call(
            session, "approve_requirements", _approval(snapshot)
        )
        snapshot = await _snapshot_call(session, "approve_plan", _approval(snapshot))

        pending = snapshot.pending_execution
        assert pending is not None
        local = _execution_option(pending, ExecutionMode.LOCAL)
        stale = await session.call_tool(
            "select_executor",
            _execution_decision(snapshot, local, preview_digest="0" * 64),
        )
        assert stale.isError is True

        observed = await _snapshot_call(session, "get_run", {"run_id": snapshot.run_id})
        assert observed.pending_execution is not None
        assert observed.pending_execution.preview_digest == pending.preview_digest
        assert observed.checkout_state is CheckoutState.NOT_APPLIED
        assert '@app.get("/health")' not in (target / "app.py").read_text()
        await _snapshot_call(session, "cancel_run", {"run_id": snapshot.run_id})


@pytest.mark.anyio
async def test_deferred_run_never_selects_local_without_a_tool_call(
    tmp_path: Path,
) -> None:
    target = _copy_demo(tmp_path)
    async with _stdio_session() as session:
        snapshot = await _snapshot_call(
            session,
            "start_run",
            {
                "request": _start_request(
                    target, tmp_path / "runs", executor="deferred"
                ).model_dump(mode="json")
            },
        )
        snapshot = await _snapshot_call(
            session, "approve_requirements", _approval(snapshot)
        )
        snapshot = await _snapshot_call(session, "approve_plan", _approval(snapshot))
        assert snapshot.pending_execution is not None
        assert snapshot.execution_mode is None

        observed = await _snapshot_call(session, "get_run", {"run_id": snapshot.run_id})
        assert observed.pending_execution is not None
        assert observed.execution_mode is None
        assert observed.checkout_state is CheckoutState.NOT_APPLIED
        assert '@app.get("/health")' not in (target / "app.py").read_text()

        await _snapshot_call(session, "cancel_run", {"run_id": snapshot.run_id})


@pytest.mark.anyio
async def test_selecting_executor_does_not_apply_the_patch(tmp_path: Path) -> None:
    target = _copy_demo(tmp_path)
    async with _stdio_session() as session:
        snapshot = await _snapshot_call(
            session,
            "start_run",
            {
                "request": _start_request(
                    target, tmp_path / "runs", executor="deferred"
                ).model_dump(mode="json")
            },
        )
        snapshot = await _snapshot_call(
            session, "approve_requirements", _approval(snapshot)
        )
        snapshot = await _snapshot_call(session, "approve_plan", _approval(snapshot))
        assert snapshot.pending_execution is not None

        snapshot = await _select_local_until_patch_pending(session, snapshot)

        assert snapshot.checkout_state is CheckoutState.NOT_APPLIED
        assert snapshot.selected_patch_applied is False
        assert '@app.get("/health")' not in (target / "app.py").read_text()
        assert snapshot.pending_approval is not None
        assert snapshot.pending_approval.kind is ApprovalKind.PATCH

        await _snapshot_call(session, "cancel_run", {"run_id": snapshot.run_id})

    assert '@app.get("/health")' not in (target / "app.py").read_text()


@pytest.mark.anyio
async def test_cancel_during_pending_execution_records_not_applied_checkout(
    tmp_path: Path,
) -> None:
    target = _copy_demo(tmp_path)
    async with _stdio_session() as session:
        snapshot = await _snapshot_call(
            session,
            "start_run",
            {
                "request": _start_request(
                    target, tmp_path / "runs", executor="deferred"
                ).model_dump(mode="json")
            },
        )
        snapshot = await _snapshot_call(
            session, "approve_requirements", _approval(snapshot)
        )
        snapshot = await _snapshot_call(session, "approve_plan", _approval(snapshot))
        assert snapshot.pending_execution is not None

        cancelled = await _snapshot_call(
            session, "cancel_run", {"run_id": snapshot.run_id}
        )

    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.checkout_state is CheckoutState.NOT_APPLIED
    assert _manifest(cancelled)["checkout_state"] == CheckoutState.NOT_APPLIED.value
    assert '@app.get("/health")' not in (target / "app.py").read_text()


@pytest.mark.anyio
async def test_second_run_is_locked_while_executor_selection_is_pending(
    tmp_path: Path,
) -> None:
    target = _copy_demo(tmp_path)
    request = _start_request(
        target, tmp_path / "runs", executor="deferred"
    ).model_dump(mode="json")
    async with _stdio_session() as session:
        snapshot = await _snapshot_call(session, "start_run", {"request": request})
        snapshot = await _snapshot_call(
            session, "approve_requirements", _approval(snapshot)
        )
        snapshot = await _snapshot_call(session, "approve_plan", _approval(snapshot))
        assert snapshot.pending_execution is not None

        second = await session.call_tool("start_run", {"request": request})
        assert second.isError is True

        observed = await _snapshot_call(session, "get_run", {"run_id": snapshot.run_id})
        assert observed.checkout_state is CheckoutState.NOT_APPLIED
        await _snapshot_call(session, "cancel_run", {"run_id": snapshot.run_id})


@pytest.mark.anyio
async def test_no_target_code_executes_before_executor_selection(
    tmp_path: Path,
) -> None:
    target = _copy_demo(tmp_path)
    # A canary test fires only if the target's suite is imported or collected.
    # Its module-level side effect writes a sentinel to an absolute path outside
    # the checkout; static inventory/graph/localization only AST-parse files and
    # never execute them, so the sentinel proves whether target code has run.
    sentinel = tmp_path / "canary-fired"
    (target / "tests" / "test_canary.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('fired')\n"
        "def test_canary() -> None:\n"
        "    assert True\n"
    )
    async with _stdio_session() as session:
        snapshot = await _snapshot_call(
            session,
            "start_run",
            {
                "request": _start_request(
                    target, tmp_path / "runs", executor="deferred"
                ).model_dump(mode="json")
            },
        )
        snapshot = await _snapshot_call(
            session, "approve_requirements", _approval(snapshot)
        )
        snapshot = await _snapshot_call(session, "approve_plan", _approval(snapshot))
        assert snapshot.pending_execution is not None

        # The run is paused at the unvalidated preview with no executor selected;
        # nothing may have executed the target repository's code yet.
        assert not sentinel.exists()

        await _snapshot_call(session, "cancel_run", {"run_id": snapshot.run_id})

    assert not sentinel.exists()


@pytest.mark.anyio
async def test_disconnect_at_pending_execution_preserves_not_applied_evidence(
    tmp_path: Path,
) -> None:
    target = _copy_demo(tmp_path)
    request = _start_request(
        target, tmp_path / "runs", executor="deferred"
    ).model_dump(mode="json")
    async with _stdio_session() as session:
        snapshot = await _snapshot_call(session, "start_run", {"request": request})
        snapshot = await _snapshot_call(
            session, "approve_requirements", _approval(snapshot)
        )
        snapshot = await _snapshot_call(session, "approve_plan", _approval(snapshot))
        assert snapshot.pending_execution is not None
        evidence_path = Path(snapshot.evidence_path)
        # Leave without cancelling: exiting the context tears down the stdio
        # subprocess, exercising the bounded disconnect finalizer.

    # A later client can only inspect persisted evidence; the durable checkout
    # state stays authoritative and no mutation happened during the disconnect.
    manifest = json.loads((evidence_path / "run.json").read_text())
    assert manifest["checkout_state"] == CheckoutState.NOT_APPLIED.value
    assert '@app.get("/health")' not in (target / "app.py").read_text()
