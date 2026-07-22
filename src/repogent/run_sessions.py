from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from repogent.approval_gate import ApprovalGateError, GateApprover
from repogent.approvals import Approver
from repogent.domain import PendingApproval, RunManifest, RunStatus
from repogent.mcp_models import RunDecision, RunReport, RunSnapshot, RunStart
from repogent.run_builder import PreparedRun, RunOptions, build_run

_ORIGINAL_OS_OPEN = os.open
_ORIGINAL_OS_STAT = os.stat


class SessionError(RuntimeError):
    pass


class _CancellationEvent(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self._observed = threading.Event()

    def is_set(self) -> bool:
        requested = super().is_set()
        if requested:
            self._observed.set()
        return requested

    def requested(self) -> bool:
        return super().is_set()

    def was_observed(self) -> bool:
        return self._observed.is_set()


class RunBuilder(Protocol):
    def __call__(
        self,
        options: RunOptions,
        approver_factory: Callable[[str], Approver],
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> PreparedRun: ...


class RunSession:
    _WAIT_INTERVAL_SECONDS = 0.05

    def __init__(
        self,
        prepared: PreparedRun,
        approver: GateApprover,
        cancel_event: threading.Event,
        on_done: Callable[[str, Path], None],
    ) -> None:
        self.prepared = prepared
        self.approver = approver
        self._cancel = cancel_event
        self._on_done = on_done
        self._root = prepared.workflow.root.resolve(strict=True)
        self._evidence_identity = self._capture_evidence_identity()
        self._done = threading.Event()
        self._root_released = threading.Event()
        self._result: RunManifest | None = None
        self._workflow_finished = False
        self._terminal = False
        self._pending: PendingApproval | None = None
        self._generation = 0
        self._operation_lock = threading.RLock()
        self._wait_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name=f"repogent-session-{prepared.manifest.run_id}",
            daemon=True,
        )

    def cancellation_requested(self) -> bool:
        if isinstance(self._cancel, _CancellationEvent):
            return self._cancel.requested()
        return self._cancel.is_set()

    def start(self) -> RunSnapshot:
        self.start_worker()
        return self.wait_for_change()

    def start_worker(self) -> None:
        self._thread.start()

    def snapshot(self) -> RunSnapshot:
        while True:
            with self._operation_lock:
                manifest = self._result or self.prepared.workflow.manifest
                if (
                    manifest.status is RunStatus.RUNNING
                    or self._root_released.is_set()
                ):
                    return self._snapshot(manifest)
            self._root_released.wait()

    def decide(self, decision: RunDecision) -> RunSnapshot:
        with self._operation_lock:
            try:
                self.approver.submit(
                    decision.kind,
                    decision.digest,
                    decision.decision,
                    decision.feedback,
                )
            except ApprovalGateError as error:
                raise SessionError(str(error)) from error
            self._pending = None
        return self.wait_for_change()

    def cancel(self) -> RunSnapshot:
        if not self.request_cancel():
            raise SessionError("run is already terminal")
        return self.wait_for_change()

    def request_cancel(self) -> bool:
        with self._operation_lock:
            return self._request_cancel_locked()

    def request_shutdown(self, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            acquired = self._operation_lock.acquire(blocking=False)
        else:
            acquired = self._operation_lock.acquire(timeout=remaining)
        try:
            if acquired:
                self._request_cancel_locked()
        finally:
            if acquired:
                self._operation_lock.release()
        self.approver.close()
        return acquired and time.monotonic() <= deadline

    def close(self) -> None:
        self.approver.close()

    def wait_for_change(self) -> RunSnapshot:
        with self._wait_lock:
            while not self._done.is_set():
                with self._operation_lock:
                    generation = self._generation
                generation, pending = self.approver.wait(
                    after_generation=generation,
                    timeout_seconds=self._WAIT_INTERVAL_SECONDS,
                )
                with self._operation_lock:
                    if generation > self._generation:
                        self._generation = generation
                        self._pending = pending
                        if pending is not None:
                            return self._snapshot()
                self._done.wait(self._WAIT_INTERVAL_SECONDS)
            with self._operation_lock:
                self._pending = None
                return self._snapshot()

    def join(self, timeout_seconds: float) -> None:
        self._thread.join(timeout=max(0.0, timeout_seconds))

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def is_done(self) -> bool:
        return self._done.is_set()

    def read_report(self) -> str:
        if not _supports_secure_report_access():
            raise SessionError("secure report access is unavailable on this platform")
        root_descriptor = -1
        report_descriptor = -1
        try:
            inspected_root = os.stat(
                self.prepared.store.root,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(inspected_root.st_mode):
                raise SessionError("run evidence path is not a directory")
            if _identity(inspected_root) != self._evidence_identity:
                raise SessionError("run evidence directory changed after preparation")

            root_descriptor = os.open(
                self.prepared.store.root,
                _read_flags(directory=True),
            )
            opened_root = os.fstat(root_descriptor)
            if not stat.S_ISDIR(opened_root.st_mode):
                raise SessionError("opened run evidence path is not a directory")
            if _identity(opened_root) != _identity(inspected_root):
                raise SessionError("run evidence directory changed while it was opened")

            inspected_report = os.stat(
                "report.md",
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(inspected_report.st_mode):
                raise SessionError("run report must be a regular file")
            report_descriptor = os.open(
                "report.md",
                _read_flags(),
                dir_fd=root_descriptor,
            )
            opened_report = os.fstat(report_descriptor)
            if not stat.S_ISREG(opened_report.st_mode):
                raise SessionError("opened run report is not a regular file")
            if _identity(opened_report) != _identity(inspected_report):
                raise SessionError("run report changed while it was opened")

            with os.fdopen(
                report_descriptor,
                "r",
                encoding="utf-8",
            ) as handle:
                report_descriptor = -1
                return handle.read(64_001)
        except SessionError:
            raise
        except (OSError, UnicodeError) as error:
            raise SessionError(f"run report is unavailable: {error}") from error
        finally:
            if report_descriptor >= 0:
                os.close(report_descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)

    def _run(self) -> None:
        result: RunManifest | None = None
        try:
            result = self.prepared.workflow.run()
        finally:
            with self._operation_lock:
                self._workflow_finished = True
            try:
                self._on_done(self.prepared.manifest.run_id, self._root)
            finally:
                self._root_released.set()
                with self._operation_lock:
                    self._result = result
                    self._terminal = True
                    self._done.set()

    def _snapshot(self, manifest: RunManifest | None = None) -> RunSnapshot:
        manifest = manifest or self._result or self.prepared.workflow.manifest
        pending = (
            self._pending.model_copy(deep=True) if self._pending is not None else None
        )
        return RunSnapshot(
            run_id=manifest.run_id,
            status=manifest.status,
            stage=manifest.stage,
            pending_approval=pending,
            checkout_state=manifest.checkout_state,
            selected_patch_applied=manifest.selected_patch_applied,
            applied_paths=list(manifest.applied_paths),
            final_validation_status=manifest.final_validation_status,
            reason=manifest.reason,
            evidence_path=str(self.prepared.store.root),
            cancellation_requested=self.cancellation_requested(),
        )

    def _capture_evidence_identity(self) -> tuple[int, int]:
        try:
            evidence = os.stat(self.prepared.store.root, follow_symlinks=False)
        except OSError as error:
            raise SessionError(f"run evidence directory is unavailable: {error}") from error
        if not stat.S_ISDIR(evidence.st_mode):
            raise SessionError("run evidence path is not a directory")
        return _identity(evidence)

    def _cancellation_was_observed(self) -> bool:
        return isinstance(self._cancel, _CancellationEvent) and self._cancel.was_observed()

    def _request_cancel_locked(self) -> bool:
        if (
            self._terminal
            or self._workflow_finished
            or self.prepared.workflow.manifest.status is not RunStatus.RUNNING
        ):
            return False
        self._cancel.set()
        if (
            self.prepared.workflow.manifest.status is not RunStatus.RUNNING
            and not self._cancellation_was_observed()
        ):
            self._cancel.clear()
            return False
        self.approver.close()
        self._pending = None
        return True


class SessionManager:
    def __init__(
        self,
        *,
        builder: RunBuilder = build_run,
        shutdown_timeout_seconds: float = 10.0,
    ) -> None:
        self._builder = builder
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._lock = threading.RLock()
        self._sessions: dict[str, RunSession] = {}
        self._active_roots: dict[Path, str | None] = {}
        self._closed = False

    def start(self, request: RunStart) -> RunSnapshot:
        root = request.repository.resolve(strict=True)
        with self._lock:
            if self._closed:
                raise SessionError("session manager has been shut down")
            if root in self._active_roots:
                active_run = self._active_roots[root]
                if active_run is None:
                    raise SessionError(
                        "another run is being prepared for this repository"
                    )
                raise SessionError(
                    f"repository already has an active run: {active_run}"
                )
            self._active_roots[root] = None

        registered = False
        session: RunSession | None = None
        try:
            cancel_event = _CancellationEvent()
            prepared = self._builder(
                RunOptions(
                    repository=root,
                    request=request.request,
                    provider=request.provider,
                    model=request.model,
                    script=request.script,
                    executor=request.executor,
                    output_dir=request.output_dir,
                ),
                GateApprover,
                cancel_requested=cancel_event.is_set,
            )
            prepared_root = prepared.workflow.root.resolve(strict=True)
            if prepared_root != root:
                raise SessionError("prepared run repository does not match requested repository")
            approver = cast(GateApprover, prepared.approver)
            session = RunSession(
                prepared,
                approver,
                cancel_event,
                self._release_root,
            )
            run_id = prepared.manifest.run_id
            with self._lock:
                if self._closed:
                    raise SessionError("session manager has been shut down")
                if run_id in self._sessions:
                    raise SessionError(f"run already exists: {run_id}")
                self._active_roots[root] = run_id
                self._sessions[run_id] = session
                try:
                    session.start_worker()
                except BaseException:
                    self._sessions.pop(run_id, None)
                    self._active_roots.pop(root, None)
                    raise
                registered = True
            return session.wait_for_change()
        except BaseException:
            if registered and session is not None:
                session.request_cancel()
                session.join(self._shutdown_timeout_seconds)
                with self._lock:
                    if self._sessions.get(session.prepared.manifest.run_id) is session:
                        self._sessions.pop(session.prepared.manifest.run_id, None)
            raise
        finally:
            if not registered:
                with self._lock:
                    if self._active_roots.get(root) is None:
                        self._active_roots.pop(root, None)

    def get(self, run_id: str) -> RunSnapshot:
        return self._get_session(run_id).snapshot()

    def decide(self, decision: RunDecision) -> RunSnapshot:
        return self._get_session(decision.run_id).decide(decision)

    def cancel(self, run_id: str) -> RunSnapshot:
        return self._get_session(run_id).cancel()

    def get_report(self, run_id: str) -> RunReport:
        session = self._get_session(run_id)
        if not session.is_done():
            raise SessionError("run is not terminal")
        snapshot = session.snapshot()
        report = session.read_report()
        if len(report) > 64_000:
            raise SessionError("run report exceeds 64,000 characters")
        return RunReport(
            run_id=run_id,
            status=snapshot.status,
            checkout_state=snapshot.checkout_state,
            evidence_path=snapshot.evidence_path,
            report=report,
        )

    def shutdown(self) -> None:
        deadline = time.monotonic() + max(0.0, self._shutdown_timeout_seconds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            acquired = self._lock.acquire(blocking=False)
        else:
            acquired = self._lock.acquire(timeout=remaining)
        if not acquired:
            raise SessionError("session shutdown timeout acquiring manager lock")
        try:
            self._closed = True
            sessions = list(self._sessions.values())
        finally:
            self._lock.release()
        timed_out = False
        for session in sessions:
            if not session.request_shutdown(deadline):
                timed_out = True
        for session in sessions:
            session.join(deadline - time.monotonic())
        if timed_out or any(session.is_alive() for session in sessions):
            raise SessionError("session workers did not stop before shutdown timeout")

    def _get_session(self, run_id: str) -> RunSession:
        with self._lock:
            try:
                return self._sessions[run_id]
            except KeyError as error:
                raise SessionError(f"unknown run: {run_id}") from error

    def _release_root(self, run_id: str, root: Path) -> None:
        with self._lock:
            if self._active_roots.get(root) == run_id:
                self._active_roots.pop(root, None)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _read_flags(*, directory: bool = False) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    return flags


def _supports_secure_report_access() -> bool:
    return (
        _ORIGINAL_OS_OPEN in os.supports_dir_fd
        and _ORIGINAL_OS_STAT in os.supports_dir_fd
        and _ORIGINAL_OS_STAT in os.supports_follow_symlinks
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )
