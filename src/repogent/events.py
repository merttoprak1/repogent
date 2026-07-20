from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from repogent.domain import RunEvent
from repogent.sanitization import sanitize_data

MAX_EVENT_BYTES = 65_536


class EventSink(Protocol):
    def emit(self, event: RunEvent) -> None:
        raise NotImplementedError


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
