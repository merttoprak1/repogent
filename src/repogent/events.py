from __future__ import annotations

import fcntl
import json
import math
import os
import re
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from repogent.domain import RunEvent, RunStage, RunStatus
from repogent.sanitization import redact_text, sanitize_data

MAX_EVENT_BYTES = 65_536
MAX_TIMELINE_COUNT = 1_000_000_000
MAX_TIMELINE_COST_CHARS = 32
_COUNT_TEXT = re.compile(r"[0-9]+$")
_KNOWN_STAGES = frozenset(stage.value for stage in RunStage)
_KNOWN_STATUSES = frozenset(status.value for status in RunStatus)


class EventSink(Protocol):
    def emit(self, event: RunEvent) -> None:
        raise NotImplementedError


class CompositeEventSink:
    """Fan an event to ordered sinks, preserving failures for the workflow."""

    def __init__(self, sinks: Sequence[EventSink]) -> None:
        self.sinks = tuple(sinks)

    def emit(self, event: RunEvent) -> None:
        for sink in self.sinks:
            sink.emit(event)


class ConsoleEventSink:
    """Render a small, sanitized progress line without command output."""

    def __init__(
        self, write: Callable[[str], object], secrets: Sequence[str] = ()
    ) -> None:
        self.write = write
        self.secrets = tuple(secrets)

    def emit(self, event: RunEvent) -> None:
        message = " ".join(redact_text(event.message, self.secrets).split())
        suffix = self._suffix(event)
        self.write(f"[{event.kind.value}] {message}{suffix}")

    @staticmethod
    def _suffix(event: RunEvent) -> str:
        kind = event.kind.value
        if kind == "validation":
            return ConsoleEventSink._validation_suffix(event)
        if kind == "stage":
            return _bounded_label_suffix(event.data.get("stage"), _KNOWN_STAGES)
        if kind == "terminal":
            return _bounded_label_suffix(event.data.get("status"), _KNOWN_STATUSES)
        return ""

    @staticmethod
    def _validation_suffix(event: RunEvent) -> str:
        passed = _nonnegative_int(event.data.get("passed"))
        failed = _nonnegative_int(event.data.get("failed"))
        skipped = _nonnegative_int(event.data.get("skipped"))
        values = [f"{passed} passed", f"{failed} failed", f"{skipped} skipped"]
        cost = _cost(event.data.get("cost_usd"))
        if cost is not None:
            values.append(f"${cost}")
        return f" ({', '.join(values)})"


def _bounded_label_suffix(value: object, allowed: frozenset[str]) -> str:
    """Render a closed-vocabulary label (a workflow stage or run status) safely.

    Only values that exactly match a known enum member are surfaced; anything
    else (including arbitrarily large or malformed input) is dropped rather
    than echoed, so this can never become a vector for raw command output or
    unbounded console text.
    """
    return f" ({value})" if isinstance(value, str) and value in allowed else ""


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 0 <= value <= MAX_TIMELINE_COUNT else 0
    if isinstance(value, Decimal):
        if (
            value.is_finite()
            and 0 <= value <= MAX_TIMELINE_COUNT
            and value == value.to_integral_value()
        ):
            return int(value)
        return 0
    if isinstance(value, float):
        if math.isfinite(value) and 0 <= value <= MAX_TIMELINE_COUNT and value.is_integer():
            return int(value)
        return 0
    if isinstance(value, str) and len(value) <= len(str(MAX_TIMELINE_COUNT)):
        return int(value) if _COUNT_TEXT.fullmatch(value) else 0
    return 0


def _cost(value: object) -> str | None:
    if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
        return None
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not cost.is_finite() or cost < 0:
        return None
    rendered = str(cost)
    return rendered if len(rendered) <= MAX_TIMELINE_COST_CHARS else None


class JsonlEventStore:
    def __init__(self, path: Path, secrets: list[str] | None = None) -> None:
        self.path = path
        self.secrets = secrets or []
        self._last_sequence = self._load_last_sequence()

    def emit(self, event: RunEvent) -> None:
        line = self._serialize_event(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_descriptor = os.open(self._lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            try:
                last_sequence = self._load_last_sequence()
                if event.sequence <= last_sequence:
                    raise ValueError("event sequence must increase monotonically")
                self._append_line(line)
                self._last_sequence = event.sequence
            finally:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)

    @property
    def _lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    def _load_last_sequence(self) -> int:
        if not self.path.exists():
            return 0

        last_sequence = 0
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    event = RunEvent.model_validate(json.loads(line))
                    if event.sequence <= last_sequence:
                        raise ValueError(
                            "event sequence must increase monotonically "
                            f"(line {line_number})"
                        )
                    last_sequence = event.sequence
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
            raise ValueError("invalid event log") from error
        return last_sequence

    def _serialize_event(self, event: RunEvent) -> str:
        payload = sanitize_data(event.model_dump(mode="json"), self.secrets)
        line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        if len(line.encode()) > MAX_EVENT_BYTES:
            raise ValueError(f"event exceeds maximum size of {MAX_EVENT_BYTES} bytes")
        return line

    def _append_line(self, line: str) -> None:
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8", closefd=False) as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
