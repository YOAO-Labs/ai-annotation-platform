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
from training_contract.model_return import segmentation_identity


INPUT_ENV = (
    "VALIDATION_CONTROLLED_SEGMENT_WEIGHTS",
    "VALIDATION_CONTROLLED_SEGMENT_FIXTURE",
    "VALIDATION_CONTROLLED_SEGMENT_IMAGE",
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


def _read_fixture(path: Path) -> dict:
    if not 0 < path.stat().st_size <= 64 * 1024:
        raise ValueError("Controlled fixture JSON exceeds its byte budget")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or value.get("schema") not in {1, 2}:
        raise ValueError("Unsupported controlled fixture schema")
    fields = FIXTURE_FIELDS | (
        {"base_model", "framework_version"} if value["schema"] == 2 else set()
    )
    if set(value) != fields:
        raise ValueError("Controlled fixture fields differ from the contract")
    if type(value["schema"]) is not int:
        raise ValueError("Unsupported controlled fixture schema")
    if value["schema"] == 2:
        segmentation_identity(
            value["base_model"], framework_version=value["framework_version"]
        )
    ref = ManagedModelRef.model_validate(value["frozen_model_ref"])
    if ref.model_dump(mode="json") != value["frozen_model_ref"]:
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
        return self.fixture["frozen_model_ref"]

    @property
    def pool_key(self):
        return f"trained:{self.reference['model_version_id']}/{self.reference['sha256']}:{digest(self.receipt)}/segment"

    def predict_body(self):
        return {
            "tasks": [{"id": "controlled-segment", "file_path": str(self.image)}],
            "context": {
                "task": "segment",
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
            **self.reference,
            "classes": self.fixture["classes"],
            "framework": "ultralytics",
            "framework_version": self.fixture.get("framework_version", "8.4.177"),
            "task": "segmentation",
            **segmentation_identity(
                self.fixture.get("base_model", "yolo11n-seg"),
                framework_version=self.fixture.get("framework_version", "8.4.177"),
            ),
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
            if installed.status_code != 200 or installed.json() != expected:
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
        segmentation = meta.get("segmentation") or {}
        if (
            item.get("task") != "controlled-segment"
            or item.get("model_version")
            != f"trained:{self.reference['model_version_id']}"
            or meta.get("trained_model") != self.reference
            or segmentation
            != {
                "output_contract": self.receipt["output_contract"],
                "dimensions": self.dimensions,
                "mask_processing": self.receipt["runtime_qualification"][
                    "mask_processing"
                ],
                "decoder": self.receipt["runtime_qualification"]["decoder"],
                "runtime_qualification": self.receipt["runtime_qualification"],
            }
        ):
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
                "instances": len(instances),
                "mask_objects": len(objects),
                "cold_load": True,
                "unloaded": False,
            }
        )

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
                "Controlled segmentation requires two complete cold-load/unload cycles"
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
            "base_model": self.fixture.get("base_model", "yolo11n-seg"),
            "framework_version": self.fixture.get("framework_version", "8.4.177"),
            "receipt_digest": digest(self.receipt),
            "output_contract": self.receipt["output_contract"],
            "runtime_qualification_digest": digest(
                self.receipt["runtime_qualification"]
            ),
            "image_sha256": artifacts[1]["sha256"],
            "fixture_sha256": artifacts[2]["sha256"],
            "dimensions": self.dimensions,
            "cycles": self.cycles,
        }


def load_controlled_fixture(root: Path):
    inputs = [os.getenv(name, "").strip() for name in INPUT_ENV]
    if not any(inputs):
        return None
    if not all(inputs):
        raise ValueError("All three controlled segmentation inputs are required")
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
        validate_budget(width, height, fixture["max_det"])
        opened.verify()
    return ControlledSegmentationFixture(
        fixture, private_weights, private_image, private_fixture, [height, width]
    )
