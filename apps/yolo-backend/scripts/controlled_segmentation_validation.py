"""Offline inputs and exact managed-model probes for lifecycle qualification."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from aap_backend_runtime import artifact_evidence
from aap_protocol_v2.mask_interaction import (
    CocoRlePayload,
    MAX_MASK_RESPONSE_BYTES,
    canonical_rle_bytes,
)
from managed_models import InstallRequest, ManagedModelRef, digest, file_digest
from managed_segmentation import validate_budget
from training_contract.model_return import segmentation_identity, ultralytics_identity
from training_contract.models import model_spec


INPUT_ENV = (
    "VALIDATION_CONTROLLED_SEGMENT_WEIGHTS",
    "VALIDATION_CONTROLLED_SEGMENT_FIXTURE",
    "VALIDATION_CONTROLLED_SEGMENT_IMAGE",
)
MODEL_INPUT_ENV = (
    "VALIDATION_CONTROLLED_MODEL_WEIGHTS",
    "VALIDATION_CONTROLLED_MODEL_FIXTURE",
    "VALIDATION_CONTROLLED_MODEL_IMAGE",
)
CHECKS = (
    "controlled_segment_install",
    "controlled_segment_reference_mismatch_rejected",
    "controlled_segment_receipt_mismatch_rejected",
    "controlled_segment_native_rle",
    "controlled_segment_cold_reload",
)
FIXTURE_FIELDS = {
    "schema",
    "frozen_model_ref",
    "classes",
    "confidence",
    "max_det",
    "min_instances",
}


def _fixture_task(fixture):
    return fixture["task"] if fixture["schema"] == 3 else "segment"


def _fixture_identity(fixture):
    base_model = fixture.get("base_model", "yolo11n-seg")
    version = fixture.get("framework_version", "8.4.177")
    task = _fixture_task(fixture)
    if not isinstance(task, str) or task not in {"detect", "segment"}:
        raise ValueError("Controlled fixture requires detect or segment task")
    if task == "segment":
        return segmentation_identity(base_model, framework_version=version)
    if model_spec("ultralytics", base_model).task != task:
        raise ValueError("Controlled fixture model and task differ")
    identity = ultralytics_identity(base_model, framework_version=version)
    if identity is None:
        raise ValueError(
            "Controlled detection requires an explicit native qualification"
        )
    return identity


def _read_fixture(path: Path) -> dict:
    if not 0 < path.stat().st_size <= 64 * 1024:
        raise ValueError("Controlled fixture JSON exceeds its byte budget")
    value = json.loads(path.read_bytes())
    if (
        not isinstance(value, dict)
        or type(value.get("schema")) is not int
        or value["schema"] not in {1, 2, 3}
    ):
        raise ValueError("Unsupported controlled fixture schema")
    fields = FIXTURE_FIELDS | (
        {"base_model", "framework_version"} if value["schema"] >= 2 else set()
    )
    if value["schema"] == 3:
        fields.add("task")
    if set(value) != fields:
        raise ValueError("Controlled fixture fields differ from the contract")
    _fixture_identity(value)
    ref = ManagedModelRef.model_validate(value["frozen_model_ref"])
    if ref.model_dump(mode="json", exclude_none=True) != value["frozen_model_ref"]:
        raise ValueError("Controlled fixture requires canonical model reference")
    classes = value["classes"]
    if (
        not isinstance(classes, list)
        or not 0 < len(classes) <= 1000
        or any(
            not isinstance(name, str) or not 0 < len(name) <= 120 for name in classes
        )
        or len(set(classes)) != len(classes)
        or digest(classes) != ref.classes_digest
    ):
        raise ValueError("Controlled fixture class identity differs")
    confidence = value["confidence"]
    if (
        type(confidence) not in {float, int}
        or not math.isfinite(confidence)
        or not 0 < confidence <= 1
    ):
        raise ValueError("Controlled fixture confidence must be finite and positive")
    if (
        type(value["max_det"]) is not int
        or not 1 <= value["max_det"] <= 300
        or type(value["min_instances"]) is not int
        or not 1 <= value["min_instances"] <= value["max_det"]
    ):
        raise ValueError("Controlled fixture requires at least one native instance")
    return value


@contextmanager
def _weight_server(weights: Path):
    """Serve exactly the private checkpoint to the real HTTP installer."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - standard library HTTP handler
            if self.path != "/weights.pt":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(weights.stat().st_size))
            self.end_headers()
            with weights.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=1024 * 1024)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/weights.pt"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Controlled checkpoint server did not stop")


