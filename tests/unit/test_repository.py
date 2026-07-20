import os
from pathlib import Path

from pytest import MonkeyPatch

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
