from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from repogent.domain import RunEvent
from repogent.sanitization import sanitize_data


class EventSink(Protocol):
    def emit(self, event: RunEvent) -> None:
        raise NotImplementedError


class JsonlEventStore:
    def __init__(self, path: Path, secrets: list[str] | None = None) -> None:
        self.path = path
        self.secrets = secrets or []
        self._last_sequence = 0

    def emit(self, event: RunEvent) -> None:
        if event.sequence <= self._last_sequence:
            raise ValueError("event sequence must increase monotonically")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = sanitize_data(event.model_dump(mode="json"), self.secrets)
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "a", encoding="utf-8", closefd=False) as handle:
                handle.write(json.dumps(payload) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        self._last_sequence = event.sequence
