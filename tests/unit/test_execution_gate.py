from __future__ import annotations

import hashlib
import threading
from concurrent.futures import Future
from pathlib import Path

import pytest
from pydantic import ValidationError

from repogent.candidates import PatchPreview
from repogent.domain import (
    CandidateRecord,
    Decision,
    ExecutionMode,
    IsolationLevel,
    PatchProposal,
    ProviderUsage,
    VerificationStatus,
)
from repogent.execution import ValidationPolicy
from repogent.execution_gate import ExecutionGateError, GateExecutorSelector
from repogent.executor_selection import (
    LOCAL_RISK_STATEMENT,
    ExecutorSelectionError,
    PreparedExecutor,
)
from repogent.mcp_models import (
    ExecutionDecision,
    ExecutorAvailability,
    ExecutorOption,
    PendingExecutionChoice,
)
from repogent.preflight import PreflightReport
from repogent.workflow import ExecutorSelectionRejected, WorkflowCancelled


class RecordingRegistry:
    def __init__(self) -> None:
        self.prepare_calls: list[ExecutionMode] = []
        self.prepare_error: ExecutorSelectionError | None = None
        self.availability = [
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

    def inspect_availability(
        self, root: Path, policy: ValidationPolicy
    ) -> list[ExecutorAvailability]:
        del root, policy
        return [item.model_copy(deep=True) for item in self.availability]

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
                    (
                        f"{run_id}:{preview_digest}:{item.mode.value}:"
                        f"{item.risk_statement or ''}"
                    ).encode()
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
        if self.prepare_error is not None:
            raise self.prepare_error
        return PreparedExecutor(
            mode=mode,
            isolation_level=(
                IsolationLevel.ISOLATED
                if mode is ExecutionMode.DOCKER
                else IsolationLevel.REDUCED_ISOLATION
            ),
            preflight=PreflightReport(
                checks=[],
                git_commit=None,
                dirty=False,
                repository_fingerprint="repository",
            ),
            validator=object(),  # type: ignore[arg-type]
        )


class BlockingPrepareRegistry(RecordingRegistry):
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


class UnexpectedOnceRegistry(RecordingRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def prepare(
        self, root: Path, mode: ExecutionMode, policy: ValidationPolicy
    ) -> PreparedExecutor:
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("unexpected readiness failure")
        return super().prepare(root, mode, policy)


class OverlappingPrepareRegistry(RecordingRegistry):
    def __init__(self) -> None:
        super().__init__()
        self._call_lock = threading.Lock()
        self._calls = 0
        self.old_entered = threading.Event()
        self.release_old = threading.Event()
        self.new_entered = threading.Event()
        self.release_new = threading.Event()

    def prepare(
        self, root: Path, mode: ExecutionMode, policy: ValidationPolicy
    ) -> PreparedExecutor:
        with self._call_lock:
            self._calls += 1
            call = self._calls
        if call == 1:
            self.old_entered.set()
            assert self.release_old.wait(timeout=5)
        elif call == 2:
            self.new_entered.set()
            assert self.release_new.wait(timeout=5)
        return super().prepare(root, mode, policy)


def preview(*, replacement: str = "value = 2") -> PatchPreview:
    diff = (
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
        f"-value = 1\n+{replacement}\n"
    )
    proposal = PatchProposal(
        summary="Change value",
        diff=diff,
        acceptance_criteria_addressed=["value is 2"],
    )
    candidate = CandidateRecord(
        candidate_id="candidate-1",
        proposal=proposal,
        generation_reason="initial implementation",
        diff_sha256=hashlib.sha256(diff.encode()).hexdigest(),
        usage=ProviderUsage(model="scripted"),
    )
    return PatchPreview(
        candidate=candidate,
        touched_paths=["app.py"],
        changed_files=1,
        changed_lines=2,
        acceptance_criteria_coverage=1,
        verification_status=VerificationStatus.UNVALIDATED,
    )


def start_selection(
    gate: GateExecutorSelector,
    patch_preview: PatchPreview,
    *,
    timeout_seconds: float = 5,
) -> tuple[threading.Thread, Future[PreparedExecutor]]:
    outcome: Future[PreparedExecutor] = Future()

    def select() -> None:
        try:
            outcome.set_result(
                gate.select(patch_preview, timeout_seconds=timeout_seconds)
            )
        except BaseException as error:
            outcome.set_exception(error)

    worker = threading.Thread(target=select)
    worker.start()
    return worker, outcome


def pending_choice(gate: GateExecutorSelector) -> PendingExecutionChoice:
    generation, pending = gate.wait(after_generation=0, timeout_seconds=1)
    assert generation == 1
    assert pending is not None
    return pending


def option(
    pending: PendingExecutionChoice, mode: ExecutionMode
) -> ExecutorOption:
    return next(item for item in pending.options if item.mode is mode)


def local_decision(
    pending: PendingExecutionChoice,
    selected: ExecutorOption,
    *,
    run_id: str | None = None,
    preview_digest: str | None = None,
    option_digest: str | None = None,
    decision: Decision = Decision.APPROVED,
) -> ExecutionDecision:
    return ExecutionDecision(
        run_id=run_id or pending.run_id,
        preview_digest=preview_digest or pending.preview_digest,
        mode=selected.mode,
        option_digest=option_digest or selected.option_digest,
        decision=decision,
    )


def close_and_join(gate: GateExecutorSelector, worker: threading.Thread) -> None:
    gate.close()
    worker.join(timeout=1)
    assert not worker.is_alive()


def test_local_selection_requires_matching_preview_and_option_digest(
    tmp_path: Path,
) -> None:
    registry = RecordingRegistry()
    gate = GateExecutorSelector("run-1", tmp_path, ValidationPolicy(), registry)
    worker, outcome = start_selection(gate, preview())
    pending = pending_choice(gate)
    local = option(pending, ExecutionMode.LOCAL)
    try:
        with pytest.raises(ExecutionGateError, match="preview digest mismatch"):
            gate.submit(
                local_decision(pending, local, preview_digest="f" * 64)
            )
        with pytest.raises(ExecutionGateError, match="option digest mismatch"):
            gate.submit(local_decision(pending, local, option_digest="e" * 64))

        gate.submit(local_decision(pending, local))

        assert outcome.result(timeout=1).mode is ExecutionMode.LOCAL
        assert registry.prepare_calls == [ExecutionMode.LOCAL]
    finally:
        close_and_join(gate, worker)


def test_plain_approval_without_local_option_digest_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExecutionDecision(
            run_id="run-1",
            preview_digest="a" * 64,
            mode=ExecutionMode.LOCAL,
            option_digest="",
            decision=Decision.APPROVED,
        )


@pytest.mark.parametrize(
    ("field", "expected"),
    [("run_id", "run ID mismatch"), ("mode", "mode mismatch")],
)
def test_selection_rejects_decision_for_another_gate_binding(
    tmp_path: Path, field: str, expected: str
) -> None:
    gate = GateExecutorSelector(
        "run-1", tmp_path, ValidationPolicy(), RecordingRegistry()
    )
    worker, _outcome = start_selection(gate, preview())
    pending = pending_choice(gate)
    selected = option(pending, ExecutionMode.LOCAL)
    decision = local_decision(pending, selected)
    decision = decision.model_copy(
        update={
            field: "run-2" if field == "run_id" else ExecutionMode.DOCKER
        }
    )
    try:
        with pytest.raises(ExecutionGateError, match=expected):
            gate.submit(decision)
    finally:
        close_and_join(gate, worker)


def test_unavailable_option_is_rejected_without_prepare(tmp_path: Path) -> None:
    registry = RecordingRegistry()
    registry.availability[1] = registry.availability[1].model_copy(
        update={"available": False, "message": "Local validation is unavailable"}
    )
    gate = GateExecutorSelector("run-1", tmp_path, ValidationPolicy(), registry)
    worker, _outcome = start_selection(gate, preview())
    pending = pending_choice(gate)
    local = option(pending, ExecutionMode.LOCAL)
    try:
        with pytest.raises(ExecutionGateError, match="selected executor is unavailable"):
            gate.submit(local_decision(pending, local))
        assert registry.prepare_calls == []
    finally:
        close_and_join(gate, worker)


def test_prepare_rechecks_readiness_before_releasing_selector(tmp_path: Path) -> None:
    registry = RecordingRegistry()
    registry.prepare_error = ExecutorSelectionError(
        "selected executor is unavailable"
    )
    gate = GateExecutorSelector("run-1", tmp_path, ValidationPolicy(), registry)
    worker, _outcome = start_selection(gate, preview())
    pending = pending_choice(gate)
    local = option(pending, ExecutionMode.LOCAL)
    try:
        with pytest.raises(ExecutionGateError, match="selected executor is unavailable"):
            gate.submit(local_decision(pending, local))
        assert registry.prepare_calls == [ExecutionMode.LOCAL]
    finally:
        close_and_join(gate, worker)


def test_unexpected_prepare_failure_releases_generation_for_retry(
    tmp_path: Path,
) -> None:
    registry = UnexpectedOnceRegistry()
    gate = GateExecutorSelector("run-1", tmp_path, ValidationPolicy(), registry)
    worker, outcome = start_selection(gate, preview())
    pending = pending_choice(gate)
    local = option(pending, ExecutionMode.LOCAL)
    try:
        with pytest.raises(ExecutionGateError, match="executor preparation failed"):
            gate.submit(local_decision(pending, local))

        gate.submit(local_decision(pending, local))

        assert outcome.result(timeout=1).mode is ExecutionMode.LOCAL
        assert registry.attempts == 2
    finally:
        close_and_join(gate, worker)


def test_rejected_selection_wakes_worker_with_selection_rejection(
    tmp_path: Path,
) -> None:
    gate = GateExecutorSelector(
        "run-1", tmp_path, ValidationPolicy(), RecordingRegistry()
    )
    worker, outcome = start_selection(gate, preview())
    pending = pending_choice(gate)
    local = option(pending, ExecutionMode.LOCAL)
    try:
        gate.submit(
            local_decision(pending, local, decision=Decision.REJECTED)
        )

        with pytest.raises(ExecutorSelectionRejected, match="rejected"):
            outcome.result(timeout=1)
    finally:
        close_and_join(gate, worker)


def test_close_wakes_selector_and_public_waiters(tmp_path: Path) -> None:
    gate = GateExecutorSelector(
        "run-1", tmp_path, ValidationPolicy(), RecordingRegistry()
    )
    worker, outcome = start_selection(gate, preview())
    pending_choice(gate)

    gate.close()

    with pytest.raises(WorkflowCancelled, match="closed"):
        outcome.result(timeout=1)
    generation, pending = gate.wait(after_generation=0, timeout_seconds=1)
    assert generation == 2
    assert pending is None
    worker.join(timeout=1)
    assert not worker.is_alive()


def test_close_is_not_blocked_by_readiness_recheck(tmp_path: Path) -> None:
    registry = BlockingPrepareRegistry()
    gate = GateExecutorSelector("run-1", tmp_path, ValidationPolicy(), registry)
    worker, outcome = start_selection(gate, preview())
    pending = pending_choice(gate)
    local = option(pending, ExecutionMode.LOCAL)
    submit_errors: list[BaseException] = []

    def submit() -> None:
        try:
            gate.submit(local_decision(pending, local))
        except BaseException as error:
            submit_errors.append(error)

    submitter = threading.Thread(target=submit)
    closer = threading.Thread(target=gate.close)
    submitter.start()
    try:
        assert registry.prepare_entered.wait(timeout=1)
        closer.start()
        closer.join(timeout=0.2)
        assert not closer.is_alive()
        with pytest.raises(WorkflowCancelled, match="closed"):
            outcome.result(timeout=1)
    finally:
        registry.release_prepare.set()
        submitter.join(timeout=1)
        closer.join(timeout=1)
        worker.join(timeout=1)
    assert not submitter.is_alive()
    assert not worker.is_alive()
    assert len(submit_errors) == 1
    assert isinstance(submit_errors[0], ExecutionGateError)
    assert "closed" in str(submit_errors[0])


def test_gate_rejects_preview_changed_by_recursive_sanitization(
    tmp_path: Path,
) -> None:
    registry = RecordingRegistry()
    gate = GateExecutorSelector("run-1", tmp_path, ValidationPolicy(), registry)

    with pytest.raises(ExecutionGateError, match="unsafe to display"):
        gate.select(
            preview(replacement="api_key = 'super-secret-value'"),
            timeout_seconds=1,
        )

    assert gate.wait(after_generation=0, timeout_seconds=0) == (0, None)
    assert registry.prepare_calls == []


def test_resolved_generation_rejects_duplicate_or_stale_decision(
    tmp_path: Path,
) -> None:
    gate = GateExecutorSelector(
        "run-1", tmp_path, ValidationPolicy(), RecordingRegistry()
    )
    worker, outcome = start_selection(gate, preview())
    pending = pending_choice(gate)
    local = option(pending, ExecutionMode.LOCAL)
    decision = local_decision(pending, local)
    try:
        gate.submit(decision)
        assert outcome.result(timeout=1).mode is ExecutionMode.LOCAL

        with pytest.raises(ExecutionGateError, match="no executor selection is pending"):
            gate.submit(decision)
    finally:
        close_and_join(gate, worker)


def test_identical_preview_rejects_decision_from_earlier_generation(
    tmp_path: Path,
) -> None:
    gate = GateExecutorSelector(
        "run-1", tmp_path, ValidationPolicy(), RecordingRegistry()
    )
    first_worker, first_outcome = start_selection(gate, preview())
    first_generation, first_pending = gate.wait(
        after_generation=0, timeout_seconds=1
    )
    assert first_pending is not None
    first_local = option(first_pending, ExecutionMode.LOCAL)
    stale_approval = local_decision(first_pending, first_local)
    gate.submit(
        local_decision(
            first_pending,
            first_local,
            decision=Decision.REJECTED,
        )
    )
    with pytest.raises(ExecutorSelectionRejected, match="rejected"):
        first_outcome.result(timeout=1)
    first_worker.join(timeout=1)
    assert not first_worker.is_alive()

    second_worker, second_outcome = start_selection(gate, preview())
    second_generation, second_pending = gate.wait(
        after_generation=first_generation,
        timeout_seconds=1,
    )
    assert second_generation > first_generation
    assert second_pending is not None
    second_local = option(second_pending, ExecutionMode.LOCAL)
    try:
        with pytest.raises(ExecutionGateError, match="option digest mismatch"):
            gate.submit(stale_approval)
        assert second_local.option_digest != first_local.option_digest

        gate.submit(local_decision(second_pending, second_local))

        assert second_outcome.result(timeout=1).mode is ExecutionMode.LOCAL
    finally:
        close_and_join(gate, second_worker)


def test_obsolete_prepare_cannot_clear_newer_generation_reservation(
    tmp_path: Path,
) -> None:
    registry = OverlappingPrepareRegistry()
    gate = GateExecutorSelector("run-1", tmp_path, ValidationPolicy(), registry)
    old_worker, old_outcome = start_selection(
        gate, preview(), timeout_seconds=0.1
    )
    old_generation, old_pending = gate.wait(
        after_generation=0, timeout_seconds=1
    )
    assert old_pending is not None
    old_local = option(old_pending, ExecutionMode.LOCAL)
    old_errors: list[BaseException] = []
    old_done = threading.Event()

    def submit_old() -> None:
        try:
            gate.submit(local_decision(old_pending, old_local))
        except BaseException as error:
            old_errors.append(error)
        finally:
            old_done.set()

    old_submitter = threading.Thread(target=submit_old)
    old_submitter.start()
    assert registry.old_entered.wait(timeout=1)
    with pytest.raises(ExecutorSelectionRejected, match="timed out"):
        old_outcome.result(timeout=1)
    old_worker.join(timeout=1)
    assert not old_worker.is_alive()

    new_worker, new_outcome = start_selection(gate, preview())
    new_generation, new_pending = gate.wait(
        after_generation=old_generation,
        timeout_seconds=1,
    )
    assert new_generation > old_generation
    assert new_pending is not None
    new_local = option(new_pending, ExecutionMode.LOCAL)
    new_errors: list[BaseException] = []

    def submit_new() -> None:
        try:
            gate.submit(local_decision(new_pending, new_local))
        except BaseException as error:
            new_errors.append(error)

    new_submitter = threading.Thread(target=submit_new)
    new_submitter.start()
    try:
        assert registry.new_entered.wait(timeout=1)
        registry.release_old.set()
        assert old_done.wait(timeout=1)
        with pytest.raises(ExecutionGateError, match="already been submitted"):
            gate.submit(local_decision(new_pending, new_local))

        registry.release_new.set()
        assert new_outcome.result(timeout=1).mode is ExecutionMode.LOCAL
    finally:
        registry.release_old.set()
        registry.release_new.set()
        old_submitter.join(timeout=1)
        new_submitter.join(timeout=1)
        close_and_join(gate, new_worker)
    assert len(old_errors) == 1
    assert isinstance(old_errors[0], ExecutionGateError)
    assert "generation changed" in str(old_errors[0])
    assert new_errors == []


def test_timeout_advances_generation_to_publish_pending_removal(
    tmp_path: Path,
) -> None:
    gate = GateExecutorSelector(
        "run-1", tmp_path, ValidationPolicy(), RecordingRegistry()
    )
    worker, outcome = start_selection(gate, preview(), timeout_seconds=0.1)
    generation, pending = gate.wait(after_generation=0, timeout_seconds=1)
    assert pending is not None

    with pytest.raises(ExecutorSelectionRejected, match="timed out"):
        outcome.result(timeout=1)
    removal_generation, removed = gate.wait(
        after_generation=generation,
        timeout_seconds=0.2,
    )

    assert removal_generation > generation
    assert removed is None
    worker.join(timeout=1)
    assert not worker.is_alive()
