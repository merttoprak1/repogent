from app import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_root() -> None:
    assert client.get("/").json() == {"message": "demo"}  # noqa: S101  # nosec B101
