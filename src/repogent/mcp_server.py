from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated, TypeVar, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, WithJsonSchema

from repogent.doctor import DoctorService
from repogent.domain import ApprovalKind, Decision
from repogent.mcp_models import (
    DoctorReport,
    DoctorRequest,
    ExecutionDecision,
    RunDecision,
    RunReport,
    RunSnapshot,
    RunStart,
)
from repogent.run_sessions import SessionManager
from repogent.sanitization import sanitize_data

RunId = Annotated[
    str,
    WithJsonSchema({"type": "string", "minLength": 1, "maxLength": 256}),
]

_ResultT = TypeVar("_ResultT")
_LIFECYCLE_ERROR = "session shutdown failed; inspect local Repogent logs"


class _ServiceError(StrEnum):
    DOCTOR = "readiness check failed; inspect local Repogent logs"
    START = "run could not be started; inspect local Repogent logs"
    GET = "run state is unavailable; inspect local Repogent logs"
    DECISION = "run decision could not be applied; inspect local Repogent logs"
    EXECUTOR = "executor selection could not be applied; inspect local Repogent logs"
    CANCEL = "run could not be cancelled; inspect local Repogent logs"
    REPORT = "run report is unavailable; inspect local Repogent logs"


def _call_service(
    action: Callable[[], _ResultT], message: _ServiceError
) -> _ResultT:
    try:
        result = action()
        if not isinstance(result, BaseModel):
            return result
        payload = sanitize_data(result.model_dump(mode="json"))
        if not isinstance(payload, dict):
            raise ValueError("structured response sanitization failed")
        if isinstance(result, RunSnapshot) and result.pending_approval is not None:
            pending = payload.get("pending_approval")
            if (
                not isinstance(pending, dict)
                or pending.get("artifact") != result.pending_approval.artifact
            ):
                raise ValueError("approval artifact is unsafe to return")
        if isinstance(result, RunSnapshot) and result.pending_execution is not None:
            pending_execution = payload.get("pending_execution")
            if (
                not isinstance(pending_execution, dict)
                or pending_execution.get("preview") != result.pending_execution.preview
            ):
                raise ValueError("execution preview is unsafe to return")
        model_type = cast(type[BaseModel], type(result))
        return cast(_ResultT, model_type.model_validate(payload))
    except Exception:
        raise RuntimeError(message.value) from None


def _shutdown_sessions(sessions: SessionManager) -> bool:
    try:
        sessions.shutdown()
    except Exception:
        return False
    return True


def create_server(
    manager: SessionManager | None = None,
    doctor: DoctorService | None = None,
) -> FastMCP:
    sessions = manager if manager is not None else SessionManager()
    readiness = doctor if doctor is not None else DoctorService()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, object]]:
        try:
            yield {"sessions": sessions}
        finally:
            if not _shutdown_sessions(sessions):
                lifecycle_error = RuntimeError(_LIFECYCLE_ERROR)
                try:
                    raise lifecycle_error
                finally:
                    # AnyIO cancellation would otherwise remain linked as context.
                    lifecycle_error.__context__ = None

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
        return _call_service(lambda: readiness.run(request), _ServiceError.DOCTOR)

    @server.tool(
        name="start_run",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    def start_run(request: RunStart) -> RunSnapshot:
        return _call_service(lambda: sessions.start(request), _ServiceError.START)

    def require_run_id(run_id: RunId) -> str:
        if not 1 <= len(run_id) <= 256:
            raise ValueError("run ID must be between 1 and 256 characters")
        return run_id

    @server.tool(
        name="get_run",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_run(run_id: RunId) -> RunSnapshot:
        bounded_run_id = require_run_id(run_id)
        return _call_service(lambda: sessions.get(bounded_run_id), _ServiceError.GET)

    def require_kind(decision: RunDecision, expected: ApprovalKind) -> None:
        if decision.kind is not expected:
            raise ValueError(f"decision kind must be {expected.value}")

    @server.tool(
        name="approve_requirements",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    def approve_requirements(decision: RunDecision) -> RunSnapshot:
        require_kind(decision, ApprovalKind.REQUIREMENTS)
        return _call_service(lambda: sessions.decide(decision), _ServiceError.DECISION)

    @server.tool(
        name="approve_plan",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    def approve_plan(decision: RunDecision) -> RunSnapshot:
        require_kind(decision, ApprovalKind.PLAN)
        return _call_service(lambda: sessions.decide(decision), _ServiceError.DECISION)

    @server.tool(
        name="select_executor",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def select_executor(decision: ExecutionDecision) -> RunSnapshot:
        if decision.decision is not Decision.APPROVED:
            raise ValueError("select_executor requires an approved decision")
        return _call_service(
            lambda: sessions.select_executor(decision),
            _ServiceError.EXECUTOR,
        )

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
        return _call_service(lambda: sessions.decide(decision), _ServiceError.DECISION)

    @server.tool(
        name="cancel_run",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def cancel_run(run_id: RunId) -> RunSnapshot:
        bounded_run_id = require_run_id(run_id)
        return _call_service(lambda: sessions.cancel(bounded_run_id), _ServiceError.CANCEL)

    @server.tool(
        name="get_report",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def get_report(run_id: RunId) -> RunReport:
        bounded_run_id = require_run_id(run_id)
        return _call_service(lambda: sessions.get_report(bounded_run_id), _ServiceError.REPORT)

    return server


def serve_stdio() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    serve_stdio()
