from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.db import get_session
from app.main import app


class FakeSession:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def execute(self, _statement: object) -> None:
        if self.error:
            raise self.error


def test_healthz_ok(client: TestClient) -> None:
    app.dependency_overrides[get_session] = lambda: FakeSession()
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_reports_sqlalchemy_failure(client: TestClient) -> None:
    error = OperationalError("SELECT 1", {}, Exception("down"))
    app.dependency_overrides[get_session] = lambda: FakeSession(error)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_healthz_reports_connection_refused(client: TestClient) -> None:
    app.dependency_overrides[get_session] = lambda: FakeSession(ConnectionRefusedError())
    response = client.get("/healthz")
    assert response.status_code == 503
