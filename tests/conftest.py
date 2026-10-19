import pytest


@pytest.fixture(autouse=True)
def _test_client_host(monkeypatch):
    """Starlette's TestClient sends `Host: testserver`; the manager only answers to names it knows."""
    monkeypatch.setenv("VSM_ALLOWED_HOSTS", "testserver")
