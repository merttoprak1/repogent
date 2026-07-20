import json
from pathlib import Path

import pytest

from repogent.artifacts import ArtifactStore
from repogent.domain import EventKind, RunEvent
from repogent.events import MAX_EVENT_BYTES, JsonlEventStore


def _event(sequence: int, *, data: dict[str, object] | None = None) -> RunEvent:
    return RunEvent(
        run_id="run-1",
        sequence=sequence,
        kind=EventKind.STAGE,
        message="event",
        data=data or {},
    )


def _canonical_line(event: RunEvent) -> str:
    return json.dumps(
        event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ) + "\n"


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


def test_jsonl_event_store_reopens_from_last_persisted_sequence(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    artifact_store = ArtifactStore.create(tmp_path / "runs", target, "change", run_id="run-1")
    artifact_store.event_store().emit(_event(1))

    reopened = artifact_store.event_store()

    with pytest.raises(ValueError, match="sequence"):
        reopened.emit(_event(1))
    reopened.emit(_event(2))

    sequences = [json.loads(line)["sequence"] for line in reopened.path.read_text().splitlines()]
    assert sequences == [1, 2]


@pytest.mark.parametrize(
    ("contents", "match"),
    [
        ("not-json\n", "event log"),
        ('{"schema_version":"1","sequence":1}\n', "event log"),
    ],
)
def test_jsonl_event_store_rejects_corrupt_existing_log(
    tmp_path: Path, contents: str, match: str
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(contents)

    with pytest.raises(ValueError, match=match):
        JsonlEventStore(path)


def test_jsonl_event_store_rejects_non_monotonic_existing_log(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(_event(1).model_dump_json() + "\n" + _event(1).model_dump_json() + "\n")

    with pytest.raises(ValueError, match="monotonically"):
        JsonlEventStore(path)


def test_jsonl_event_store_accepts_event_at_byte_limit(tmp_path: Path) -> None:
    empty_event = _event(1, data={"payload": ""})
    payload_size = MAX_EVENT_BYTES - len(_canonical_line(empty_event).encode())
    event = _event(1, data={"payload": "x" * payload_size})

    assert len(_canonical_line(event).encode()) == MAX_EVENT_BYTES

    store = JsonlEventStore(tmp_path / "events.jsonl")
    store.emit(event)

    assert (tmp_path / "events.jsonl").read_bytes() == _canonical_line(event).encode()


def test_jsonl_event_store_rejects_event_over_byte_limit_before_creating_file(
    tmp_path: Path,
) -> None:
    empty_event = _event(1, data={"payload": ""})
    payload_size = MAX_EVENT_BYTES - len(_canonical_line(empty_event).encode()) + 1
    event = _event(1, data={"payload": "x" * payload_size})
    path = tmp_path / "events.jsonl"

    with pytest.raises(ValueError, match="maximum size"):
        JsonlEventStore(path).emit(event)

    assert not path.exists()
