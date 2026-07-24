from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_root() -> None:
    assert client.get("/").json() == {"message": "demo"}  # nosec B101
