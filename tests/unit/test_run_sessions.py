from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from repogent.agents import RoleSet
from repogent.approval_gate import ApprovalGateError
from repogent.approvals import Approver
from repogent.artifacts import ArtifactStore
from repogent.domain import (
    ApprovalKind,
    Budget,
    CheckoutState,
    CheckResult,
    CheckStatus,
    Decision,
    ExecutionMode,
    IsolationLevel,
    RunManifest,
    RunStatus,
    TrustLabel,
    ValidationReport,
    VerificationStatus,
)
from repogent.execution import ValidationPolicy
from repogent.executor_selection import LOCAL_RISK_STATEMENT, PreparedExecutor
from repogent.mcp_models import (
    ExecutionDecision,
    ExecutorAvailability,
    ExecutorOption,
    RunDecision,
    RunStart,
)
from repogent.patching import PatchApplier, PatchPolicy
from repogent.preflight import PreflightReport
from repogent.providers import ScriptedProvider
from repogent.repository import RepositoryInspector
from repogent.run_builder import PreparedRun, RunOptions
from repogent.run_sessions import SessionError, SessionManager
from repogent.workflow import Workflow

REQUIREMENTS_OUTPUT = {
    "objective": "Change value",
    "functional_requirements": ["value is 2"],
    "acceptance_criteria": ["tests pass"],
}
PLAN_OUTPUT = {
    "files_to_modify": ["app.py"],
    "steps": [{"id": "change", "description": "Change value"}],
    "tests": ["pytest"],
}
VALID_PATCH_OUTPUT = {
    "summary": "Change value",
    "diff": (
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
        " def value():\n-    return 1\n+    return 2\n"
    ),
    "acceptance_criteria_addressed": ["tests pass"],
    "focused_tests": ["pytest"],
}


class PassingValidator:
    def run(
        self, root: Path, *, timeout_seconds: float | None = None
    ) -> ValidationReport:
        del root, timeout_seconds
        return ValidationReport(
            checks=[CheckResult(name="pytest", argv=["pytest"], status=CheckStatus.PASSED)]
        )


class SessionRegistry:
    def __init__(self) -> None:
        self.prepare_calls: list[ExecutionMode] = []

    def inspect_availability(
        self, root: Path, policy: ValidationPolicy
    ) -> list[ExecutorAvailability]:
        del root, policy
        return [
            ExecutorAvailability(
                mode=ExecutionMode.DOCKER,
                available=True,
                isolation_level=IsolationLevel.ISOLATED,
                message="Docker validation is available",
            ),
            ExecutorAvailability(
                mode=ExecutionMode.LOCAL,
                available=True,
                isolation_level=IsolationLevel.REDUCED_ISOLATION,
                message="Local validation is available",
                risk_statement=LOCAL_RISK_STATEMENT,
            ),
        ]

    def build_options(
        self,
        run_id: str,
        preview_digest: str,
        availability: list[ExecutorAvailability],
    ) -> list[ExecutorOption]:
        return [
            ExecutorOption(
                mode=item.mode,
                available=item.available,
                isolation_level=item.isolation_level,
                option_digest=hashlib.sha256(
                    f"{run_id}:{preview_digest}:{item.mode.value}".encode()
                ).hexdigest(),
                message=item.message,
                remediation=item.remediation,
                risk_statement=item.risk_statement,
            )
            for item in availability
        ]

    def prepare(
        self, root: Path, mode: ExecutionMode, policy: ValidationPolicy
    ) -> PreparedExecutor:
        del root, policy
        self.prepare_calls.append(mode)
        return PreparedExecutor(
            mode=mode,
            isolation_level=(
                IsolationLevel.ISOLATED
                if mode is ExecutionMode.DOCKER
                else IsolationLevel.REDUCED_ISOLATION
            ),
            preflight=PreflightReport(
                checks=[], git_commit=None, dirty=False, repository_fingerprint="repo"
            ),
            validator=PassingValidator(),  # type: ignore[arg-type]
        )


class FalseySessionRegistry(SessionRegistry):
    def __bool__(self) -> bool:
        return False


