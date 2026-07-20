from pathlib import Path

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


def test_lexical_retrieval_ranks_matching_route_first(tmp_path: Path) -> None:
    (tmp_path / "auth.py").write_text("def login_rate_limit():\n    return 5\n")
    (tmp_path / "billing.py").write_text("def create_invoice():\n    return 1\n")
    inventory = RepositoryInspector().inspect(tmp_path)
    snippets = LexicalRetriever().retrieve(inventory, "add rate limiting to login", limit=1)
    assert snippets[0].path == "auth.py"
    assert "login" in snippets[0].reason
