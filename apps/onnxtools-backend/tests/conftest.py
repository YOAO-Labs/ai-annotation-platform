"""Explicit authentication for backend business tests."""

# Business/lifecycle tests use an explicit test credential; the shared runtime
# auth suite separately exercises disk bootstrap, denial, and manual precedence.
import pytest


@pytest.fixture(autouse=True)
def authenticated_backend_clients(monkeypatch):
    import httpx
    from starlette.testclient import TestClient
    from aap_backend_runtime import backend_auth

    token = "backend-source-test-credential-" + "x" * 40
    monkeypatch.setattr(backend_auth, "backend_credential", lambda _provider: token)
    original_sync = TestClient.__init__
    original_async = httpx.AsyncClient.__init__

    def sync_client(self, *args, **kwargs):
        kwargs["headers"] = {
            "Authorization": f"Bearer {token}",
            **(kwargs.get("headers") or {}),
        }
        original_sync(self, *args, **kwargs)

    def async_client(self, *args, **kwargs):
        if isinstance(kwargs.get("transport"), httpx.ASGITransport):
            kwargs["headers"] = {
                "Authorization": f"Bearer {token}",
                **(kwargs.get("headers") or {}),
            }
        original_async(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", sync_client)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", async_client)
