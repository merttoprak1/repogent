from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from repogent.agents import RoleSet
from repogent.approvals import Approver
from repogent.artifacts import ArtifactStore
from repogent.domain import (
    ApprovalKind,
    Budget,
    CheckoutState,
    CheckResult,
    CheckStatus,
    Decision,
    RunManifest,
    RunStatus,
    ValidationReport,
)
from repogent.mcp_models import RunDecision, RunStart
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


class PassingValidator:
    def run(
        self, root: Path, *, timeout_seconds: float | None = None
    ) -> ValidationReport:
        del root, timeout_seconds
        return ValidationReport(
            checks=[CheckResult(name="pytest", argv=["pytest"], status=CheckStatus.PASSED)]
        )


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
) -> Callable[..., PreparedRun]:
    counter = 0

    def builder(
        options: RunOptions,
        approver_factory: Callable[[str], Approver],
        *,
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
        workflow = Workflow(
            root=root,
            request=options.request,
            manifest=manifest,
            roles=RoleSet.from_provider(
                ScriptedProvider([REQUIREMENTS_OUTPUT, PLAN_OUTPUT])
            ),
            approver=approver,
            patch_policy=PatchPolicy(),
            patch_applier=PatchApplier(),
            validator=PassingValidator(),
            artifacts=store,
            inspector=inspector or RepositoryInspector(),  # type: ignore[arg-type]
            budget=Budget(),
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


def decision_for(snapshot, decision: Decision = Decision.APPROVED) -> RunDecision:
    assert snapshot.pending_approval is not None
    return RunDecision(
        run_id=snapshot.run_id,
        kind=snapshot.pending_approval.kind,
        digest=snapshot.pending_approval.digest,
        decision=decision,
    )


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
        real_open = Path.open

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

        def guarded_open(path: Path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            return GuardedReader(handle) if path == report_path else handle

        monkeypatch.setattr(Path, "open", guarded_open)
        with pytest.raises(SessionError, match="64,000"):
            manager.get_report(snapshot.run_id)
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
