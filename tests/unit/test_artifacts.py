import json
from pathlib import Path

import pytest

from repogent.artifacts import ArtifactStore, ArtifactStoreError, redact
from repogent.domain import RunManifest, RunStage


def test_store_rejects_output_inside_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ArtifactStoreError, match="outside target"):
        ArtifactStore.create(target / ".repogent", target, "change")


@pytest.mark.parametrize("run_id", ["/" + "tmp/escape", "", ".", "..", "../escape", "runs/escape"])
def test_store_rejects_unsafe_run_ids(tmp_path: Path, run_id: str) -> None:
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ArtifactStoreError, match="run ID"):
        ArtifactStore.create(tmp_path / "runs", target, "change", run_id=run_id)


def test_model_write_is_versioned_and_manifest_is_atomic(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    store = ArtifactStore.create(tmp_path / "runs", target, "change", run_id="run-1")
    manifest = RunManifest(run_id="run-1", request="change", stage=RunStage.ANALYZED)
    first = store.write_model("requirements", manifest)
    second = store.write_model("requirements", manifest)
    store.update_manifest(manifest)
    store.write_final("report.md", "# Final report\n")
    assert first.name == "requirements-001.json"
    assert second.name == "requirements-002.json"
    assert json.loads((store.root / "run.json").read_text())["stage"] == "analyzed"
    assert (store.root / "report.md").read_text() == "# Final report\n"
    assert not list(store.root.glob("*.tmp"))


def test_redaction_removes_named_secrets_and_common_api_keys() -> None:
    text = "OPENAI_API_KEY=sk-secretvalue token=ghp_abcdefghijklmnopqrstuvwxyz123456"
    assert "sk-secretvalue" not in redact(text, ["sk-secretvalue"])
    assert "ghp_" not in redact(text, [])


@pytest.mark.parametrize("name", ["/" + "tmp/escape", "../escape"])
def test_text_write_rejects_unsafe_artifact_names(tmp_path: Path, name: str) -> None:
    target = tmp_path / "target"
    target.mkdir()
    store = ArtifactStore.create(tmp_path / "runs", target, "change", run_id="run-1")
    with pytest.raises(ArtifactStoreError, match="artifact name"):
        store.write_text(name, "content")


def test_text_write_rejects_unsafe_suffix(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    store = ArtifactStore.create(tmp_path / "runs", target, "change", run_id="run-1")
    with pytest.raises(ArtifactStoreError, match="suffix"):
        store.write_text("evidence", "content", suffix="/../../escape")


def test_manifest_write_redacts_explicit_and_pattern_secrets(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    store = ArtifactStore.create(
        tmp_path / "runs", target, "change", run_id="run-1", secrets=["named-secret"]
    )
    manifest = RunManifest(
        run_id="run-1",
        request="named-secret token=ghp_abcdefghijklmnopqrstuvwxyz123456",
    )
    store.update_manifest(manifest)
    persisted = (store.root / "run.json").read_text()
    assert "named-secret" not in persisted
    assert "ghp_" not in persisted
