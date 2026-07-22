from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from repogent.candidates import PatchPreview, patch_preview_digest
from repogent.domain import Decision, ExecutionMode
from repogent.execution import ValidationPolicy
from repogent.executor_selection import (
    ExecutorRegistry,
    ExecutorSelectionError,
    PreparedExecutor,
    validate_executor_isolation,
)
from repogent.mcp_models import ExecutionDecision, PendingExecutionChoice
from repogent.sanitization import sanitize_data
from repogent.workflow import ExecutorSelectionRejected, WorkflowCancelled


class ExecutionGateError(RuntimeError):
    pass


class GateExecutorSelector:
    def __init__(
        self,
        run_id: str,
        root: Path,
        policy: ValidationPolicy,
        registry: ExecutorRegistry,
    ) -> None:
        self.run_id = run_id
        self._root = root.resolve(strict=True)
        self._policy = policy
        self._registry = registry
        self._condition = threading.Condition()
        self._generation = 0
        self._pending: PendingExecutionChoice | None = None
        self._pending_generation: int | None = None
        self._preparing_generation: int | None = None
        self._prepared: PreparedExecutor | None = None
        self._rejection: str | None = None
        self._closed = False

    def select(
        self,
        preview: PatchPreview,
        *,
        timeout_seconds: float,
    ) -> PreparedExecutor:
        preview_payload = preview.model_dump(mode="json")
        sanitized = sanitize_data(preview_payload)
        if not isinstance(sanitized, dict) or sanitized != preview_payload:
            raise ExecutionGateError("patch preview is unsafe to display")
        digest = patch_preview_digest(preview)
        availability = self._registry.inspect_availability(self._root, self._policy)
        base_options = self._registry.build_options(
            self.run_id, digest, availability
        )
        if (
            len(base_options) != 2
            or {item.mode for item in base_options}
            != {ExecutionMode.DOCKER, ExecutionMode.LOCAL}
        ):
            raise ExecutionGateError("executor registry must provide exactly two options")
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            if self._closed:
                raise WorkflowCancelled("executor selection gate is closed")
            if self._pending is not None:
                raise ExecutionGateError("another executor selection is already pending")
            generation = self._generation + 1
            pending = PendingExecutionChoice(
                run_id=self.run_id,
                preview_digest=digest,
                preview=sanitized,
                options=[
                    option.model_copy(
                        update={
                            "option_digest": _generation_option_digest(
                                option.option_digest, generation
                            )
                        }
                    )
                    for option in base_options
                ],
            )
            self._generation = generation
            self._pending = pending
            self._pending_generation = generation
            self._prepared = None
            self._rejection = None
            self._condition.notify_all()
            while (
                self._prepared is None
                and self._rejection is None
                and not self._closed
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._clear_generation(generation)
                    self._condition.notify_all()
                    raise ExecutorSelectionRejected("executor selection timed out")
                self._condition.wait(remaining)
            if self._closed:
                self._clear_generation(generation)
                raise WorkflowCancelled("executor selection gate is closed")
            if self._rejection is not None:
                rejection = self._rejection
                self._clear_generation(generation)
                raise ExecutorSelectionRejected(rejection)
            prepared = self._prepared
            if prepared is None:
                self._clear_generation(generation)
                raise ExecutorSelectionRejected("executor selection did not complete")
            self._clear_generation(generation)
            self._condition.notify_all()
            return prepared

    def submit(self, decision: ExecutionDecision) -> None:
        with self._condition:
            if self._closed:
                raise ExecutionGateError("executor selection gate is closed")
            pending = self._pending
            generation = self._pending_generation
            if pending is None or generation is None:
                raise ExecutionGateError("no executor selection is pending")
            if (
                self._prepared is not None
                or self._rejection is not None
                or self._preparing_generation is not None
            ):
                raise ExecutionGateError(
                    "executor selection decision has already been submitted"
                )
            if decision.run_id != pending.run_id:
                raise ExecutionGateError("executor selection run ID mismatch")
            if decision.preview_digest != pending.preview_digest:
                raise ExecutionGateError("executor selection preview digest mismatch")
            selected = next(
                (
                    item
                    for item in pending.options
                    if item.option_digest == decision.option_digest
                ),
                None,
            )
            if selected is None:
                raise ExecutionGateError("executor selection option digest mismatch")
            if selected.mode is not decision.mode:
                raise ExecutionGateError("executor selection mode mismatch")
            if not selected.available:
                raise ExecutionGateError("selected executor is unavailable")
            if decision.decision is not Decision.APPROVED:
                self._rejection = "executor selection rejected"
                self._condition.notify_all()
                return
            self._preparing_generation = generation
        try:
            prepared = self._registry.prepare(
                self._root, decision.mode, self._policy
            )
        except ExecutorSelectionError as error:
            self._release_preparing_generation(generation)
            raise ExecutionGateError(str(error)) from error
        except Exception as error:
            self._release_preparing_generation(generation)
            raise ExecutionGateError("executor preparation failed") from error
        try:
            if prepared.mode is not decision.mode:
                raise ExecutionGateError("prepared executor mode mismatch")
            validate_executor_isolation(prepared.mode, prepared.isolation_level)
        except ValueError as error:
            self._release_preparing_generation(generation)
            raise ExecutionGateError(str(error)) from error
        except ExecutionGateError:
            self._release_preparing_generation(generation)
            raise
        with self._condition:
            if self._closed:
                if self._preparing_generation == generation:
                    self._preparing_generation = None
                raise ExecutionGateError("executor selection gate is closed")
            if generation != self._pending_generation:
                if self._preparing_generation == generation:
                    self._preparing_generation = None
                raise ExecutionGateError("executor selection generation changed")
            if self._preparing_generation != generation:
                raise ExecutionGateError("executor selection generation changed")
            self._preparing_generation = None
            self._prepared = prepared
            self._condition.notify_all()

    def wait(
        self,
        *,
        after_generation: int,
        timeout_seconds: float,
    ) -> tuple[int, PendingExecutionChoice | None]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            if self._closed:
                return self._generation, None
            while self._generation <= after_generation and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._generation, None
                self._condition.wait(remaining)
            if self._closed:
                return self._generation, None
            pending = (
                self._pending.model_copy(deep=True)
                if self._pending is not None
                else None
            )
            return self._generation, pending

    def close(self) -> None:
        with self._condition:
            self._closed = True
            generation = self._pending_generation
            if generation is not None:
                self._clear_generation(generation)
            self._condition.notify_all()

    def _clear_generation(self, generation: int) -> None:
        if self._pending_generation == generation:
            self._pending = None
            self._pending_generation = None
            if self._preparing_generation == generation:
                self._preparing_generation = None
            self._prepared = None
            self._rejection = None
            self._generation += 1
            self._condition.notify_all()

    def _release_preparing_generation(self, generation: int) -> None:
        with self._condition:
            if self._preparing_generation == generation:
                self._preparing_generation = None
                self._condition.notify_all()


def _generation_option_digest(option_digest: str, generation: int) -> str:
    canonical = json.dumps(
        {"generation": generation, "option_digest": option_digest},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
