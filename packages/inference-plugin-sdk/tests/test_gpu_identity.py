from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from aap_inference_plugin import DetectionConfig, create_detection_app
from aap_inference_plugin.qualification import _sign
from aap_protocol_v2.lifecycle import AdmissionScope
from test_detection_app import TOKEN, image_url
from test_managed_models import IDENTITY, Install, Provider, Store, VERSION


def test_absent_historical_runtime_is_not_upgraded_to_cuda():
    cuda_runtime = {"revision": 1, "qualification": {"device": "cuda"}}
    registry, resource = str(uuid4()), "local/GPU-123"
    key = Ed25519PrivateKey.generate()
    config = DetectionConfig(
        token=TOKEN,
        device="cuda:0",
        physical_gpu_uuid="GPU-123",
        catalog={"t": {}},
        default_model="t",
        plugin_identity={"plugin_id": "libreyolo", "version": "1"},
        expected_registry_id=registry,
        expected_gpu_resource_id=resource,
        verify_keyring={"local": key.public_key()},
        evictable_verified=True,
        runtime_info=lambda: {
            "device": "cuda",
            "precision": None,
            "driver_version": "595.1",
            "total_memory_mb": 24000,
        },
        managed_store=Store(),
        runtime_receipt=lambda receipt: {**receipt, "inference_runtime": cuda_runtime},
        install_request_model=Install,
        managed_model_compatibility=[
            {"framework": "libreyolo", "format": "native", "runtime_device": "cuda"}
        ],
    )
    with TestClient(
        create_detection_app(Provider(), config),
        headers={"Authorization": "Bearer " + TOKEN},
    ) as client:
        boot = client.get("/health").json()["residency"]["boot_id"]
        mode = _sign(key, boot, registry, resource, AdmissionScope.MODE)
        assert (
            client.post(
                "/lifecycle/mode",
                headers={"X-AAP-GPU-Admission-Token": mode},
                json={"gate": "enforce", "control_epoch": "2"},
            ).status_code
            == 200
        )
        admission = _sign(
            key, boot, registry, resource, AdmissionScope.PREDICT, generation="1"
        )
        body = {
            "tasks": [{"id": "crop", "file_path": image_url()}],
            "context": {
                "model_id": "trained:" + VERSION,
                "frozen_model_ref": IDENTITY,
                "model_variants": {},
            },
        }
        response = client.post(
            "/predict",
            json=body,
            headers={
                "X-AAP-GPU-Generation": "1",
                "X-AAP-GPU-Admission-Token": admission,
            },
        )
        assert response.status_code == 409
        assert client.get("/health").json()["residency"]["gpu_loaded"] is False
