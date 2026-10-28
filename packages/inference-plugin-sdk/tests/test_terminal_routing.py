"""Real app-level terminal admission behavior with a bound routing controller."""
import base64
from uuid import uuid4

from fastapi.testclient import TestClient
from aap_protocol_v2.routing import ROUTE_CONTROL_PATH, ROUTE_CONTROL_PROTOCOL

from aap_inference_plugin import DetectionConfig, create_detection_app
from test_detection_app import Provider, TOKEN


ROUTE_TOKEN = "route-control-secret-" + "x" * 32


def bind_route(client, registry):
    command = {"schema": ROUTE_CONTROL_PROTOCOL, "command_id": str(uuid4()),
               "registry_id": registry, "authority_epoch": str(uuid4()), "retired_epoch": None,
               "challenge": "a" * 64, "action": "drain", "signing_key": None}
    headers = {"Authorization": "Bearer " + ROUTE_TOKEN}
    response = client.post(ROUTE_CONTROL_PATH, headers=headers, json=command)
    assert response.status_code == 200
    assert response.json()["phase"] == "drained"
    command["action"] = "bind"
    command["signing_key"] = base64.urlsafe_b64encode(b"r" * 32).decode().rstrip("=")
    response = client.post(ROUTE_CONTROL_PATH, headers=headers, json=command)
    assert response.status_code == 200
    assert response.json()["phase"] == "bound"


def test_cpu_terminal_path_bypasses_only_routing_not_bearer(monkeypatch):
    monkeypatch.setenv("AAP_ROUTE_CONTROL_TOKEN", ROUTE_TOKEN)
    registry = str(uuid4())
    config = DetectionConfig(token=TOKEN, device="cpu", catalog={"t": {}}, default_model="t", expected_registry_id=registry)
    with TestClient(create_detection_app(Provider(), config)) as client:
        bind_route(client, registry)
        boot = client.get("/health").json()["residency"]["boot_id"]
        body = {"registry_id": registry, "boot_id": boot, "operation_id": "stop"}
        wrong = client.post("/lifecycle/terminal-drain", headers={"Authorization": "Bearer " + "z" * 32}, json=body)
        assert wrong.status_code in (401, 403)
        near = client.post("/lifecycle/terminal-drain-extra", headers={"Authorization": "Bearer " + TOKEN}, json=body)
        assert near.status_code in (401, 503)  # route admission rejected, not a 404 bypass
        stopped = client.post("/lifecycle/terminal-drain", headers={"Authorization": "Bearer " + TOKEN}, json=body)
        assert stopped.status_code == 200
        assert stopped.json()["unloaded"] is True
        assert stopped.json()["registry_id"] == registry


def test_gpu_terminal_path_requires_bearer_and_gpu_signature_with_routing_bound(monkeypatch):
    monkeypatch.setenv("AAP_ROUTE_CONTROL_TOKEN", ROUTE_TOKEN)
    registry = str(uuid4())
    config = DetectionConfig(token=TOKEN, device="cuda:0", physical_gpu_uuid="GPU-123",
                             catalog={"t": {}}, default_model="t", expected_registry_id=registry)
    with TestClient(create_detection_app(Provider(), config)) as client:
        bind_route(client, registry)
        path = "/lifecycle/terminal-drain-signed"
        body = {"generation": "1"}
        wrong = client.post(path, headers={"Authorization": "Bearer " + "z" * 32}, json=body)
        assert wrong.status_code in (401, 403)
        unsigned = client.post(path, headers={"Authorization": "Bearer " + TOKEN}, json=body)
        assert 400 <= unsigned.status_code < 500 and unsigned.status_code != 404
        near = client.post(path + "-extra", headers={"Authorization": "Bearer " + TOKEN}, json=body)
        assert near.status_code in (401, 503)
