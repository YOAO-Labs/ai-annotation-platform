import asyncio
import threading

import httpx
import pytest

from aap_inference_plugin import DetectionConfig, create_detection_app
from test_detection_app import Model, TOKEN, image_url


@pytest.mark.asyncio
async def test_cancelled_request_retains_model_until_native_predict_exits():
    entered = threading.Event()
    release = threading.Event()

    class BlockingModel(Model):
        def predict(self, image, parameters):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release prediction")
            return super().predict(image, parameters)

    model = BlockingModel()
    class Provider:
        def load(self, model_id, device, config):
            return model

    app = create_detection_app(Provider(), DetectionConfig(token=TOKEN, device="cpu", catalog={"t": {}}, default_model="t"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://plugin", headers={"Authorization": "Bearer " + TOKEN}) as client:
            request = asyncio.create_task(client.post("/predict", json={"tasks": [{"id": 1, "file_path": image_url()}], "context": {}}))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                request.cancel()
                assert (await client.post("/unload")).json()["unloaded"] == 0
                assert not model.closed
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await request
            assert (await client.post("/unload")).json()["unloaded"] == 1
            assert model.closed
