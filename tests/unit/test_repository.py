import os
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from repogent import repository
from repogent.repository import LexicalRetriever, RepositoryInspector


def test_inspector_extracts_fastapi_route_and_symbols(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/health')\n"
        "def health() -> dict[str, str]:\n"
        "    return {'status': 'ok'}\n"
    )
    inventory = RepositoryInspector().inspect(tmp_path)
    record = inventory.files[0]
    assert record.path == "app.py"
    assert "health" in record.symbols
    assert "GET /health" in record.routes


def test_inspector_skips_ignored_large_and_symlinked_files(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret").write_text("secret")
    (tmp_path / "large.py").write_bytes(b"x" * 1_000_001)
    outside = tmp_path.parent / "outside.py"
    outside.write_text("password = 'secret'")
    (tmp_path / "escape.py").symlink_to(outside)
    inventory = RepositoryInspector(max_file_bytes=1_000_000).inspect(tmp_path)
    assert inventory.files == []
    assert sorted(inventory.skipped) == [".git", "escape.py", "large.py"]


def test_inspector_skips_linked_worktree_git_file(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /private/metadata")
    inventory = RepositoryInspector().inspect(tmp_path)
    assert inventory.files == []
    assert inventory.skipped == [".git"]


def test_inspector_excludes_sensitive_files_and_credential_directories(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("setting = 'safe'\n")
    (tmp_path / ".env.production").write_text("OPENAI_API_KEY=sk-proj-secretvalue\n")
    (tmp_path / ".envrc").write_text("export OPENAI_API_KEY=secret\n")
    (tmp_path / ".git-credentials").write_text("https://user:secret@example.com\n")
    (tmp_path / "service-account.json").write_text('{"private_key": "secret"}')
    (tmp_path / "deploy.pem").write_text("-----BEGIN PRIVATE KEY-----\nsecret\n")
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "credentials").write_text("aws_secret_access_key=secret\n")

    inventory = RepositoryInspector().inspect(tmp_path)

    assert [record.path for record in inventory.files] == ["app.py"]
    assert inventory.skipped == [
        ".aws",
        ".env.production",
        ".envrc",
        ".git-credentials",
        "deploy.pem",
        "service-account.json",
    ]


def test_inspector_keeps_non_secret_environment_templates_and_public_keys(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.example").write_text("API_KEY=replace-me\n")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 public-material\n")

    inventory = RepositoryInspector().inspect(tmp_path)

    assert [record.path for record in inventory.files] == [
        ".env.example",
        "id_ed25519.pub",
    ]


def test_inspector_keeps_public_pem_and_service_account_schema(tmp_path: Path) -> None:
    (tmp_path / "certificate.pem").write_text(
        "-----BEGIN CERTIFICATE-----\npublic-certificate\n-----END CERTIFICATE-----\n"
    )
    (tmp_path / "service_account_schema.json").write_text('{"type": "object"}\n')

    inventory = RepositoryInspector().inspect(tmp_path)

    assert [record.path for record in inventory.files] == [
        "certificate.pem",
        "service_account_schema.json",
    ]


@pytest.mark.parametrize(
    ("limits", "files", "reason"),
    [
        ({"max_files": 1}, {"a.py": "a", "b.py": "b"}, "file count"),
        ({"max_total_bytes": 1}, {"a.py": "ab"}, "aggregate bytes"),
        ({"max_directory_entries": 1}, {"a.py": "a", "b.py": "b"}, "directory entries"),
    ],
)
def test_inspector_fails_closed_when_aggregate_limit_is_exceeded(
    tmp_path: Path,
    limits: dict[str, int],
    files: dict[str, str],
    reason: str,
) -> None:
    for name, content in files.items():
        (tmp_path / name).write_text(content)

    with pytest.raises(repository.RepositoryLimitError, match=reason):
        RepositoryInspector(**limits).inspect(tmp_path)


def test_inspector_fails_closed_when_traversal_depth_is_exceeded(tmp_path: Path) -> None:
    nested = tmp_path / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "app.py").write_text("value = 1\n")

    with pytest.raises(repository.RepositoryLimitError, match="depth"):
        RepositoryInspector(max_depth=1).inspect(tmp_path)


def test_inspector_honors_external_deadline(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("value = 1\n")
    monkeypatch.setattr("repogent.repository.time.monotonic", lambda: 10.0)

    with pytest.raises(repository.RepositoryLimitError, match="deadline"):
        RepositoryInspector().inspect(tmp_path, deadline=9.0)


def test_inspector_does_not_read_file_replaced_by_symlink_during_open(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate.py"
    candidate.write_text("safe = True")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("password = 'secret'")
    original_open = os.open

    def swap_before_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        if path == "candidate.py":
            candidate.unlink()
            candidate.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    inventory = RepositoryInspector().inspect(tmp_path)
    assert inventory.files == []
    assert inventory.skipped == ["candidate.py"]


def test_lexical_retrieval_ranks_matching_route_first(tmp_path: Path) -> None:
    (tmp_path / "auth.py").write_text("def login_rate_limit():\n    return 5\n")
    (tmp_path / "billing.py").write_text("def create_invoice():\n    return 1\n")
    inventory = RepositoryInspector().inspect(tmp_path)
    snippets = LexicalRetriever().retrieve(inventory, "add rate limiting to login", limit=1)
    assert snippets[0].path == "auth.py"
    assert "login" in snippets[0].reason
