from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from repogent.doctor import DoctorService
from repogent.domain import ApprovalKind, Decision
from repogent.mcp_models import (
    DoctorReport,
    DoctorRequest,
    RunDecision,
    RunReport,
    RunSnapshot,
    RunStart,
)
from repogent.run_sessions import SessionManager


def create_server(
    manager: SessionManager | None = None,
    doctor: DoctorService | None = None,
) -> FastMCP:
    sessions = manager or SessionManager()
    readiness = doctor or DoctorService()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, object]]:
        try:
            yield {"sessions": sessions}
        finally:
            sessions.shutdown()

    server = FastMCP("Repogent", json_response=True, lifespan=lifespan)

    @server.tool(
        name="repogent_doctor",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def repogent_doctor(request: DoctorRequest) -> DoctorReport:
        return readiness.run(request)

    @server.tool(
        name="start_run",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def start_run(request: RunStart) -> RunSnapshot:
        return sessions.start(request)

    @server.tool(
        name="get_run",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_run(run_id: str) -> RunSnapshot:
        return sessions.get(run_id)

    def require_kind(decision: RunDecision, expected: ApprovalKind) -> None:
        if decision.kind is not expected:
            raise ValueError(f"decision kind must be {expected.value}")

    @server.tool(
        name="approve_requirements",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def approve_requirements(decision: RunDecision) -> RunSnapshot:
        require_kind(decision, ApprovalKind.REQUIREMENTS)
        return sessions.decide(decision)

    @server.tool(
        name="approve_plan",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def approve_plan(decision: RunDecision) -> RunSnapshot:
        require_kind(decision, ApprovalKind.PLAN)
        return sessions.decide(decision)

    @server.tool(
        name="approve_patch",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def approve_patch(decision: RunDecision) -> RunSnapshot:
        require_kind(decision, ApprovalKind.PATCH)
        if decision.decision is not Decision.APPROVED:
            raise ValueError("approve_patch requires an approved decision")
        return sessions.decide(decision)

    @server.tool(
        name="cancel_run",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def cancel_run(run_id: str) -> RunSnapshot:
        return sessions.cancel(run_id)

    @server.tool(
        name="get_report",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_report(run_id: str) -> RunReport:
        return sessions.get_report(run_id)

    return server


def serve_stdio() -> None:
    create_server().run(transport="stdio")
