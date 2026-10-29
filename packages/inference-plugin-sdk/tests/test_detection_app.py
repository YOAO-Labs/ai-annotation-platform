import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from aap_inference_plugin import Detection, DetectionConfig, create_detection_app


TOKEN = "a" * 32


class Model:
    classes = ["cat", "dog"]

    def __init__(self):
        self.closed = False

    def predict(self, image, parameters):
        detections = [Detection(1, 2, 5, 8, 0, 0.9), Detection(5, 0, 10, 4, 1, 0.4)]
        selected = parameters.get("classes")
        return [
            item
            for item in detections
            if selected is None or item.class_index in selected
        ]

    def close(self):
        self.closed = True


class Provider:
    def __init__(self):
        self.models = []

    def load(self, model_id, device, config):
        assert model_id == "t"
        assert device == "cpu"
        model = Model()
        self.models.append(model)
        return model


def image_url():
    output = io.BytesIO()
    Image.new("RGB", (10, 10)).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def test_detection_protocol_and_cleanup():
    provider = Provider()
    app = create_detection_app(
        provider,
        DetectionConfig(
            token=TOKEN, device="cpu", catalog={"t": {}}, default_model="t"
        ),
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/setup").status_code in (401, 403)
        client.headers["Authorization"] = "Bearer " + TOKEN
        setup = client.get("/setup").json()
        assert setup["models"][0]["supported_inputs"] == ["full_image", "crop"]
        response = client.post(
            "/predict",
            json={
                "tasks": [{"id": 7, "file_path": image_url()}],
                "context": {"task": "detect"},
            },
        )
        assert response.status_code == 200
        result = response.json()["results"][0]
        assert result["task"] == 7
        assert [r["native_class_index"] for r in result["result"]] == [0, 1]
        assert result["result"][0]["value"]["x"] == 10
        assert result["result"][0]["value"]["width"] == 40
        assert (
            client.post(
                "/predict",
                json={
                    "tasks": [{"id": 7, "file_path": image_url()}],
                    "context": {"params": {"conf": 2}},
                },
            ).status_code
            == 422
        )
        assert client.post("/unload").json()["unloaded"] == 1
        assert provider.models[0].closed


def test_class_filter_applies_to_emitted_detections():
    app = create_detection_app(
        Provider(),
        DetectionConfig(
            token=TOKEN, device="cpu", catalog={"t": {}}, default_model="t"
        ),
    )
    with TestClient(app, headers={"Authorization": "Bearer " + TOKEN}) as client:
        body = {
            "tasks": [{"id": 7, "file_path": image_url()}],
            "context": {"classes": [1]},
        }
        response = client.post("/predict", json=body)
        assert response.status_code == 200
        assert [
            item["native_class_index"]
            for item in response.json()["results"][0]["result"]
        ] == [1]
        for invalid in ([True], ["1"], [-1], [2], [], None, "1"):
            body["context"]["classes"] = invalid
            assert client.post("/predict", json=body).status_code == 422
        body["context"] = {"params": {"classes": [1]}}
        assert client.post("/predict", json=body).status_code == 422


def test_reject_local_file_and_unapproved_host():
    from aap_inference_plugin.image import fetch_image

    with pytest.raises(ValueError):
        fetch_image("/etc/passwd", [])
    with pytest.raises(ValueError):
        fetch_image("http://127.0.0.1/internal", [])


def test_reject_invalid_detection_instead_of_returning_empty_success():
    from aap_inference_plugin.conversion import rectangles

    with pytest.raises(ValueError):
        rectangles([Detection(1, 0, 11, 2, 0, 0.5)], ["cat"], 10, 10)
    assert rectangles([], ["cat"], 10, 10) == []


def test_boxes_touching_image_edges_stay_inside_percentage_bounds():
    from aap_inference_plugin.conversion import rectangles

    value = rectangles(
        [Detection(669.6178588867188, 669.6178588867188, 810, 810, 0, 0.9)],
        ["person"],
        810,
        810,
    )[0]["value"]
    assert value["width"] <= 100 - value["x"]
    assert value["height"] <= 100 - value["y"]
    assert value["x"] + value["width"] == 100
    assert value["y"] + value["height"] == 100
