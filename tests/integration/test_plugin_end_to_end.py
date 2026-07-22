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

from repogent.domain import (
    ApprovalKind,
    CheckoutState,
    CheckStatus,
    Decision,
    FinalValidationStatus,
    RunStatus,
)
from repogent.mcp_models import RunDecision, RunSnapshot, RunStart


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def _stdio_session(
    *, env: dict[str, str] | None = None
) -> AsyncIterator[ClientSession]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "repogent.mcp_server"],
        env=env,
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


def _copy_demo(tmp_path: Path, name: str = "target") -> Path:
    target = tmp_path / name
    shutil.copytree(Path("examples/fastapi_demo"), target)
    return target


def _start_request(target: Path, output_dir: Path, *, executor: str = "local") -> RunStart:
    return RunStart(
        repository=target,
        request='Add a health endpoint that returns {"status": "ok"}',
        provider="scripted",
        script=Path("examples/scripted_run.json").resolve(),
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


def _manifest(snapshot: RunSnapshot) -> dict[str, object]:
    return json.loads(Path(snapshot.evidence_path, "run.json").read_text())


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
                    target, tmp_path / "runs"
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
        pending = snapshot.pending_approval
        assert pending is not None
        assert pending.kind is ApprovalKind.PATCH
        assert isinstance(pending.artifact, dict)
        patch_artifact = pending.artifact
        selected = patch_artifact["selected_candidate"]
        assert selected["proposal"]["diff"] == expected_diff
        selected_summary = next(
            item for item in patch_artifact["candidates"] if item["selected"]
        )
        checks = selected_summary["checks"]
        assert len(checks) == 4
        assert {check["status"] for check in checks} == {CheckStatus.PASSED.value}

        snapshot = await _snapshot_call(session, "approve_patch", _approval(snapshot))

    assert snapshot.status is RunStatus.COMPLETED
    assert snapshot.checkout_state is CheckoutState.APPLIED
    assert snapshot.final_validation_status is FinalValidationStatus.PASSED
    assert '@app.get("/health")' in (target / "app.py").read_text()
    assert Path(snapshot.evidence_path, "report.md").is_file()
    manifest = _manifest(snapshot)
    assert manifest["checkout_state"] == CheckoutState.APPLIED.value
    assert manifest["final_validation_status"] == FinalValidationStatus.PASSED.value


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
