from fastapi.testclient import TestClient

from aap_inference_plugin import DetectionConfig, create_detection_app
from test_detection_app import TOKEN, image_url
from test_managed_models import IDENTITY, Install, Provider, Store, VERSION


def test_absent_historical_runtime_is_not_upgraded_to_cuda():
    cuda_runtime = {"revision": 1, "qualification": {"device": "cuda"}}
    config = DetectionConfig(token=TOKEN, device="cuda:0", physical_gpu_uuid="GPU-123", catalog={"t": {}}, default_model="t",
                             plugin_identity={"plugin_id": "libreyolo", "version": "1"},
                             runtime_info=lambda: {"device": "cuda", "precision": None, "driver_version": "595.1", "total_memory_mb": 24000},
                             managed_store=Store(), runtime_receipt=lambda receipt: {**receipt, "inference_runtime": cuda_runtime},
                             install_request_model=Install,
                             managed_model_compatibility=[{"framework": "libreyolo", "format": "native", "runtime_device": "cuda"}])
    with TestClient(create_detection_app(Provider(), config), headers={"Authorization": "Bearer " + TOKEN}) as client:
        setup = client.get("/setup").json()
        assert setup["physical_gpu_uuid"] == "GPU-123"
        assert setup["plugin_identity"] == {"plugin_id": "libreyolo", "version": "1"}
        assert setup["runtime_info"]["precision"] is None
        assert client.get("/health").json()["gpu_info"]["physical_gpu_uuid"] == "GPU-123"
        assert client.get("/health").json()["gpu_info"]["driver_version"] == "595.1"
        assert client.get("/health").json()["gpu_info"]["total_memory_mb"] == 24000
        body = {"tasks": [{"id": "crop", "file_path": image_url()}],
                "context": {"model_id": "trained:" + VERSION, "frozen_model_ref": IDENTITY, "model_variants": {}}}
        assert client.post("/predict", json=body).status_code == 409
