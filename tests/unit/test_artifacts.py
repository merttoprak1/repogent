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


def test_raw_text_redaction_preserves_database_url_delimiters() -> None:
    text = "dsn=postgresql://alice:secret@db.example/app,next=visible"

    sanitized = redact(text, [])

    assert sanitized == "dsn=[REDACTED],next=visible"


def test_explicit_secret_redaction_does_not_duplicate_placeholder_delimiters() -> None:
    assert redact("token=named-secret", ["named-secret"]) == "token=[REDACTED]"


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
    assert json.loads(persisted)["run_id"] == "run-1"
    assert "named-secret" not in persisted
    assert "ghp_" not in persisted


def test_json_artifact_recursively_redacts_secret_value_families(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    store = ArtifactStore.create(
        tmp_path / "runs",
        target,
        "change",
        run_id="run-1",
        secrets=["configured-secret"],
    )
    secrets = [
        "password=hunter2",
        "token=ghp_abcdefghijklmnopqrstuvwxyz123456",
        "api_key=sk-proj-abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "postgresql://alice:db-secret@db.example/app",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signatureABCDE",
        "configured-secret",
    ]

    artifact = store.write_text(
        "payload", json.dumps({"nested": [{"value": secret} for secret in secrets]}), suffix=".json"
    )

    payload = json.loads(artifact.read_text())
    persisted = json.dumps(payload)
    assert len(payload["nested"]) == len(secrets)
    for secret in (
        "hunter2",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "sk-proj-abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "postgresql://alice:db-secret@db.example/app",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signatureABCDE",
        "configured-secret",
    ):
        assert secret not in persisted


def test_json_artifact_redacts_sensitive_fields_and_quoted_assignments(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    store = ArtifactStore.create(tmp_path / "runs", target, "change", run_id="run-1")
    artifact = store.write_text(
        "payload",
        json.dumps(
            {
                "credentials": {
                    "password": "correct horse battery staple",
                    "token": "opaque-token-value",
                    "api-key": "opaque-api-key-value",
                },
                "note": 'password="another secret with spaces"',
                "embedded": '{"password": "embedded secret with spaces"}',
                "safe": "ordinary source remains visible",
            }
        ),
        suffix=".json",
    )

    persisted = artifact.read_text()
    payload = json.loads(persisted)
    assert payload["safe"] == "ordinary source remains visible"
    for secret in (
        "correct horse battery staple",
        "opaque-token-value",
        "opaque-api-key-value",
        "another secret with spaces",
        "embedded secret with spaces",
    ):
        assert secret not in persisted


def test_manifest_redaction_preserves_valid_json_structure(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    store = ArtifactStore.create(
        tmp_path / "runs", target, "change", run_id="run-1", secrets=["named-secret"]
    )
    manifest = RunManifest(
        run_id="run-1",
        request="password=hunter2",
    )

    store.update_manifest(manifest)

    payload = json.loads((store.root / "run.json").read_text())
    assert payload["run_id"] == "run-1"
    assert "hunter2" not in payload["request"]
