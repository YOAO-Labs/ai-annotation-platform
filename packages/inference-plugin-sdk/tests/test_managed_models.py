from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import BaseModel

from aap_inference_plugin import DetectionConfig, create_detection_app
from test_detection_app import Model, TOKEN, image_url

VERSION = "7c5fba75-1432-458d-b874-111111111111"
IDENTITY = {"model_version_id": VERSION, "sha256": "a" * 64, "classes_digest": "b" * 64}
RUNTIME = {"qualification": {"device": "cpu"}, "revision": 1}


class Install(BaseModel):
    model_version_id: str


class Store:
    def receipt(self, version_id):
        assert version_id == VERSION
        return dict(IDENTITY)
    def resolve(self, version_id, sha256):
        assert (version_id, sha256) == (VERSION, "a" * 64)
        return Path("/controlled/checkpoint"), self.receipt(version_id)
    async def install(self, request):
        return self.receipt(request.model_version_id)


class Provider:
    def load(self, model_id, device, config):
        assert model_id == "trained:" + VERSION
        assert config["path"] == Path("/controlled/checkpoint")
        return Model()


def test_managed_reference_is_pinned_and_returned():
    config = DetectionConfig(token=TOKEN, device="cpu", catalog={"t": {}}, default_model="t",
                             managed_store=Store(), runtime_receipt=lambda receipt: {**receipt, "inference_runtime": RUNTIME},
                             install_request_model=Install,
                             managed_model_compatibility=[{"framework": "libreyolo", "format": "native", "runtime_device": "cpu"}])
    with TestClient(create_detection_app(Provider(), config), headers={"Authorization": "Bearer " + TOKEN}) as client:
        body = {"tasks": [{"id": "crop", "file_path": image_url()}], "context": {"model_id": "trained:" + VERSION,
                "frozen_model_ref": {**IDENTITY, "inference_runtime": RUNTIME}, "model_variants": {}}}
        response = client.post("/predict", json=body)
        assert response.status_code == 200
        meta = response.json()["results"][0]["meta"]
        assert meta == {"trained_model": IDENTITY, "inference_runtime": RUNTIME}
        del body["context"]["frozen_model_ref"]["inference_runtime"]
        assert client.post("/predict", json=body).status_code == 200
        body["context"]["frozen_model_ref"]["inference_runtime"] = {"qualification": {"device": "cuda"}, "revision": 1}
        assert client.post("/predict", json=body).status_code == 409
        body["context"]["frozen_model_ref"]["inference_runtime"] = RUNTIME
        body["context"]["model_variants"] = {"size": "t"}
        assert client.post("/predict", json=body).status_code == 422
