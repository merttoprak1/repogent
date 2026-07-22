import traceback
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


class FalseyManager(FakeManager):
    def __bool__(self) -> bool:
        return False


class FalseyDoctor(FakeDoctor):
    def __bool__(self) -> bool:
        return False


_INTERNAL_FAILURE_DETAIL = (
    "secret-value at /private/secret/path; subprocess stdout contained credentials"
)


class FailingManager(FakeManager):
    @staticmethod
    def _fail() -> None:
        raise RuntimeError(_INTERNAL_FAILURE_DETAIL)

    def start(self, request: RunStart) -> RunSnapshot:
        self._fail()
        raise AssertionError("unreachable")

    def get(self, run_id: str) -> RunSnapshot:
        self._fail()
        raise AssertionError("unreachable")

    def decide(self, decision: RunDecision) -> RunSnapshot:
        self._fail()
        raise AssertionError("unreachable")

    def cancel(self, run_id: str) -> RunSnapshot:
        self._fail()
        raise AssertionError("unreachable")

    def get_report(self, run_id: str) -> RunReport:
        self._fail()
        raise AssertionError("unreachable")


class FailingDoctor(FakeDoctor):
    def run(self, request: DoctorRequest) -> DoctorReport:
        raise RuntimeError(_INTERNAL_FAILURE_DETAIL)


class FailingShutdownManager(FakeManager):
    def shutdown(self) -> None:
        self.shutdown_called = True
        raise RuntimeError(_INTERNAL_FAILURE_DETAIL)


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
    expected_run_id_schema = {
        "maxLength": 256,
        "minLength": 1,
        "title": "Run Id",
        "type": "string",
    }
    for name in ("get_run", "cancel_run", "get_report"):
        assert tools[name].inputSchema["properties"] == {
            "run_id": expected_run_id_schema
        }

    expected_annotations = {
        "repogent_doctor": (True, False, True, False),
        "start_run": (False, False, False, True),
        "get_run": (True, False, True, False),
        "approve_requirements": (False, False, False, True),
        "approve_plan": (False, False, False, True),
        "approve_patch": (False, True, False, False),
        "cancel_run": (False, False, True, False),
        "get_report": (True, False, True, False),
    }
    for name, (
        read_only,
        destructive,
        idempotent,
        open_world,
    ) in expected_annotations.items():
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is read_only
        assert annotations.destructiveHint is destructive
        assert annotations.idempotentHint is idempotent
        assert annotations.openWorldHint is open_world