@dataclass
class ControlledSegmentationFixture:
    fixture: dict
    weights: Path
    image: Path
    fixture_json: Path
    dimensions: list[int]
    receipt: dict | None = None
    cycles: list[dict] = field(default_factory=list)

    @property
    def reference(self):
        ref = self.fixture["frozen_model_ref"]
        runtime = (self.receipt or {}).get("inference_runtime")
        return {**ref, "inference_runtime": runtime} if runtime is not None else ref

    @property
    def task(self):
        return _fixture_task(self.fixture)

    @property
    def check_prefix(self):
        return f"controlled_{self.task}"

    @property
    def output_check(self):
        return (
            f"{self.check_prefix}_native_{'rle' if self.task == 'segment' else 'boxes'}"
        )

    @property
    def checks(self):
        return (
            f"{self.check_prefix}_install",
            f"{self.check_prefix}_reference_mismatch_rejected",
            f"{self.check_prefix}_receipt_mismatch_rejected",
            self.output_check,
            f"{self.check_prefix}_cold_reload",
        )

    @property
    def pool_key(self):
        return f"trained:{self.reference['model_version_id']}/{self.reference['sha256']}:{digest(self.receipt)}/{self.task}"

    def predict_body(self):
        return {
            "tasks": [{"id": f"controlled-{self.task}", "file_path": str(self.image)}],
            "context": {
                "task": self.task,
                "model_id": f"trained:{self.reference['model_version_id']}",
                "frozen_model_ref": self.reference,
                "params": {
                    "conf": self.fixture["confidence"],
                    "iou": 0.7,
                    "max_det": self.fixture["max_det"],
                },
            },
        }

    async def install(self, client):
        expected = {
            **{k: v for k, v in self.reference.items() if k != "inference_runtime"},
            "classes": self.fixture["classes"],
            "framework": "ultralytics",
            "framework_version": self.fixture.get("framework_version", "8.4.177"),
            "task": "segmentation" if self.task == "segment" else "detection",
            **_fixture_identity(self.fixture),
        }
        with _weight_server(self.weights) as download_url:
            body = InstallRequest.model_validate(
                {
                    **expected,
                    "size_bytes": self.weights.stat().st_size,
                    "download_url": download_url,
                }
            ).model_dump(mode="json", exclude_none=True)
            installed = await client.post("/managed-models", json=body)
            received = installed.json()
            if "inference_runtime" in received:
                from training_contract.inference_runtime import (
                    validate_inference_runtime,
                )

                validate_inference_runtime(expected, received["inference_runtime"])
                pinned = self.reference.get("inference_runtime")
                if pinned is not None and pinned != received["inference_runtime"]:
                    raise AssertionError("Controlled frozen inference runtime differs")
                expected["inference_runtime"] = received["inference_runtime"]
            if installed.status_code != 200 or received != expected:
                raise AssertionError("Controlled checkpoint installation failed")
            # Reinstallation must use the real immutable-receipt conflict guard.
            conflicting = copy.deepcopy(body)
            conflicting["classes"] = ["lifecycle-conflict-" + str(uuid.uuid4())]
            conflicting["classes_digest"] = digest(conflicting["classes"])
            rejected = await client.post("/managed-models", json=conflicting)
            if rejected.status_code != 409:
                raise AssertionError("Conflicting managed receipt was not rejected")
        response = await client.get(
            f"/managed-models/{self.reference['model_version_id']}"
        )
        if response.status_code != 200 or response.json() != expected:
            raise AssertionError(
                "Installed managed receipt differs from the frozen fixture"
            )
        self.receipt = expected

    async def reject_mismatched_references(self, client, headers):
        for name in ("sha256", "classes_digest"):
            body = self.predict_body()
            body = copy.deepcopy(body)
            old = body["context"]["frozen_model_ref"][name]
            body["context"]["frozen_model_ref"][name] = (
                "0" if old[0] != "0" else "1"
            ) + old[1:]
            response = await client.post("/predict", json=body, headers=headers())
            if response.status_code != 409:
                raise AssertionError(
                    "Mismatched frozen managed reference was not rejected"
                )

    def inspect_prediction(self, response, *, cycle: int, snapshot: dict):
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("results"), list)
            or len(response["results"]) != 1
        ):
            raise AssertionError("Expected one controlled batch prediction")
        item = response["results"][0]
        meta = item.get("meta") or {}
        if (
            item.get("task") != f"controlled-{self.task}"
            or item.get("model_version")
            != f"trained:{self.reference['model_version_id']}"
            or meta.get("trained_model")
            != {k: v for k, v in self.reference.items() if k != "inference_runtime"}
            or (
                "inference_runtime" in self.reference
                and meta.get("inference_runtime") != self.reference["inference_runtime"]
            )
        ):
            raise AssertionError("Controlled native response identity differs")
        if self.task == "segment":
            details = self._inspect_masks(item, meta)
        else:
            details = self._inspect_boxes(item, meta)
        if (
            item.get("cache_hit") is not False
            or self.pool_key not in {entry["key"] for entry in snapshot["loaded_keys"]}
            or snapshot["gpu_resident"] is not True
            or not str(snapshot["device"]).startswith("cuda")
            or snapshot["builders"]
            or snapshot["borrowers"]
            or snapshot["waiters"]
        ):
            raise AssertionError(
                "Controlled prediction did not cold-load its exact CUDA pool key"
            )
        self.cycles.append(
            {
                "cycle": cycle,
                **details,
                "cold_load": True,
                "unloaded": False,
            }
        )

    def _inspect_masks(self, item, meta):
        runtime = self.receipt.get("inference_runtime", {}).get(
            "qualification", self.receipt["runtime_qualification"]
        )
        if meta.get("segmentation") != {
            "output_contract": self.receipt["output_contract"],
            "dimensions": self.dimensions,
            "mask_processing": runtime["mask_processing"],
            "decoder": runtime["decoder"],
            "runtime_qualification": runtime,
        }:
            raise AssertionError("Controlled native mask response identity differs")
        objects, instances = meta.get("mask_objects"), item.get("result")
        if (
            not isinstance(objects, dict)
            or not isinstance(instances, list)
            or not self.fixture["min_instances"]
            <= len(instances)
            <= self.fixture["max_det"]
            or len(json.dumps(meta, separators=(",", ":")).encode())
            > MAX_MASK_RESPONSE_BYTES
        ):
            raise AssertionError(
                "Controlled fixture did not produce bounded native instances"
            )
        referenced = set()
        for index, instance in enumerate(instances):
            geometry = instance.get("geometry") or {}
            ref = geometry.get("mask") or {}
            sha = ref.get("sha256")
            if sha not in objects:
                raise AssertionError("Controlled native mask object is missing")
            rle = CocoRlePayload.model_validate(objects[sha])
            encoded = canonical_rle_bytes(rle)
            class_index = instance.get("native_class_index")
            score = instance.get("score")
            if (
                geometry.get("type") != "raster_mask"
                or instance.get("tool_unit_id") != "region"
                or instance.get("instance_index") != index
                or type(class_index) is not int
                or not 0 <= class_index < len(self.fixture["classes"])
                or instance.get("class_name") != self.fixture["classes"][class_index]
                or type(score) not in {int, float}
                or not math.isfinite(score)
                or not 0 <= score <= 1
                or score != instance.get("confidence")
                or rle.size != self.dimensions
                or sum(rle.counts[1::2]) <= 0
                or hashlib.sha256(encoded).hexdigest() != sha
                or ref
                != {
                    "encoding": "coco_rle_ref",
                    "size": self.dimensions,
                    "object_key": f"raster-masks/sha256/{sha[:2]}/{sha[2:4]}/{sha}.json",
                    "sha256": sha,
                    "runs": len(rle.counts),
                    "bytes": len(encoded),
                }
            ):
                raise AssertionError(
                    "Controlled native instance geometry or binding differs"
                )
            referenced.add(sha)
        if referenced != set(objects):
            raise AssertionError(
                "Controlled native mask objects contain unreferenced entries"
            )
        return {"instances": len(instances), "mask_objects": len(objects)}

    def _inspect_boxes(self, item, meta):
        boxes = item.get("result")
        expected_meta = {"trained_model"}
        if "inference_runtime" in self.reference:
            expected_meta.add("inference_runtime")
        if (
            not isinstance(boxes, list)
            or not self.fixture["min_instances"]
            <= len(boxes)
            <= self.fixture["max_det"]
            or set(meta) != expected_meta
        ):
            raise AssertionError("Controlled detection requires bounded native boxes")
        for box in boxes:
            if not isinstance(box, dict):
                raise AssertionError("Controlled native box must be an object")
            value = box.get("value")
            class_index, score = box.get("native_class_index"), box.get("score")
            if (
                box.get("type") != "rectanglelabels"
                or not isinstance(value, dict)
                or set(value) != {"x", "y", "width", "height", "rectanglelabels"}
                or type(class_index) is not int
                or not 0 <= class_index < len(self.fixture["classes"])
                or value["rectanglelabels"] != [self.fixture["classes"][class_index]]
                or type(score) not in {int, float}
                or not math.isfinite(score)
                or not self.fixture["confidence"] <= score <= 1
                or any(
                    type(value[key]) not in {int, float}
                    or not math.isfinite(value[key])
                    for key in ("x", "y", "width", "height")
                )
            ):
                raise AssertionError("Controlled native box class or shape differs")
            x, y, width, height = (value[key] for key in ("x", "y", "width", "height"))
            # Batch rectanglelabels use percentages, including floating point
            # division at image boundaries. Zero-area native boxes cannot qualify.
            if (
                not 0 <= x <= 100
                or not 0 <= y <= 100
                or width <= 0
                or height <= 0
                or x + width > 100 + 1e-6
                or y + height > 100 + 1e-6
            ):
                raise AssertionError("Controlled native box exceeds original image")
        return {"instances": len(boxes), "boxes": len(boxes)}

    def unloaded(self, *, cycle: int, snapshot: dict):
        if (
            snapshot["current_size"]
            or snapshot["loaded_keys"]
            or snapshot["gpu_resident"] is not False
        ):
            raise AssertionError("Controlled model remained in the shared pool")
        match = next(item for item in self.cycles if item["cycle"] == cycle)
        match["unloaded"] = True

    def evidence(self, *, model_approval: str, fixture_approval: str):
        if (
            len(self.cycles) != 2
            or [item["cycle"] for item in self.cycles] != [1, 2]
            or not all(item["unloaded"] for item in self.cycles)
        ):
            raise AssertionError(
                "Controlled model requires two complete cold-load/unload cycles"
            )
        artifacts = [
            artifact_evidence(self.weights, kind="weight", approval_ref=model_approval),
            artifact_evidence(
                self.image, kind="fixture", approval_ref=fixture_approval
            ),
            artifact_evidence(
                self.fixture_json, kind="fixture", approval_ref=fixture_approval
            ),
        ]
        return artifacts, {
            "frozen_model_ref": self.reference,
            "task": self.task,
            "base_model": self.fixture.get("base_model", "yolo11n-seg"),
            "framework_version": self.fixture.get("framework_version", "8.4.177"),
            **(
                {"inference_runtime": self.receipt["inference_runtime"]}
                if "inference_runtime" in self.receipt
                else {}
            ),
            "receipt_digest": digest(self.receipt),
            **(
                {"output_contract": self.receipt["output_contract"]}
                if self.task == "segment"
                else {}
            ),
            "runtime_qualification_digest": digest(
                self.receipt.get("inference_runtime", {}).get(
                    "qualification", self.receipt.get("runtime_qualification")
                )
            ),
            "image_sha256": artifacts[1]["sha256"],
            "fixture_sha256": artifacts[2]["sha256"],
            "dimensions": self.dimensions,
            "cycles": self.cycles,
        }


