from fastapi import FastAPI
from fastapi.testclient import TestClient

from aap_backend_runtime.plugin_service import install_plugin_service


def test_plugin_auth_rejects_missing_and_incorrect_tokens(tmp_path, monkeypatch):
    token = "a" * 64
    token_file = tmp_path / "token"
    token_file.write_text(token)
    control_token = "b" * 64
    control_file = tmp_path / "route-token"
    control_file.write_text(control_token)
    registry = "12345678-1234-4234-8234-123456789abc"
    monkeypatch.setenv("AAP_PLUGIN_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AAP_PLUGIN_DEVICE", "cpu")
    monkeypatch.setenv("AAP_PLUGIN_REGISTRY_ID", registry)
    monkeypatch.setenv("AAP_ROUTE_REGISTRY_ID", registry)
    monkeypatch.setenv("AAP_ROUTE_CONTROL_TOKEN_FILE", str(control_file))
    monkeypatch.delenv("AAP_ROUTE_ALLOW_LEGACY_EXECUTION", raising=False)
    monkeypatch.delenv("AAP_PLUGIN_GPU_RESOURCE_ID", raising=False)
    monkeypatch.delenv("AAP_PLUGIN_GPU_UUID", raising=False)
    app = FastAPI()
    install_plugin_service(app, lambda: None)

    @app.get("/setup")
    def setup():
        return {"service": "plugin"}

    with TestClient(app) as client:
        assert client.get("/setup").status_code == 403
        assert (
            client.get("/setup", headers={"Authorization": "Bearer wrong"}).status_code
            == 403
        )
        response = client.get("/setup", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json() == {"service": "plugin"}
        status = "/v1/aap-routing/status"
        assert (
            client.get(status, headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )
        assert (
            client.get(
                status, headers={"Authorization": f"Bearer {control_token}"}
            ).status_code
            == 503
        )