@pytest.mark.anyio
async def test_tools_route_typed_requests_and_return_structured_content(
    client_session: tuple[ClientSession, FakeManager, FakeDoctor],
) -> None:
    session, manager, doctor = client_session
    doctor_request = DoctorRequest(repository=Path("/repository"), executor="local")
    start_request = RunStart(
        repository=Path("/repository"), request="make a bounded change", executor="deferred"
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
async def test_successful_structured_results_are_recursively_redacted() -> None:
    manager = FakeManager()
    manager.snapshot = manager.snapshot.model_copy(
        update={"reason": "token=sk-proj-1234567890abcdef password=do-not-show"}
    )
    manager.report = manager.report.model_copy(
        update={"report": "credential token=sk-proj-1234567890abcdef"}
    )
    doctor = FakeDoctor()
    doctor.report = doctor.report.model_copy(
        update={"repository": "/bounded/token=sk-proj-1234567890abcdef"}
    )
    server = create_server(manager=manager, doctor=doctor)

    async with create_connected_server_and_client_session(
        server, raise_exceptions=True
    ) as session:
        results = [
            await session.call_tool("get_run", {"run_id": "run-1"}),
            await session.call_tool("get_report", {"run_id": "run-1"}),
            await session.call_tool(
                "repogent_doctor",
                {
                    "request": DoctorRequest(
                        repository=Path("/repository"), executor="local"
                    ).model_dump(mode="json")
                },
            ),
        ]

    assert [result.isError for result in results] == [False, False, False]
    serialized = str([result.structuredContent for result in results])
    assert "sk-proj-1234567890abcdef" not in serialized
    assert "do-not-show" not in serialized
    assert serialized.count("[REDACTED]") >= 4


@pytest.mark.anyio
async def test_mcp_doctor_and_start_reject_regular_file_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository.py"
    repository.write_text("value = 1\n")
    server = create_server()

    async with create_connected_server_and_client_session(
        server, raise_exceptions=True
    ) as session:
        doctor_result = await session.call_tool(
            "repogent_doctor",
            {
                "request": DoctorRequest(
                    repository=repository,
                    provider="openai",
                    executor="local",
                ).model_dump(mode="json")
            },
        )
        start_result = await session.call_tool(
            "start_run",
            {
                "request": RunStart(
                    repository=repository,
                    request="change the file",
                    provider="openai",
                    executor="local",
                ).model_dump(mode="json")
            },
        )

    assert doctor_result.isError is False
    assert doctor_result.structuredContent is not None
    report = DoctorReport.model_validate(doctor_result.structuredContent)
    assert report.ready is False
    assert report.checks[0].message == "repository must be a directory"
    assert start_result.isError is True
    assert start_result.content[0].text.endswith(
        "run could not be started; inspect local Repogent logs"
    )


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


@pytest.mark.anyio
async def test_falsey_injected_dependencies_are_used_and_shut_down() -> None:
    manager = FalseyManager()
    doctor = FalseyDoctor()
    server = create_server(manager=manager, doctor=doctor)
    doctor_request = DoctorRequest(repository=Path("/repository"), executor="local")

    async with create_connected_server_and_client_session(
        server, raise_exceptions=True
    ) as session:
        doctor_result = await session.call_tool(
            "repogent_doctor",
            {"request": doctor_request.model_dump(mode="json")},
        )
        run_result = await session.call_tool("get_run", {"run_id": "run-1"})

    assert doctor_result.structuredContent == doctor.report.model_dump(mode="json")
    assert run_result.structuredContent == manager.snapshot.model_dump(mode="json")
    assert doctor.calls == [doctor_request]
    assert manager.calls == [("get", "run-1")]
    assert manager.shutdown_called is True


@pytest.mark.anyio
async def test_exceptional_server_context_still_shuts_down_injected_manager() -> None:
    class ContextFailure(RuntimeError):
        pass

    manager = FalseyManager()
    server = create_server(manager=manager, doctor=FalseyDoctor())

    with pytest.raises(ExceptionGroup) as raised:
        async with create_connected_server_and_client_session(
            server, raise_exceptions=True
        ):
            raise ContextFailure

    assert raised.value.subgroup(ContextFailure) is not None
    assert manager.shutdown_called is True


@pytest.mark.anyio
@pytest.mark.parametrize("tool_name", ["get_run", "cancel_run", "get_report"])
@pytest.mark.parametrize(
    "run_id",
    [
        "",
        "x" * 257,
        "secret-value" + "x" * 245,
    ],
    ids=["empty", "257-characters", "secret-257-characters"],
)
async def test_standalone_run_ids_are_bounded_and_redacted_before_routing(
    client_session: tuple[ClientSession, FakeManager, FakeDoctor],
    tool_name: str,
    run_id: str,
) -> None:
    session, manager, _doctor = client_session

    result = await session.call_tool(tool_name, {"run_id": run_id})

    assert result.isError is True
    assert result.content[0].text.endswith(
        "run ID must be between 1 and 256 characters"
    )
    assert len(result.content[0].text) <= 160
    assert "secret-value" not in result.content[0].text
    assert manager.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool_name", "manager_method"),
    [
        ("get_run", "get"),
        ("cancel_run", "cancel"),
        ("get_report", "get_report"),
    ],
)
@pytest.mark.parametrize(
    "run_id",
    ["x", "x" * 256],
    ids=["one-character", "256-characters"],
)
async def test_standalone_run_id_boundaries_route_to_manager(
    client_session: tuple[ClientSession, FakeManager, FakeDoctor],
    tool_name: str,
    manager_method: str,
    run_id: str,
) -> None:
    session, manager, _doctor = client_session

    result = await session.call_tool(tool_name, {"run_id": run_id})

    assert result.isError is False
    assert manager.calls == [(manager_method, run_id)]


