import json
from pathlib import Path

import pytest

from repogent.domain import EventKind, RunEvent
from repogent.events import JsonlEventStore


def test_jsonl_event_store_appends_versioned_sanitized_events(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "events.jsonl", secrets=["sk-secret"])
    store.emit(
        RunEvent(
            run_id="run-1",
            sequence=1,
            kind=EventKind.WARNING,
            stage="preflight",
            message="credential sk-secret was not forwarded",
        )
    )
    payload = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
    assert payload["schema_version"] == "1"
    assert payload["sequence"] == 1
    assert "sk-secret" not in payload["message"]


def test_jsonl_event_store_rejects_non_monotonic_sequence(tmp_path: Path) -> None:
    store = JsonlEventStore(tmp_path / "events.jsonl")
    event = RunEvent(run_id="run-1", sequence=1, kind=EventKind.STAGE, message="start")
    store.emit(event)
    with pytest.raises(ValueError, match="sequence"):
        store.emit(event)
