import asyncio
import threading
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from aap_inference_plugin import DetectionConfig, create_detection_app
from test_detection_app import Model, Provider, TOKEN, image_url


def test_cpu_terminal_drain_auth_identity_and_irreversibility():
    registry = str(uuid4())
    provider = Provider()
    config = DetectionConfig(token=TOKEN, device="cpu", catalog={"t": {}}, default_model="t", expected_registry_id=registry)
    with TestClient(create_detection_app(provider, config)) as client:
        boot = client.get("/health").json()["residency"]["boot_id"]
        body = {"registry_id": registry, "boot_id": boot, "operation_id": "stop-1"}
        assert client.post("/lifecycle/terminal-drain", json=body).status_code in (401, 403)
        client.headers["Authorization"] = "Bearer " + TOKEN
        assert client.get("/setup").json()["terminal_lifecycle"] == {"terminal_drain_endpoint": "/lifecycle/terminal-drain"}
        assert client.get("/setup").json()["registry_id"] == registry
        assert client.get("/setup").json()["gpu_resource_id"] is None
        assert client.post("/warmup", json={"task": "detect"}).status_code == 200
        wrong = {**body, "registry_id": str(uuid4())}
        assert client.post("/lifecycle/terminal-drain", json=wrong).status_code >= 400
        response = client.post("/lifecycle/terminal-drain", json=body)
        assert response.status_code == 200
        assert response.json() == {**body, "control_epoch": None, "generation": None, "pool_ids": ["models"], "unloaded": True}
        assert provider.models[0].closed
        assert client.post("/lifecycle/terminal-drain", json=body).json() == response.json()
        assert client.post("/warmup", json={"task": "detect"}).status_code >= 400
        assert client.post("/predict", json={"tasks": [{"id": 1, "file_path": image_url()}], "context": {}}).status_code >= 400


@pytest.mark.asyncio
async def test_cpu_terminal_drain_waits_for_native_borrower_and_fences_new_work():
    entered, release = threading.Event(), threading.Event()
    class BlockingModel(Model):
        def predict(self, image, parameters):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release prediction")
            return super().predict(image, parameters)
    model = BlockingModel()
    class BlockingProvider:
        def load(self, model_id, device, config):
            return model
    registry = str(uuid4())
    config = DetectionConfig(token=TOKEN, device="cpu", catalog={"t": {}}, default_model="t", expected_registry_id=registry)
    app = create_detection_app(BlockingProvider(), config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://plugin", headers={"Authorization": "Bearer " + TOKEN}) as client:
            boot = (await client.get("/health")).json()["residency"]["boot_id"]
            prediction = asyncio.create_task(client.post("/predict", json={"tasks": [{"id": 1, "file_path": image_url()}], "context": {}}))
            stop = None
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                stop = asyncio.create_task(client.post("/lifecycle/terminal-drain", json={"registry_id": registry, "boot_id": boot, "operation_id": "stop-2"}))
                for _ in range(100):
                    if (await client.post("/warmup", json={"task": "detect"})).status_code >= 400:
                        break
                    await asyncio.sleep(.01)
                else:
                    pytest.fail("terminal stop never fenced new work")
                assert not stop.done()
                assert not model.closed
            finally:
                release.set()
                await prediction
            assert stop is not None and (await stop).json()["unloaded"] is True
            assert model.closed


def test_cuda_cannot_use_bearer_only_cpu_terminal_drain():
    config = DetectionConfig(token=TOKEN, device="cuda:0", physical_gpu_uuid="GPU-123", catalog={"t": {}}, default_model="t", expected_registry_id=str(uuid4()))
    with TestClient(create_detection_app(Provider(), config), headers={"Authorization": "Bearer " + TOKEN}) as client:
        boot = client.get("/health").json()["residency"]["boot_id"]
        response = client.post("/lifecycle/terminal-drain", json={"registry_id": config.expected_registry_id, "boot_id": boot, "operation_id": "stop"})
        assert response.status_code == 404
        assert client.post("/lifecycle/terminal-drain-signed", json={"generation": "1"}).status_code >= 400

def test_unqualified_bound_cuda_advertises_only_signed_terminal_control():
    registry = str(uuid4())
    config = DetectionConfig(token=TOKEN, device="cuda:0", physical_gpu_uuid="GPU-123",
                             catalog={"t": {}}, default_model="t", expected_registry_id=registry,
                             expected_gpu_resource_id="local/GPU-123", evictable_verified=False)
    with TestClient(create_detection_app(Provider(), config), headers={"Authorization": "Bearer " + TOKEN}) as client:
        setup = client.get("/setup").json()
        assert setup["terminal_lifecycle"] == {"terminal_drain_signed_endpoint": "/lifecycle/terminal-drain-signed"}
        assert "managed_lifecycle" not in setup
        assert client.post("/lifecycle/terminal-drain-signed", json={"generation": "1"}).status_code >= 400



def test_cuda_terminal_drain_requires_and_consumes_signed_gpu_authority():
    from aap_protocol_v2.lifecycle import AdmissionScope
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from aap_inference_plugin.qualification import _sign

    registry, resource = str(uuid4()), "local/GPU-123"
    key = Ed25519PrivateKey.generate()
    class GpuProvider:
        def __init__(self):
            self.models = []
        def load(self, model_id, device, config):
            assert model_id == "t" and device == "cuda:0"
            model = Model()
            self.models.append(model)
            return model
    provider = GpuProvider()
    config = DetectionConfig(token=TOKEN, device="cuda:0", physical_gpu_uuid="GPU-123",
                             catalog={"t": {}}, default_model="t", expected_registry_id=registry,
                             expected_gpu_resource_id=resource, verify_keyring={"local": key.public_key()},
                             evictable_verified=True)
    with TestClient(create_detection_app(provider, config), headers={"Authorization": "Bearer " + TOKEN}) as client:
        boot = client.get("/health").json()["residency"]["boot_id"]
        assert client.get("/setup").json()["terminal_lifecycle"] == {
            "terminal_drain_signed_endpoint": "/lifecycle/terminal-drain-signed"
        }
        mode = _sign(key, boot, registry, resource, AdmissionScope.MODE)
        assert client.post("/lifecycle/mode", headers={"X-AAP-GPU-Admission-Token": mode},
                           json={"gate": "enforce", "control_epoch": "2"}).status_code == 200
        warm = _sign(key, boot, registry, resource, AdmissionScope.WARMUP, generation="1")
        assert client.post("/warmup", headers={"X-AAP-GPU-Generation": "1", "X-AAP-GPU-Admission-Token": warm},
                           json={"task": "detect"}).status_code == 200
        unsigned = client.post("/lifecycle/terminal-drain-signed", json={"generation": "2"})
        assert unsigned.status_code >= 400
        drain = _sign(key, boot, registry, resource, AdmissionScope.DRAIN, generation="2")
        response = client.post("/lifecycle/terminal-drain-signed",
                               headers={"X-AAP-GPU-Generation": "2", "X-AAP-GPU-Admission-Token": drain},
                               json={"generation": "2"})
        assert response.status_code == 200
        assert response.json()["registry_id"] == registry
        assert response.json()["gpu_resource_id"] == resource
        assert response.json()["unloaded"] is True
        assert provider.models[0].closed
        later = _sign(key, boot, registry, resource, AdmissionScope.WARMUP, generation="3")
        assert client.post("/warmup", headers={"X-AAP-GPU-Generation": "3", "X-AAP-GPU-Admission-Token": later},
                           json={"task": "detect"}).status_code >= 400