def load_controlled_fixture(root: Path):
    legacy = [os.getenv(name, "").strip() for name in INPUT_ENV]
    generic = [os.getenv(name, "").strip() for name in MODEL_INPUT_ENV]
    if any(legacy) and any(generic):
        raise ValueError(
            "Controlled model and segmentation input groups cannot be mixed"
        )
    inputs = generic if any(generic) else legacy
    if not any(inputs):
        return None
    if not all(inputs):
        raise ValueError("All three controlled model inputs are required")
    weights, fixture_json, image = map(Path, inputs)
    fixture = _read_fixture(fixture_json)
    if (
        not 0 < weights.stat().st_size <= 2 * 1024**3
        or not 0 < image.stat().st_size <= 256 * 1024**2
    ):
        raise ValueError("Controlled input files exceed their byte budget")
    target = root / "controlled-inputs"
    target.mkdir()
    private_weights, private_image, private_fixture = (
        target / "weights.pt",
        target / "image.bin",
        target / "fixture.json",
    )
    for source, destination in (
        (weights, private_weights),
        (image, private_image),
        (fixture_json, private_fixture),
    ):
        shutil.copyfile(source, destination)
    if (
        _read_fixture(private_fixture) != fixture
        or file_digest(private_weights) != fixture["frozen_model_ref"]["sha256"]
    ):
        raise ValueError("Controlled input identity changed while freezing the fixture")
    with Image.open(private_image) as opened:
        width, height = opened.size
        validate_budget(
            width,
            height,
            fixture["max_det"] if _fixture_task(fixture) == "segment" else 1,
        )
        opened.verify()
    return ControlledSegmentationFixture(
        fixture, private_weights, private_image, private_fixture, [height, width]
    )
