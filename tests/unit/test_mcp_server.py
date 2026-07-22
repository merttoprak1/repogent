from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from repogent import mcp_server
from repogent.domain import (
    ApprovalKind,
    CheckoutState,
    Decision,
    FinalValidationStatus,
    RunStage,
    RunStatus,
)
from repogent.mcp_models import (
    DoctorCheck,
    DoctorReport,
    DoctorRequest,
    RunDecision,
    RunReport,
    RunSnapshot,
    RunStart,
)
from repogent.mcp_server import create_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _snapshot(run_id: str = "run-1") -> RunSnapshot:
    return RunSnapshot(
        run_id=run_id,
        status=RunStatus.RUNNING,
        stage=RunStage.REQUIREMENTS,
        checkout_state=CheckoutState.NOT_APPLIED,
        selected_patch_applied=False,
        applied_paths=[],
        final_validation_status=FinalValidationStatus.NOT_STARTED,
        evidence_path="/bounded/evidence",
    )


class FakeManager:
    def __init__(self) -> None:
        self.snapshot = _snapshot()
        self.report = RunReport(
            run_id="run-1",
            status=RunStatus.COMPLETED,
            checkout_state=CheckoutState.NOT_APPLIED,
            evidence_path="/bounded/evidence",
            report="bounded report",
        )
        self.calls: list[tuple[str, object]] = []
        self.shutdown_called = False

    def start(self, request: RunStart) -> RunSnapshot:
        self.calls.append(("start", request))
        return self.snapshot

    def get(self, run_id: str) -> RunSnapshot:
        self.calls.append(("get", run_id))
        return self.snapshot

    def decide(self, decision: RunDecision) -> RunSnapshot:
        self.calls.append(("decide", decision))
        return self.snapshot

    def cancel(self, run_id: str) -> RunSnapshot:
        self.calls.append(("cancel", run_id))
        return self.snapshot

    def get_report(self, run_id: str) -> RunReport:
        self.calls.append(("get_report", run_id))
        return self.report

    def shutdown(self) -> None:
        self.shutdown_called = True


class FakeDoctor:
    def __init__(self) -> None:
        self.calls: list[DoctorRequest] = []
        self.report = DoctorReport(
            ready=True,
            repository="/bounded/repository",
            provider="codex-cli",
            executor="local",
            checks=[
                DoctorCheck(
                    name="repository",
                    passed=True,
                    required=True,
                    message="repository is accessible",
                )
            ],
        )

    def run(self, request: DoctorRequest) -> DoctorReport:
        self.calls.append(request)
        return self.report


@pytest.fixture
async def client_session() -> AsyncIterator[tuple[ClientSession, FakeManager, FakeDoctor]]:
    manager = FakeManager()
    doctor = FakeDoctor()
    server = create_server(manager=manager, doctor=doctor)
    async with create_connected_server_and_client_session(server, raise_exceptions=True) as session:
        yield session, manager, doctor


def test_create_server_constructs_without_starting_transport() -> None:

    server = create_server()

    assert server.name == "Repogent"


@pytest.mark.anyio
async def test_tool_catalog_has_exact_typed_contracts_and_annotations(
    client_session: tuple[ClientSession, FakeManager, FakeDoctor],
) -> None:
    session, _manager, _doctor = client_session

    listed = await session.list_tools()
    tools = {tool.name: tool for tool in listed.tools}

    assert set(tools) == {
        "repogent_doctor",
        "start_run",
        "get_run",
        "approve_requirements",
        "approve_plan",
        "approve_patch",
        "cancel_run",
        "get_report",
    }
    assert tools["repogent_doctor"].inputSchema["$defs"]["DoctorRequest"] == (
        DoctorRequest.model_json_schema()
    )
    assert tools["start_run"].inputSchema["$defs"]["RunStart"] == (RunStart.model_json_schema())
    decision_schema = tools["approve_plan"].inputSchema
    assert decision_schema["properties"]["decision"]["$ref"] == ("#/$defs/RunDecision")
    assert decision_schema["$defs"]["RunDecision"]["properties"] == {
        key: value for key, value in RunDecision.model_json_schema()["properties"].items()
    }
    assert tools["repogent_doctor"].outputSchema == DoctorReport.model_json_schema()
    assert tools["start_run"].outputSchema == RunSnapshot.model_json_schema()
    assert tools["get_report"].outputSchema == RunReport.model_json_schema()
    assert tools["cancel_run"].inputSchema["properties"] == {
        "run_id": {"title": "Run Id", "type": "string"}
    }

    expected_annotations = {
        "repogent_doctor": (True, False, True),
        "start_run": (False, False, False),
        "get_run": (True, False, True),
        "approve_requirements": (False, False, False),
        "approve_plan": (False, False, False),
        "approve_patch": (False, True, False),
        "cancel_run": (False, False, True),
        "get_report": (True, False, True),
    }
    for name, (read_only, destructive, idempotent) in expected_annotations.items():
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is read_only
        assert annotations.destructiveHint is destructive
        assert annotations.idempotentHint is idempotent
        assert annotations.openWorldHint is False