class BlockingSessionRegistry(SessionRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.prepare_entered = threading.Event()
        self.release_prepare = threading.Event()

    def prepare(
        self, root: Path, mode: ExecutionMode, policy: ValidationPolicy
    ) -> PreparedExecutor:
        self.prepare_entered.set()
        assert self.release_prepare.wait(timeout=5)
        return super().prepare(root, mode, policy)


class BlockingInspector:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self._delegate = RepositoryInspector()

    def inspect(self, root: Path, *, deadline: float | None = None):
        self.started.set()
        assert self.release.wait(timeout=5)
        return self._delegate.inspect(root, deadline=deadline)


def make_target(tmp_path: Path, name: str = "target") -> Path:
    target = tmp_path / name
    target.mkdir()
    (target / "app.py").write_text("def value():\n    return 1\n")
    return target


def make_builder(
    *,
    inspector: object | None = None,
    before_build: Callable[[], None] | None = None,
    budget: Budget | None = None,
) -> Callable[..., PreparedRun]:
    counter = 0

    def builder(
        options: RunOptions,
        approver_factory: Callable[[str], Approver],
        *,
        executor_selector_factory=None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> PreparedRun:
        nonlocal counter
        if before_build is not None:
            before_build()
        counter += 1
        root = options.repository.resolve(strict=True)
        run_id = f"run-{counter}"
        store = ArtifactStore.create(
            options.output_dir or root.parent / "runs",
            root,
            options.request,
            run_id=run_id,
        )
        approver = approver_factory(run_id)
        manifest = RunManifest(run_id=run_id, request=options.request)
        selector = (
            executor_selector_factory(run_id, root, ValidationPolicy())
            if options.executor == "deferred"
            else None
        )
        workflow = Workflow(
            root=root,
            request=options.request,
            manifest=manifest,
            roles=RoleSet.from_provider(
                ScriptedProvider(
                    [REQUIREMENTS_OUTPUT, PLAN_OUTPUT, VALID_PATCH_OUTPUT]
                )
            ),
            approver=approver,
            patch_policy=PatchPolicy(),
            patch_applier=PatchApplier(),
            validator=None if selector is not None else PassingValidator(),
            executor_selector=selector,
            artifacts=store,
            inspector=inspector or RepositoryInspector(),  # type: ignore[arg-type]
            budget=budget or Budget(),
            cancel_requested=cancel_requested,
        )
        return PreparedRun(
            store=store,
            manifest=manifest,
            workflow=workflow,
            approver=approver,
            preflight=PreflightReport(
                checks=[], git_commit=None, dirty=False, repository_fingerprint="repo"
            ),
            executor_selector=workflow.executor_selector,
        )

    return builder


def start_request(target: Path, output_dir: Path) -> RunStart:
    return RunStart(
        repository=target,
        request="Change value",
        provider="scripted",
        script=target.parent / "script.json",
        executor="local",
        output_dir=output_dir,
    )


def deferred_request(target: Path, output_dir: Path) -> RunStart:
    return start_request(target, output_dir).model_copy(
        update={"executor": "deferred"}
    )


def decision_for(snapshot, decision: Decision = Decision.APPROVED) -> RunDecision:
    assert snapshot.pending_approval is not None
    return RunDecision(
        run_id=snapshot.run_id,
        kind=snapshot.pending_approval.kind,
        digest=snapshot.pending_approval.digest,
        decision=decision,
    )


def execution_decision_for(
    snapshot,
    *,
    mode: ExecutionMode = ExecutionMode.LOCAL,
    decision: Decision = Decision.APPROVED,
) -> ExecutionDecision:
    assert snapshot.pending_execution is not None
    selected = next(
        item for item in snapshot.pending_execution.options if item.mode is mode
    )
    return ExecutionDecision(
        run_id=snapshot.run_id,
        preview_digest=snapshot.pending_execution.preview_digest,
        mode=mode,
        option_digest=selected.option_digest,
        decision=decision,
    )


def deferred_manager_waiting_for_executor(
    tmp_path: Path,
    *,
    registry: SessionRegistry | None = None,
    budget: Budget | None = None,
):
    target = make_target(tmp_path)
    selected_registry = registry or SessionRegistry()
    manager = SessionManager(
        builder=make_builder(budget=budget),
        executor_registry=selected_registry,  # type: ignore[arg-type]
    )
    request = deferred_request(target, tmp_path / "runs")
    snapshot = manager.start(request)
    snapshot = manager.decide(decision_for(snapshot))
    snapshot = manager.decide(decision_for(snapshot))
    return manager, request, snapshot


def test_manager_preserves_injected_falsey_executor_registry() -> None:
    registry = FalseySessionRegistry()

    manager = SessionManager(executor_registry=registry)  # type: ignore[arg-type]

    assert manager._executor_registry is registry


def test_deferred_session_surfaces_pending_execution_after_plan(
    tmp_path: Path,
) -> None:
    manager, _request, snapshot = deferred_manager_waiting_for_executor(tmp_path)
    try:
        assert snapshot.pending_approval is None
        assert snapshot.pending_execution is not None
        assert snapshot.verification_status is VerificationStatus.UNVALIDATED
        assert snapshot.checkout_state is CheckoutState.NOT_APPLIED
        assert snapshot.selected_patch_applied is False
    finally:
        manager.cancel(snapshot.run_id)
        manager.shutdown()


def test_executor_selection_releases_waiter_but_not_root_lock(
    tmp_path: Path,
) -> None:
    manager, request, snapshot = deferred_manager_waiting_for_executor(tmp_path)
    try:
        selected = manager.select_executor(execution_decision_for(snapshot))

        assert selected.pending_execution is None
        assert selected.pending_approval is not None
        assert selected.pending_approval.kind is ApprovalKind.PATCH
        assert selected.execution_mode is ExecutionMode.LOCAL
        assert selected.isolation_level is IsolationLevel.REDUCED_ISOLATION
        with pytest.raises(SessionError, match="repository already has an active run"):
            manager.start(request)
        manager.cancel(snapshot.run_id)
    finally:
        manager.shutdown()


def test_get_remains_available_while_executor_prepare_is_blocked(
    tmp_path: Path,
) -> None:
    registry = BlockingSessionRegistry()
    manager, _request, snapshot = deferred_manager_waiting_for_executor(
        tmp_path, registry=registry
    )
    selection_results: list[object] = []
    selection_errors: list[BaseException] = []

    def select_executor() -> None:
        try:
            selection_results.append(
                manager.select_executor(execution_decision_for(snapshot))
            )
        except BaseException as error:
            selection_errors.append(error)

    selector = threading.Thread(target=select_executor)
    selector.start()
    getter_results: list[object] = []
    getter = threading.Thread(
        target=lambda: getter_results.append(manager.get(snapshot.run_id))
    )
    try:
        assert registry.prepare_entered.wait(timeout=1)
        getter.start()
        getter.join(timeout=0.2)
        assert not getter.is_alive()
        assert len(getter_results) == 1
        assert getter_results[0].pending_execution == snapshot.pending_execution  # type: ignore[union-attr]
    finally:
        registry.release_prepare.set()
        selector.join(timeout=2)
        getter.join(timeout=1)
        if not manager._sessions[snapshot.run_id].is_done():
            manager.cancel(snapshot.run_id)
        manager.shutdown()
    assert not selector.is_alive()
    assert selection_errors == []
    assert len(selection_results) == 1


def test_cancel_wins_while_executor_prepare_is_blocked(tmp_path: Path) -> None:
    registry = BlockingSessionRegistry()
    manager, _request, snapshot = deferred_manager_waiting_for_executor(
        tmp_path, registry=registry
    )
    selection_errors: list[BaseException] = []

    def select_executor() -> None:
        try:
            manager.select_executor(execution_decision_for(snapshot))
        except BaseException as error:
            selection_errors.append(error)

    selector = threading.Thread(target=select_executor)
    selector.start()
    cancellation_results: list[object] = []
    cancellation_errors: list[BaseException] = []

    def cancel() -> None:
        try:
            cancellation_results.append(manager.cancel(snapshot.run_id))
        except BaseException as error:
            cancellation_errors.append(error)

    canceller = threading.Thread(target=cancel)
    try:
        assert registry.prepare_entered.wait(timeout=1)
        canceller.start()
        canceller.join(timeout=0.5)
        assert not canceller.is_alive()
        assert cancellation_errors == []
        assert len(cancellation_results) == 1
        terminal = cancellation_results[0]
        assert terminal.status is RunStatus.CANCELLED  # type: ignore[union-attr]
        assert terminal.pending_approval is None  # type: ignore[union-attr]
        assert terminal.pending_execution is None  # type: ignore[union-attr]
        recovered = manager.get(snapshot.run_id)
        assert recovered.pending_approval is None
        assert recovered.pending_execution is None
    finally:
        registry.release_prepare.set()
        selector.join(timeout=2)
        canceller.join(timeout=1)
        manager.shutdown()
    assert not selector.is_alive()
    assert len(selection_errors) == 1
    assert isinstance(selection_errors[0], SessionError)
    assert "closed" in str(selection_errors[0])


def test_executor_timeout_terminal_snapshot_clears_pending_choice(
    tmp_path: Path,
) -> None:
    manager, _request, snapshot = deferred_manager_waiting_for_executor(
        tmp_path,
        budget=Budget(timeout_seconds=1),
    )
    session = manager._sessions[snapshot.run_id]
    try:
        assert snapshot.pending_execution is not None
        assert session._done.wait(timeout=2)

        terminal = manager.get(snapshot.run_id)

        assert terminal.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
        assert terminal.reason == "executor selection timed out"
        assert terminal.pending_approval is None
        assert terminal.pending_execution is None
    finally:
        manager.shutdown()


def test_get_reconciles_pending_execution_after_caller_disconnect(
    tmp_path: Path,
) -> None:
    manager, _request, snapshot = deferred_manager_waiting_for_executor(tmp_path)
    try:
        assert snapshot.pending_execution is not None
        original = snapshot.pending_execution.model_copy(deep=True)
        snapshot.pending_execution.preview["changed_files"] = 99
        snapshot.pending_execution.options[1].option_digest = "f" * 64

        recovered = manager.get(snapshot.run_id)

        assert recovered.pending_execution == original
        selected = manager.select_executor(execution_decision_for(recovered))
        assert selected.pending_approval is not None
        manager.cancel(snapshot.run_id)
    finally:
        manager.shutdown()


def test_executor_selection_is_rejected_while_content_approval_is_pending(
    tmp_path: Path,
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(
        builder=make_builder(), executor_registry=SessionRegistry()  # type: ignore[arg-type]
    )
    snapshot = manager.start(deferred_request(target, tmp_path / "runs"))
    try:
        assert snapshot.pending_approval is not None
        with pytest.raises(SessionError, match="no executor selection is pending"):
            manager.select_executor(
                ExecutionDecision(
                    run_id=snapshot.run_id,
                    preview_digest="a" * 64,
                    mode=ExecutionMode.LOCAL,
                    option_digest="b" * 64,
                    decision=Decision.APPROVED,
                )
            )
    finally:
        manager.cancel(snapshot.run_id)
        manager.shutdown()


def test_stale_execution_decision_does_not_consume_pending_choice(
    tmp_path: Path,
) -> None:
    manager, _request, snapshot = deferred_manager_waiting_for_executor(tmp_path)
    try:
        decision = execution_decision_for(snapshot)
        with pytest.raises(SessionError, match="preview digest mismatch"):
            manager.select_executor(
                decision.model_copy(update={"preview_digest": "f" * 64})
            )
        with pytest.raises(SessionError, match="option digest mismatch"):
            manager.select_executor(
                decision.model_copy(update={"option_digest": "e" * 64})
            )

        recovered = manager.get(snapshot.run_id)
        assert recovered.pending_execution == snapshot.pending_execution
        manager.cancel(snapshot.run_id)
    finally:
        manager.shutdown()


def test_cancel_while_awaiting_executor_closes_both_decision_channels(
    tmp_path: Path,
) -> None:
    manager, _request, snapshot = deferred_manager_waiting_for_executor(tmp_path)

    terminal = manager.cancel(snapshot.run_id)

    assert terminal.status is RunStatus.CANCELLED
    assert terminal.pending_approval is None
    assert terminal.pending_execution is None
    assert terminal.checkout_state is CheckoutState.NOT_APPLIED
    assert terminal.cancellation_requested is True
    manager.shutdown()


def test_rejected_local_selection_terminalizes_before_root_release(
    tmp_path: Path,
) -> None:
    manager, request, snapshot = deferred_manager_waiting_for_executor(tmp_path)
    try:
        terminal = manager.select_executor(
            execution_decision_for(snapshot, decision=Decision.REJECTED)
        )

        assert terminal.status is RunStatus.HUMAN_INTERVENTION_REQUIRED
        assert terminal.pending_execution is None
        replacement = manager.start(request)
        manager.cancel(replacement.run_id)
    finally:
        manager.shutdown()


def test_shutdown_closes_gate_waiter_within_shared_deadline(tmp_path: Path) -> None:
    manager, _request, snapshot = deferred_manager_waiting_for_executor(tmp_path)
    started = time.monotonic()

    manager.shutdown()

    assert time.monotonic() - started < 1
    terminal = manager.get(snapshot.run_id)
    assert terminal.status is RunStatus.CANCELLED
    assert terminal.pending_execution is None


def test_session_advances_matching_gate_and_releases_root_at_terminal(
    tmp_path: Path,
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    request = start_request(target, tmp_path / "runs")
    try:
        snapshot = manager.start(request)
        assert snapshot.pending_approval is not None
        assert snapshot.pending_approval.kind is ApprovalKind.REQUIREMENTS

        with pytest.raises(SessionError, match="active run"):
            manager.start(request)
        with pytest.raises(SessionError, match="digest"):
            manager.decide(
                RunDecision(
                    run_id=snapshot.run_id,
                    kind=snapshot.pending_approval.kind,
                    digest="f" * 64,
                    decision=Decision.APPROVED,
                )
            )

        snapshot = manager.decide(decision_for(snapshot))
        assert snapshot.pending_approval is not None
        assert snapshot.pending_approval.kind is ApprovalKind.PLAN

        terminal = manager.decide(decision_for(snapshot, Decision.REJECTED))
        assert terminal.status is RunStatus.CANCELLED
        assert terminal.pending_approval is None

        replacement = manager.start(request)
        assert replacement.run_id != terminal.run_id
        assert replacement.pending_approval is not None
        manager.cancel(replacement.run_id)
    finally:
        manager.shutdown()


def test_returned_snapshot_cannot_mutate_pending_gate_integrity(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    try:
        snapshot = manager.start(start_request(target, tmp_path / "runs"))
        assert snapshot.pending_approval is not None
        original_digest = snapshot.pending_approval.digest
        original_artifact = snapshot.pending_approval.model_copy(deep=True).artifact

        snapshot.pending_approval.digest = "f" * 64
        assert isinstance(snapshot.pending_approval.artifact, dict)
        snapshot.pending_approval.artifact["objective"] = "tampered"

        fresh = manager.get(snapshot.run_id)
        assert fresh.pending_approval is not None
        assert fresh.pending_approval.digest == original_digest
        assert fresh.pending_approval.artifact == original_artifact
        advanced = manager.decide(
            RunDecision(
                run_id=snapshot.run_id,
                kind=ApprovalKind.REQUIREMENTS,
                digest=original_digest,
                decision=Decision.APPROVED,
            )
        )
        assert advanced.pending_approval is not None
        assert advanced.pending_approval.kind is ApprovalKind.PLAN
        manager.cancel(snapshot.run_id)
    finally:
        manager.shutdown()


def test_session_snapshot_surfaces_manifest_execution_trust_evidence(
    tmp_path: Path,
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    try:
        snapshot = manager.start(start_request(target, tmp_path / "runs"))
        session = manager._sessions[snapshot.run_id]
        session.prepared.workflow.manifest = session.prepared.workflow.manifest.model_copy(
            update={
                "execution_mode": ExecutionMode.DOCKER,
                "isolation_level": IsolationLevel.ISOLATED,
                "verification_status": VerificationStatus.PASSED,
            }
        )

        refreshed = manager.get(snapshot.run_id)

        assert refreshed.execution_mode is ExecutionMode.DOCKER
        assert refreshed.isolation_level is IsolationLevel.ISOLATED
        assert refreshed.verification_status is VerificationStatus.PASSED
        assert refreshed.trust_label is TrustLabel.ISOLATED_VERIFIED
        manager.cancel(snapshot.run_id)
    finally:
        manager.shutdown()


def test_terminal_session_snapshot_recursively_redacts_manifest_reason(
    tmp_path: Path,
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    try:
        snapshot = manager.start(start_request(target, tmp_path / "runs"))
        terminal = manager.cancel(snapshot.run_id)
        session = manager._sessions[terminal.run_id]
        assert session._result is not None
        session._result = session._result.model_copy(
            update={
                "reason": "provider token=sk-proj-1234567890abcdef password=do-not-show"
            }
        )

        observed = manager.get(terminal.run_id)

        assert observed.reason == "provider token=[REDACTED] password=[REDACTED]"
        assert "sk-proj-1234567890abcdef" not in observed.model_dump_json()
        assert "do-not-show" not in observed.model_dump_json()
    finally:
        manager.shutdown()


def test_unknown_runs_are_rejected(tmp_path: Path) -> None:
    manager = SessionManager(builder=make_builder())
    try:
        with pytest.raises(SessionError, match="unknown run"):
            manager.get("missing")
        with pytest.raises(SessionError, match="unknown run"):
            manager.cancel("missing")
        with pytest.raises(SessionError, match="unknown run"):
            manager.decide(
                RunDecision(
                    run_id="missing",
                    kind=ApprovalKind.PLAN,
                    digest="a" * 64,
                    decision=Decision.APPROVED,
                )
            )
    finally:
        manager.shutdown()


def test_canonical_alias_is_locked_while_run_is_being_prepared(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    alias = tmp_path / "target-alias"
    alias.symlink_to(target, target_is_directory=True)
    entered = threading.Event()
    release = threading.Event()

    def block_first_build() -> None:
        entered.set()
        assert release.wait(timeout=5)

    manager = SessionManager(builder=make_builder(before_build=block_first_build))
    outcome: list[object] = []
    starter = threading.Thread(
        target=lambda: outcome.append(
            manager.start(start_request(target, tmp_path / "runs"))
        )
    )
    starter.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(SessionError, match="being prepared"):
            manager.start(start_request(alias, tmp_path / "runs"))
        release.set()
        starter.join(timeout=5)
        assert not starter.is_alive()
        assert len(outcome) == 1
        with pytest.raises(SessionError, match="active run"):
            manager.start(start_request(alias, tmp_path / "runs"))
        manager.cancel(outcome[0].run_id)  # type: ignore[union-attr]
    finally:
        release.set()
        manager.shutdown()
        starter.join(timeout=5)


def test_build_failure_releases_canonical_reservation(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    working_builder = make_builder()
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("construction failed")
        return working_builder(*args, **kwargs)

    manager = SessionManager(builder=fail_once)
    request = start_request(target, tmp_path / "runs")
    try:
        with pytest.raises(RuntimeError, match="construction failed"):
            manager.start(request)
        snapshot = manager.start(request)
        assert snapshot.pending_approval is not None
        manager.cancel(snapshot.run_id)
    finally:
        manager.shutdown()


def test_cancel_during_work_is_cooperative_and_preserves_checkout_state(
    tmp_path: Path,
) -> None:
    target = make_target(tmp_path)
    inspector = BlockingInspector()
    manager = SessionManager(builder=make_builder(inspector=inspector))
    outcome: list[object] = []
    starter = threading.Thread(
        target=lambda: outcome.append(
            manager.start(start_request(target, tmp_path / "runs"))
        )
    )
    starter.start()
    try:
        assert inspector.started.wait(timeout=5)
        cancelled: list[object] = []
        canceller = threading.Thread(
            target=lambda: cancelled.append(manager.cancel("run-1"))
        )
        canceller.start()
        inspector.release.set()
        starter.join(timeout=5)
        canceller.join(timeout=5)
        assert not starter.is_alive()
        assert not canceller.is_alive()
        snapshot = cancelled[0]
        assert snapshot.status is RunStatus.CANCELLED  # type: ignore[union-attr]
        assert snapshot.checkout_state is CheckoutState.NOT_APPLIED  # type: ignore[union-attr]
        assert snapshot.selected_patch_applied is False  # type: ignore[union-attr]
        assert snapshot.cancellation_requested is True  # type: ignore[union-attr]
    finally:
        inspector.release.set()
        manager.shutdown()
        starter.join(timeout=5)


def test_report_is_terminal_run_report_only_and_bounded(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    try:
        snapshot = manager.start(start_request(target, tmp_path / "runs"))
        with pytest.raises(SessionError, match="terminal"):
            manager.get_report(snapshot.run_id)
        terminal = manager.cancel(snapshot.run_id)
        assert terminal.cancellation_requested is True
        assert terminal.checkout_state is CheckoutState.NOT_APPLIED
        assert terminal.selected_patch_applied is False
        report = manager.get_report(snapshot.run_id)
        assert report.status is terminal.status
        assert report.checkout_state is terminal.checkout_state
        assert report.report == (
            Path(report.evidence_path) / "report.md"
        ).read_text(encoding="utf-8")

        evidence = Path(report.evidence_path)
        (evidence / "other.md").write_text("not the report")
        (evidence / "report.md").write_text("x" * 64_001)
        with pytest.raises(SessionError, match="64,000"):
            manager.get_report(snapshot.run_id)
    finally:
        manager.shutdown()


def test_terminal_session_rejects_cancel_and_shutdown_preserves_history(
    tmp_path: Path,
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    snapshot = manager.start(start_request(target, tmp_path / "runs"))
    snapshot = manager.decide(decision_for(snapshot))
    terminal = manager.decide(decision_for(snapshot, Decision.REJECTED))
    assert terminal.cancellation_requested is False

    manager.shutdown()

    assert manager.get(terminal.run_id).cancellation_requested is False
    with pytest.raises(SessionError, match="terminal"):
        manager.cancel(terminal.run_id)


def test_shutdown_closes_terminal_approver_without_cancelling_history(
    tmp_path: Path,
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    snapshot = manager.start(start_request(target, tmp_path / "runs"))
    snapshot = manager.decide(decision_for(snapshot))
    terminal = manager.decide(decision_for(snapshot, Decision.REJECTED))
    session = manager._sessions[terminal.run_id]

    manager.shutdown()

    assert manager.get(terminal.run_id).cancellation_requested is False
    with pytest.raises(ApprovalGateError, match="closed"):
        session.approver.submit(
            ApprovalKind.PLAN,
            "a" * 64,
            Decision.APPROVED,
            None,
        )


def test_terminal_snapshot_waits_until_root_release_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    release_entered = threading.Event()
    allow_release = threading.Event()
    original_release = manager._release_root

    def blocked_release(run_id: str, root: Path) -> None:
        release_entered.set()
        assert allow_release.wait(timeout=5)
        original_release(run_id, root)

    monkeypatch.setattr(manager, "_release_root", blocked_release)
    request = start_request(target, tmp_path / "runs")
    snapshot = manager.start(request)
    snapshot = manager.decide(decision_for(snapshot))
    results: list[object] = []
    terminalizer = threading.Thread(
        target=lambda: results.append(
            manager.decide(decision_for(snapshot, Decision.REJECTED))
        )
    )
    terminalizer.start()
    getter = threading.Thread(target=lambda: results.append(manager.get(snapshot.run_id)))
    try:
        assert release_entered.wait(timeout=5)
        getter.start()
        getter.join(timeout=0.1)
        assert getter.is_alive()
        operation_lock_available = manager._sessions[
            snapshot.run_id
        ]._operation_lock.acquire(blocking=False)
        assert operation_lock_available is True
        manager._sessions[snapshot.run_id]._operation_lock.release()
        with pytest.raises(SessionError, match="active run"):
            manager.start(request)

        allow_release.set()
        terminalizer.join(timeout=5)
        getter.join(timeout=5)
        assert not terminalizer.is_alive()
        assert not getter.is_alive()
        assert len(results) == 2
        assert all(result.status is RunStatus.CANCELLED for result in results)  # type: ignore[union-attr]

        replacement = manager.start(request)
        manager.cancel(replacement.run_id)
    finally:
        allow_release.set()
        terminalizer.join(timeout=5)
        getter.join(timeout=5)
        manager.shutdown()


def test_cancel_rejects_after_workflow_returns_before_terminal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    release_entered = threading.Event()
    allow_release = threading.Event()
    original_release = manager._release_root

    def blocked_release(run_id: str, root: Path) -> None:
        release_entered.set()
        assert allow_release.wait(timeout=5)
        original_release(run_id, root)

    monkeypatch.setattr(manager, "_release_root", blocked_release)
    snapshot = manager.start(start_request(target, tmp_path / "runs"))
    snapshot = manager.decide(decision_for(snapshot))
    terminalizer = threading.Thread(
        target=lambda: manager.decide(decision_for(snapshot, Decision.REJECTED))
    )
    terminalizer.start()
    cancel_errors: list[BaseException] = []

    def cancel_run() -> None:
        try:
            manager.cancel(snapshot.run_id)
        except BaseException as error:
            cancel_errors.append(error)

    canceller = threading.Thread(target=cancel_run)
    try:
        assert release_entered.wait(timeout=5)
        canceller.start()
        canceller.join(timeout=0.1)
        assert not canceller.is_alive()
        assert len(cancel_errors) == 1
        assert isinstance(cancel_errors[0], SessionError)
        assert "terminal" in str(cancel_errors[0])
        assert manager._sessions[snapshot.run_id].cancellation_requested() is False
    finally:
        allow_release.set()
        terminalizer.join(timeout=5)
        canceller.join(timeout=5)
        manager.shutdown()


def test_cancel_rechecks_when_workflow_returns_between_check_and_event_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    snapshot = manager.start(start_request(target, tmp_path / "runs"))
    snapshot = manager.decide(decision_for(snapshot))
    assert snapshot.pending_approval is not None
    session = manager._sessions[snapshot.run_id]
    set_entered = threading.Event()
    allow_set = threading.Event()
    original_set = session._cancel.set

    def blocked_set() -> None:
        set_entered.set()
        assert allow_set.wait(timeout=5)
        original_set()

    monkeypatch.setattr(session._cancel, "set", blocked_set)
    cancel_errors: list[BaseException] = []

    def cancel_run() -> None:
        try:
            manager.cancel(snapshot.run_id)
        except BaseException as error:
            cancel_errors.append(error)

    canceller = threading.Thread(target=cancel_run)
    canceller.start()
    try:
        assert set_entered.wait(timeout=5)
        session.approver.submit(
            snapshot.pending_approval.kind,
            snapshot.pending_approval.digest,
            Decision.REJECTED,
            None,
        )
        deadline = time.monotonic() + 5
        while (
            session.prepared.workflow.manifest.status is RunStatus.RUNNING
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert session.prepared.workflow.manifest.status is RunStatus.CANCELLED

        allow_set.set()
        canceller.join(timeout=5)
        assert not canceller.is_alive()
        assert len(cancel_errors) == 1
        assert isinstance(cancel_errors[0], SessionError)
        assert "terminal" in str(cancel_errors[0])
        assert session.cancellation_requested() is False
    finally:
        allow_set.set()
        canceller.join(timeout=5)
        manager.shutdown()


def test_report_reader_requests_only_the_bounded_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    try:
        snapshot = manager.start(start_request(target, tmp_path / "runs"))
        terminal = manager.cancel(snapshot.run_id)
        report_path = Path(terminal.evidence_path) / "report.md"
        report_path.write_text("x" * 100_000)
        real_fdopen = os.fdopen
        guarded = False

        class GuardedReader:
            def __init__(self, handle) -> None:
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def read(self, size: int = -1) -> str:
                assert size == 64_001
                return self.handle.read(size)

        def guarded_fdopen(*args, **kwargs):
            nonlocal guarded
            guarded = True
            return GuardedReader(real_fdopen(*args, **kwargs))

        monkeypatch.setattr(os, "fdopen", guarded_fdopen)
        with pytest.raises(SessionError, match="64,000"):
            manager.get_report(snapshot.run_id)
        assert guarded is True
    finally:
        manager.shutdown()


def test_report_rejects_symlink_outside_evidence(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    try:
        snapshot = manager.start(start_request(target, tmp_path / "runs"))
        terminal = manager.cancel(snapshot.run_id)
        report_path = Path(terminal.evidence_path) / "report.md"
        secret = tmp_path / "outside-secret.md"
        secret.write_text("outside evidence")
        report_path.unlink()
        report_path.symlink_to(secret)

        with pytest.raises(SessionError, match="report"):
            manager.get_report(snapshot.run_id)
    finally:
        manager.shutdown()


def test_report_rejects_non_regular_fifo(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    writer_errors: list[OSError] = []
    writer: threading.Thread | None = None
    report_path: Path | None = None
    try:
        snapshot = manager.start(start_request(target, tmp_path / "runs"))
        terminal = manager.cancel(snapshot.run_id)
        report_path = Path(terminal.evidence_path) / "report.md"
        report_path.unlink()
        os.mkfifo(report_path)

        def write_fifo() -> None:
            try:
                report_path.write_text("not a regular report")
            except OSError as error:
                writer_errors.append(error)

        writer = threading.Thread(target=write_fifo)
        writer.start()
        with pytest.raises(SessionError, match="regular"):
            manager.get_report(snapshot.run_id)
    finally:
        if writer is not None and writer.is_alive() and report_path is not None:
            descriptor = os.open(report_path, os.O_RDONLY | os.O_NONBLOCK)
            os.close(descriptor)
        if writer is not None:
            writer.join(timeout=5)
        manager.shutdown()


def test_report_rejects_lstat_open_replacement_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    try:
        snapshot = manager.start(start_request(target, tmp_path / "runs"))
        terminal = manager.cancel(snapshot.run_id)
        report_path = Path(terminal.evidence_path) / "report.md"
        original_path = report_path.with_name("original-report.md")
        real_os_open = os.open
        replaced = False

        def replacing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal replaced
            if path == "report.md" and dir_fd is not None and not replaced:
                replaced = True
                report_path.replace(original_path)
                report_path.write_text("replacement report")
            return real_os_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", replacing_open)
        with pytest.raises(SessionError, match="changed"):
            manager.get_report(snapshot.run_id)
        assert replaced is True
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    "missing_capability",
    ["dir_fd", "follow_symlinks", "O_NOFOLLOW", "O_DIRECTORY"],
)
def test_report_fails_closed_when_secure_platform_capability_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_capability: str,
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder())
    try:
        snapshot = manager.start(start_request(target, tmp_path / "runs"))
        terminal = manager.cancel(snapshot.run_id)
        if missing_capability == "dir_fd":
            monkeypatch.setattr(os, "supports_dir_fd", set())
        elif missing_capability == "follow_symlinks":
            monkeypatch.setattr(os, "supports_follow_symlinks", set())
        else:
            monkeypatch.delattr(os, missing_capability)

        with pytest.raises(SessionError, match="secure report access is unavailable"):
            manager.get_report(terminal.run_id)
    finally:
        manager.shutdown()


def test_shutdown_cancels_closes_and_joins_all_workers(tmp_path: Path) -> None:
    first = make_target(tmp_path, "first")
    second = make_target(tmp_path, "second")
    manager = SessionManager(builder=make_builder(), shutdown_timeout_seconds=5)
    first_snapshot = manager.start(start_request(first, tmp_path / "first-runs"))
    second_snapshot = manager.start(start_request(second, tmp_path / "second-runs"))

    manager.shutdown()

    assert manager.get(first_snapshot.run_id).status is RunStatus.CANCELLED
    assert manager.get(second_snapshot.run_id).status is RunStatus.CANCELLED
    assert all(not session._thread.is_alive() for session in manager._sessions.values())


def test_shutdown_timeout_is_bounded_before_uncooperative_work_releases(
    tmp_path: Path,
) -> None:
    target = make_target(tmp_path)
    inspector = BlockingInspector()
    manager = SessionManager(
        builder=make_builder(inspector=inspector), shutdown_timeout_seconds=0.05
    )
    starter = threading.Thread(
        target=lambda: manager.start(start_request(target, tmp_path / "runs"))
    )
    starter.start()
    try:
        assert inspector.started.wait(timeout=5)
        started = time.monotonic()
        with pytest.raises(SessionError, match="did not stop"):
            manager.shutdown()
        assert time.monotonic() - started < 1
    finally:
        inspector.release.set()
        starter.join(timeout=5)
        manager.shutdown()
        assert not starter.is_alive()
        assert all(not session._thread.is_alive() for session in manager._sessions.values())


def test_shutdown_deadline_includes_operation_lock_cancellation_phase(
    tmp_path: Path,
) -> None:
    target = make_target(tmp_path)
    manager = SessionManager(builder=make_builder(), shutdown_timeout_seconds=0.05)
    snapshot = manager.start(start_request(target, tmp_path / "runs"))
    session = manager._sessions[snapshot.run_id]
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_operation_lock() -> None:
        with session._operation_lock:
            lock_held.set()
            assert release_lock.wait(timeout=5)

    holder = threading.Thread(target=hold_operation_lock)
    holder.start()
    shutdown_errors: list[BaseException] = []

    def stop_manager() -> None:
        try:
            manager.shutdown()
        except BaseException as error:
            shutdown_errors.append(error)

    stopper = threading.Thread(target=stop_manager)
    try:
        assert lock_held.wait(timeout=5)
        started = time.monotonic()
        stopper.start()
        stopper.join(timeout=0.5)
        assert not stopper.is_alive()
        assert time.monotonic() - started < 0.5
        assert len(shutdown_errors) == 1
        assert isinstance(shutdown_errors[0], SessionError)
        assert "timeout" in str(shutdown_errors[0])
    finally:
        release_lock.set()
        holder.join(timeout=5)
        stopper.join(timeout=5)
        manager.shutdown()
        assert not holder.is_alive()
        assert not stopper.is_alive()
        assert all(not item._thread.is_alive() for item in manager._sessions.values())


def test_shutdown_during_preparation_prevents_late_worker_start(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def block_build() -> None:
        entered.set()
        assert release.wait(timeout=5)

    manager = SessionManager(builder=make_builder(before_build=block_build))
    errors: list[BaseException] = []

    def start_run() -> None:
        try:
            manager.start(start_request(target, tmp_path / "runs"))
        except BaseException as error:
            errors.append(error)

    starter = threading.Thread(target=start_run)
    starter.start()
    try:
        assert entered.wait(timeout=5)
        manager.shutdown()
        release.set()
        starter.join(timeout=1)
        assert not starter.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], SessionError)
        assert "shut down" in str(errors[0])
    finally:
        release.set()
        manager.shutdown()
        starter.join(timeout=5)
        assert not starter.is_alive()
        assert all(not session._thread.is_alive() for session in manager._sessions.values())


def test_shutdown_cannot_join_between_registration_and_worker_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_target(tmp_path)
    worker_start_entered = threading.Event()
    release_worker_start = threading.Event()
    real_thread_start = threading.Thread.start

    def controlled_thread_start(thread: threading.Thread) -> None:
        if thread.name.startswith("repogent-session-"):
            worker_start_entered.set()
            assert release_worker_start.wait(timeout=5)
        real_thread_start(thread)

    monkeypatch.setattr(threading.Thread, "start", controlled_thread_start)
    manager = SessionManager(builder=make_builder())
    start_errors: list[BaseException] = []
    shutdown_errors: list[BaseException] = []

    def start_run() -> None:
        try:
            manager.start(start_request(target, tmp_path / "runs"))
        except BaseException as error:
            start_errors.append(error)

    def stop_manager() -> None:
        try:
            manager.shutdown()
        except BaseException as error:
            shutdown_errors.append(error)

    starter = threading.Thread(target=start_run)
    stopper = threading.Thread(target=stop_manager)
    starter.start()
    try:
        assert worker_start_entered.wait(timeout=5)
        stopper.start()
        stopper.join(timeout=0.1)
        assert stopper.is_alive()
        release_worker_start.set()
        starter.join(timeout=5)
        stopper.join(timeout=5)
        assert not starter.is_alive()
        assert not stopper.is_alive()
        assert start_errors == []
        assert shutdown_errors == []
        assert all(not session._thread.is_alive() for session in manager._sessions.values())
    finally:
        release_worker_start.set()
        starter.join(timeout=5)
        stopper.join(timeout=5)
        manager.shutdown()


def test_worker_start_failure_releases_session_and_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = make_target(tmp_path)
    real_thread_start = threading.Thread.start
    fail_worker_start = True

    def controlled_thread_start(thread: threading.Thread) -> None:
        nonlocal fail_worker_start
        if thread.name.startswith("repogent-session-") and fail_worker_start:
            fail_worker_start = False
            raise RuntimeError("worker start failed")
        real_thread_start(thread)

    monkeypatch.setattr(threading.Thread, "start", controlled_thread_start)
    manager = SessionManager(builder=make_builder())
    request = start_request(target, tmp_path / "runs")
    try:
        with pytest.raises(RuntimeError, match="worker start failed"):
            manager.start(request)
        assert manager._sessions == {}
        snapshot = manager.start(request)
        assert snapshot.pending_approval is not None
        manager.cancel(snapshot.run_id)
    finally:
        manager.shutdown()