@pytest.mark.anyio
async def test_internal_service_errors_use_bounded_allowlisted_messages() -> None:
    manager = FailingManager()
    server = create_server(manager=manager, doctor=FailingDoctor())
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
    plan = RunDecision(
        run_id="run-1",
        kind=ApprovalKind.PLAN,
        digest="b" * 64,
        decision=Decision.APPROVED,
    )
    patch = RunDecision(
        run_id="run-1",
        kind=ApprovalKind.PATCH,
        digest="c" * 64,
        decision=Decision.APPROVED,
    )
    calls = [
        (
            "repogent_doctor",
            {"request": doctor_request.model_dump(mode="json")},
            "readiness check failed; inspect local Repogent logs",
        ),
        (
            "start_run",
            {"request": start_request.model_dump(mode="json")},
            "run could not be started; inspect local Repogent logs",
        ),
        (
            "get_run",
            {"run_id": "run-1"},
            "run state is unavailable; inspect local Repogent logs",
        ),
        (
            "approve_requirements",
            {"decision": requirements.model_dump(mode="json")},
            "run decision could not be applied; inspect local Repogent logs",
        ),
        (
            "approve_plan",
            {"decision": plan.model_dump(mode="json")},
            "run decision could not be applied; inspect local Repogent logs",
        ),
        (
            "approve_patch",
            {"decision": patch.model_dump(mode="json")},
            "run decision could not be applied; inspect local Repogent logs",
        ),
        (
            "cancel_run",
            {"run_id": "run-1"},
            "run could not be cancelled; inspect local Repogent logs",
        ),
        (
            "get_report",
            {"run_id": "run-1"},
            "run report is unavailable; inspect local Repogent logs",
        ),
    ]

    async with create_connected_server_and_client_session(
        server, raise_exceptions=True
    ) as session:
        for tool_name, arguments, category in calls:
            result = await session.call_tool(tool_name, arguments)
            message = result.content[0].text

            assert result.isError is True
            assert message == f"Error executing tool {tool_name}: {category}"
            assert len(message) <= 160
            assert "secret-value" not in message
            assert "/private/secret/path" not in message
            assert "subprocess stdout" not in message


def _walk_exception_graph(error: BaseException) -> list[BaseException]:
    pending = [error]
    visited: set[int] = set()
    graph: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        graph.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return graph


def _assert_sanitized_lifecycle_error(error: BaseException) -> None:
    representation = repr(error)
    rendered = "".join(traceback.format_exception(error))
    lifecycle_error = "session shutdown failed; inspect local Repogent logs"
    graph = _walk_exception_graph(error)
    lifecycle_nodes = [node for node in graph if str(node) == lifecycle_error]

    assert lifecycle_error in representation
    assert lifecycle_error in rendered
    assert len(lifecycle_error) <= 160
    assert len(lifecycle_nodes) == 1
    assert lifecycle_nodes[0].__cause__ is None
    assert lifecycle_nodes[0].__context__ is None
    for forbidden in (
        "secret-value",
        "/private/secret/path",
        "subprocess stdout",
    ):
        assert forbidden not in representation
        assert forbidden not in rendered
        assert all(forbidden not in repr(node) for node in graph)


@pytest.mark.anyio
async def test_shutdown_failure_is_bounded_and_redacted_on_normal_exit() -> None:
    manager = FailingShutdownManager()
    server = create_server(manager=manager, doctor=FakeDoctor())

    with pytest.raises(ExceptionGroup) as raised:
        async with create_connected_server_and_client_session(
            server, raise_exceptions=True
        ):
            pass

    _assert_sanitized_lifecycle_error(raised.value)
    assert manager.shutdown_called is True


@pytest.mark.anyio
async def test_shutdown_failure_preserves_existing_context_error_without_leaks() -> None:
    class ContextFailure(RuntimeError):
        pass

    manager = FailingShutdownManager()
    server = create_server(manager=manager, doctor=FakeDoctor())

    with pytest.raises(ExceptionGroup) as raised:
        async with create_connected_server_and_client_session(
            server, raise_exceptions=True
        ):
            raise ContextFailure("body failure remains distinguishable")

    _assert_sanitized_lifecycle_error(raised.value)
    assert raised.value.subgroup(ContextFailure) is not None
    assert manager.shutdown_called is True