@pytest.mark.anyio
async def test_tools_route_typed_requests_and_return_structured_content(
    client_session: tuple[ClientSession, FakeManager, FakeDoctor],
) -> None:
    session, manager, doctor = client_session
    doctor_request = DoctorRequest(repository=Path("/repository"), executor="local")
    start_request = RunStart(
        repository=Path("/repository"), request="make a bounded change", executor="local"
    )
    requirements = RunDecision(
        run_id="run-1",
        kind=ApprovalKind.REQUIREMENTS,
        digest="a" * 64,
        decision=Decision.APPROVED,
    )
    plan_rejection = RunDecision(
        run_id="run-1",
        kind=ApprovalKind.PLAN,
        digest="b" * 64,
        decision=Decision.REJECTED,
        feedback="revise the plan",
    )
    patch = RunDecision(
        run_id="run-1",
        kind=ApprovalKind.PATCH,
        digest="c" * 64,
        decision=Decision.APPROVED,
    )

    calls = [
        ("repogent_doctor", {"request": doctor_request.model_dump(mode="json")}),
        ("start_run", {"request": start_request.model_dump(mode="json")}),
        ("get_run", {"run_id": "run-1"}),
        (
            "approve_requirements",
            {"decision": requirements.model_dump(mode="json")},
        ),
        ("approve_plan", {"decision": plan_rejection.model_dump(mode="json")}),
        ("approve_patch", {"decision": patch.model_dump(mode="json")}),
        ("get_report", {"run_id": "run-1"}),
    ]
    results = [await session.call_tool(name, arguments) for name, arguments in calls]

    assert [result.isError for result in results] == [False] * 7
    assert results[0].structuredContent == doctor.report.model_dump(mode="json")
    assert results[1].structuredContent == manager.snapshot.model_dump(mode="json")
    assert results[6].structuredContent == manager.report.model_dump(mode="json")
    assert doctor.calls == [doctor_request]
    assert manager.calls == [
        ("start", start_request),
        ("get", "run-1"),
        ("decide", requirements),
        ("decide", plan_rejection),
        ("decide", patch),
        ("get_report", "run-1"),
    ]


@pytest.mark.anyio
async def test_server_lifespan_always_shuts_down_sessions() -> None:
    manager = FakeManager()
    server = create_server(manager=manager, doctor=FakeDoctor())

    async with create_connected_server_and_client_session(server, raise_exceptions=True):
        assert manager.shutdown_called is False

    assert manager.shutdown_called is True


@pytest.mark.anyio
async def test_decision_tools_enforce_gate_contracts_and_cancel_patch_rejection(
    client_session: tuple[ClientSession, FakeManager, FakeDoctor],
) -> None:
    session, manager, _doctor = client_session
    requirements_rejection = RunDecision(
        run_id="run-1",
        kind=ApprovalKind.REQUIREMENTS,
        digest="d" * 64,
        decision=Decision.REJECTED,
        feedback="clarify scope",
    )
    plan_approval = RunDecision(
        run_id="run-1",
        kind=ApprovalKind.PLAN,
        digest="e" * 64,
        decision=Decision.APPROVED,
    )
    wrong_requirements_kind = plan_approval.model_copy(update={"decision": Decision.REJECTED})
    wrong_plan_kind = requirements_rejection.model_copy(update={"decision": Decision.APPROVED})
    patch_rejection = RunDecision(
        run_id="run-1",
        kind=ApprovalKind.PATCH,
        digest="f" * 64,
        decision=Decision.REJECTED,
    )

    requirements_result = await session.call_tool(
        "approve_requirements",
        {"decision": requirements_rejection.model_dump(mode="json")},
    )
    plan_result = await session.call_tool(
        "approve_plan", {"decision": plan_approval.model_dump(mode="json")}
    )
    wrong_requirements_result = await session.call_tool(
        "approve_requirements",
        {"decision": wrong_requirements_kind.model_dump(mode="json")},
    )
    wrong_plan_result = await session.call_tool(
        "approve_plan", {"decision": wrong_plan_kind.model_dump(mode="json")}
    )
    patch_rejection_result = await session.call_tool(
        "approve_patch", {"decision": patch_rejection.model_dump(mode="json")}
    )
    cancellation_result = await session.call_tool("cancel_run", {"run_id": "run-1"})

    assert requirements_result.isError is False
    assert plan_result.isError is False
    assert wrong_requirements_result.isError is True
    assert "decision kind must be requirements" in wrong_requirements_result.content[0].text
    assert wrong_plan_result.isError is True
    assert "decision kind must be plan" in wrong_plan_result.content[0].text
    assert patch_rejection_result.isError is True
    assert "approve_patch requires an approved decision" in (patch_rejection_result.content[0].text)
    assert cancellation_result.isError is False
    assert manager.calls == [
        ("decide", requirements_rejection),
        ("decide", plan_approval),
        ("cancel", "run-1"),
    ]


def test_serve_stdio_runs_only_stdio_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transports: list[str] = []

    class FakeServer:
        def run(self, *, transport: str) -> None:
            transports.append(transport)

    monkeypatch.setattr(mcp_server, "create_server", FakeServer)

    mcp_server.serve_stdio()

    assert transports == ["stdio"]
