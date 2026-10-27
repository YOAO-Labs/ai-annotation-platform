"""Explicit authentication for backend business tests.

Keep the locked framework modules real across collection and test execution.
Replace model builders or loader attributes within scoped fixtures instead of
installing global framework stubs that hide native runtime identity.
"""

# Business/lifecycle tests use an explicit test credential; the shared runtime
# auth suite separately exercises disk bootstrap, denial, and manual precedence.
import pytest


@pytest.fixture(scope="module", autouse=True)
def authenticated_backend_clients():
    with pytest.MonkeyPatch.context() as monkeypatch:
        _authenticate_backend_clients(monkeypatch)
        yield


def _authenticate_backend_clients(monkeypatch):
    import httpx
    from starlette.testclient import TestClient

    token = "backend-source-test-credential-" + "x" * 40
    monkeypatch.delenv("YOLO_MANAGED_MODELS_TOKEN", raising=False)
    monkeypatch.setenv("AAP_BACKEND_AUTH_TOKEN", token)
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
