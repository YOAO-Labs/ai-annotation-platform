"""Qualified managed inference and durable, bounded sample request receipts."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import io
import importlib.metadata
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

from managed_models import digest
from training_contract.models import model_spec

MAX_RESPONSE_BYTES = 256 * 1024
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_RECEIPTS = 4096
LOCK_STRIPES = 64
HASH = re.compile(r"^[a-f0-9]{64}$")
FIXED_OPTIONS = {
    "imgsz": 640,
    "rect": False,
    "quantize": 32,
    "augment": False,
    "agnostic_nms": False,
    "max_det": 100,
    "batch": 1,
    "classes": None,
    "compile": False,
    "channels_last": False,
    "dnn": False,
    "nms": None,
    "save": False,
    "save_txt": False,
    "save_crop": False,
    "save_conf": False,
    "show": False,
    "visualize": False,
    "embed": None,
    "verbose": False,
    "stream": False,
}


def model_options(base_model=None):
    options = dict(FIXED_OPTIONS)
    if base_model is not None:
        options["nms"] = (
            False if model_spec("ultralytics", base_model).end2end else None
        )
    return options


def receipt_base_model(receipt):
    if receipt.get("framework_version") != "8.4.183":
        return None
    suffix = "-seg" if receipt.get("task") == "segmentation" else ""
    return f"{receipt.get('model_family')}{receipt.get('model_size')}{suffix}"


def verify_inference_branch(model, receipt):
    base_model = receipt_base_model(receipt)
    if base_model is None:
        return
    expected = model_spec("ultralytics", base_model).end2end
    observed = bool(getattr(model.predictor.model, "end2end", False))
    if observed != expected:
        raise ValueError(
            "Observed native inference branch differs from the installed model"
        )


def error(status, reason, message):
    return HTTPException(
        status_code=status, detail={"reason": reason, "message": message}
    )


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    binding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    storage_identity: uuid.UUID
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    issued_at: int = Field(gt=0, strict=True)
    expires_at: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def fixed_window(self):
        if self.expires_at - self.issued_at != 86400:
            raise ValueError("Evaluation requests have one fixed 24-hour window")
        return self


class EvaluationSeal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    storage_identity: uuid.UUID
    issued_at: int = Field(gt=0, strict=True)
    expires_at: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def fixed_window(self):
        if self.expires_at - self.issued_at != 86400:
            raise ValueError("Evaluation requests have one fixed 24-hour window")
        return self


def request_binding(task_id, model_ref, params, request):
    identity = request.model_dump(mode="json", exclude={"binding_digest"})
    return digest(
        {
            "task_id": str(task_id),
            "model_ref": model_ref,
            "params": params,
            "evaluation": identity,
        }
    )


@lru_cache(maxsize=1)
def runtime_identity():
    version = importlib.metadata.version("ultralytics")
    if version not in {"8.4.177", "8.4.183"}:
        raise error(
            409,
            "evaluation_runtime_unqualified",
            "This Ultralytics runtime has not qualified for sample evaluation",
        )
    root = Path(
        importlib.metadata.distribution("ultralytics").locate_file("ultralytics")
    )
    paths = {
        f"ultralytics/{name}": root / name
        for name in (
            "engine/model.py",
            "engine/predictor.py",
            "models/yolo/detect/predict.py",
            "utils/nms.py",
            "data/loaders.py",
            "data/augment.py",
            "nn/autobackend.py",
            "cfg/default.yaml",
        )
    }
    paths.update(
        {
            f"backend/{name}": Path(__file__).with_name(name)
            for name in (
                "sample_evaluations.py",
                "predictor.py",
                "schemas.py",
                "main.py",
                "managed_models.py",
            )
        }
    )
    for name in ("inference_runtime", "model_return", "models", "native_architecture"):
        module = importlib.import_module(f"training_contract.{name}")
        paths[f"training_contract/{name}.py"] = Path(module.__file__)
    try:
        sources = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in paths.items()
        }
    except OSError as exc:
        raise error(
            409,
            "evaluation_runtime_unqualified",
            "Evaluation runtime source evidence is unavailable",
        ) from exc
    return {
        "schema": 1,
        "implementation": "aap-ultralytics-detection",
        "revision": 2 if version == "8.4.183" else 1,
        "ultralytics_version": version,
        "torch_version": importlib.metadata.version("torch"),
        "pillow_version": importlib.metadata.version("Pillow"),
        "source_digest": digest(sources),
        "options": FIXED_OPTIONS,
    }


def qualification_profile(device, storage_identity, base_model=None):
    profile = {
        **runtime_identity(),
        "effective_device": str(device),
        "storage_identity": str(storage_identity),
        "options": model_options(base_model),
    }
    return {**profile, "profile_digest": digest(profile)}


def verify_input(file_path, request):
    prefix = "data:image/png;base64,"
    if (
        not file_path.startswith(prefix)
        or len(file_path) > len(prefix) + ((MAX_INPUT_BYTES + 2) // 3) * 4
    ):
        raise error(
            422,
            "evaluation_input_invalid",
            "Evaluation requires a bounded, normalized PNG input",
        )
    try:
        raw = base64.b64decode(file_path[len(prefix) :], validate=True)
        if (
            not 0 < len(raw) <= MAX_INPUT_BYTES
            or hashlib.sha256(raw).hexdigest() != request.input_sha256
        ):
            raise ValueError("Input digest differs")
        with Image.open(io.BytesIO(raw)) as image:
            if (
                image.format != "PNG"
                or image.mode != "RGB"
                or image.width * image.height > MAX_PIXELS
                or image.getexif()
                or image.info
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ValueError("Input is not a normalized RGB PNG")
            image.load()
            return {"width": image.width, "height": image.height}
    except (ValueError, OSError, binascii.Error, Image.DecompressionBombError) as exc:
        raise error(
            422,
            "evaluation_input_invalid",
            "Evaluation input bytes or logical image contract differ",
        ) from exc


def observed_inference(model, ctx, width, height):
    expected_options = ctx._evaluation_profile["options"]
    args = model.predictor.args
    actual = {
        key: getattr(args, key, None) for key in expected_options if key != "stream"
    }
    actual["stream"] = False
    if actual.get("imgsz") == [640, 640]:
        actual["imgsz"] = 640
    if (
        actual != expected_options
        or str(model.device) != ctx._evaluation_profile["effective_device"]
    ):
        raise error(
            409,
            "evaluation_options_differ",
            "Effective inference settings differ from the frozen profile",
        )
    if list(model.predictor.imgsz) != [640, 640] or getattr(
        model.predictor.model, "fp16", False
    ):
        raise error(
            409,
            "evaluation_options_differ",
            "Effective input size or precision differs from the frozen profile",
        )
    request = ctx.sample_evaluation
    return {
        "request_id": str(request.request_id),
        "binding_digest": request.binding_digest,
        "profile_digest": request.profile_digest,
        "storage_identity": str(request.storage_identity),
        "input_sha256": request.input_sha256,
        "issued_at": request.issued_at,
        "expires_at": request.expires_at,
        "width": width,
        "height": height,
        "effective_device": str(model.device),
        "options": actual,
        "conf": ctx.params.conf,
        "nms_iou": ctx.params.iou,
        "end2end": bool(getattr(model.predictor.model, "end2end", False)),
    }


class EvaluationRequestStore:
    """Fixed lock inodes protect native work, atomic receipts and delayed POST seals."""

    def __init__(self, root: Path, *, response_bytes=None):
        self.root = root
        self.response_bytes = response_bytes

    @property
    def storage_identity(self):
        self.root.mkdir(parents=True, exist_ok=True)
        identity = self.root / "storage-identity"
        with self._lock("identity"):
            try:
                fd = os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w") as stream:
                    stream.write(str(uuid.uuid4()))
                    stream.flush()
                    os.fsync(stream.fileno())
            try:
                return uuid.UUID(identity.read_text())
            except (OSError, ValueError) as exc:
                raise error(
                    409,
                    "evaluation_store_unavailable",
                    "Persistent evaluation store identity is unavailable",
                ) from exc

    def _assert_storage(self, storage_identity):
        if self.storage_identity != storage_identity:
            raise error(
                409,
                "evaluation_store_changed",
                "The frozen evaluation request store has changed",
            )

    @contextmanager
    def _lock(self, name):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f"lock-{name}").open("a+b") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise error(
                    409,
                    "evaluation_request_busy",
                    "Evaluation request is still running or being sealed",
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _stripe(self, request_id):
        return uuid.UUID(str(request_id)).int % LOCK_STRIPES

    def _path(self, request_id):
        return self.root / f"{uuid.UUID(str(request_id))}.json"

    def _read(self, request_id):
        path = self._path(request_id)
        if not path.exists():
            return None
        try:
            with path.open("rb") as stream:
                raw = stream.read((self.response_bytes or MAX_RESPONSE_BYTES) + 1)
            if len(raw) > (self.response_bytes or MAX_RESPONSE_BYTES):
                raise ValueError("Receipt exceeds budget")
            result = json.loads(raw)
            if (
                not isinstance(result, dict)
                or set(result)
                != {
                    "schema",
                    "request_id",
                    "binding_digest",
                    "profile_digest",
                    "issued_at",
                    "expires_at",
                    "state",
                    "response",
                    "response_digest",
                }
                or type(result["schema"]) is not int
                or result["schema"] != 1
                or result["request_id"] != str(request_id)
                or not HASH.fullmatch(result["binding_digest"])
                or not HASH.fullmatch(result["profile_digest"])
                or type(result["issued_at"]) is not int
                or type(result["expires_at"]) is not int
                or result["expires_at"] - result["issued_at"] != 86400
                or result["state"] not in {"bound", "completed", "sealed"}
                or (result["state"] == "completed")
                != isinstance(result["response"], dict)
                or result["response_digest"]
                != (
                    digest(result["response"])
                    if result["state"] == "completed"
                    else None
                )
            ):
                raise ValueError("Invalid receipt")
            return result
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise error(
                409,
                "evaluation_receipt_corrupt",
                "Evaluation receipt is corrupt; its identity cannot be replaced",
            ) from exc

    def _write(self, request_id, record):
        raw = json.dumps(
            record, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(raw) > (self.response_bytes or MAX_RESPONSE_BYTES):
            raise error(
                413,
                "evaluation_response_too_large",
                "Evaluation response exceeds its persistent byte budget",
            )
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.root, prefix=".receipt-", delete=False
            ) as temporary:
                path = Path(temporary.name)
                temporary.write(raw)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(path, self._path(request_id))
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    @staticmethod
    def _binding_matches(record, request):
        if any(
            record[key] != getattr(request, key)
            for key in ("binding_digest", "profile_digest", "issued_at", "expires_at")
        ):
            raise error(
                409,
                "evaluation_request_identity_conflict",
                "Evaluation request identity is immutable, including its original expiry",
            )

    def _reserve(self, request_id, request, state):
        record = {
            "schema": 1,
            "request_id": str(request_id),
            "binding_digest": request.binding_digest,
            "profile_digest": request.profile_digest,
            "issued_at": request.issued_at,
            "expires_at": request.expires_at,
            "state": state,
            "response": None,
            "response_digest": None,
        }
        with self._lock("quota"):
            paths = list(self.root.glob("*.json"))
            if len(paths) >= MAX_RECEIPTS:
                self._cleanup_expired(paths)
                if sum(1 for _ in self.root.glob("*.json")) >= MAX_RECEIPTS:
                    raise error(
                        429,
                        "evaluation_store_full",
                        "Evaluation receipt capacity is full; retry after existing requests expire",
                    )
            self._write(request_id, record)
        return record

    def _cleanup_expired(self, paths):
        now = int(time.time())
        for path in paths[: MAX_RECEIPTS + LOCK_STRIPES]:
            try:
                request_id = uuid.UUID(path.stem)
                with self._lock(self._stripe(request_id)):
                    record = self._read(request_id)
                    if record and record["expires_at"] <= now:
                        path.unlink(missing_ok=True)
            except (ValueError, HTTPException):
                continue
        # Crashed atomic writes are never published receipts.
        for path in self.root.glob(".receipt-*"):
            if path.stat().st_mtime < now - 86400:
                path.unlink(missing_ok=True)

    @contextmanager
    def execution(self, request):
        self._assert_storage(request.storage_identity)
        with self._lock(self._stripe(request.request_id)):
            record = self._read(request.request_id)
            if record:
                self._binding_matches(record, request)
            now = int(time.time())
            if now < request.issued_at - 60 or now >= request.expires_at:
                raise error(
                    410,
                    "evaluation_request_expired",
                    "Evaluation request is outside its original execution window",
                )
            if record is None:
                record = self._reserve(request.request_id, request, "bound")
            if record["state"] == "sealed":
                raise error(
                    410,
                    "evaluation_request_sealed",
                    "Evaluation request was sealed and cannot start late native work",
                )
            yield record

    def publish(self, request, record, response):
        if record["state"] == "completed":
            if record["response"] != response:
                raise error(
                    409,
                    "evaluation_result_conflict",
                    "Completed evaluation response is immutable",
                )
            return
        results = response.get("results", [])
        if len(results) != 1 or len(results[0].get("result", [])) > 100:
            raise error(
                422,
                "evaluation_response_invalid",
                "Evaluation requires one bounded sample response",
            )
        completed = {
            **record,
            "state": "completed",
            "response": response,
            "response_digest": digest(response),
        }
        self._write(request.request_id, completed)
        record.update(completed)

    def receipt(self, request_id, *, binding_digest, storage_identity):
        self._assert_storage(storage_identity)
        with self._lock(self._stripe(request_id)):
            record = self._read(request_id)
            if record is None:
                return {
                    "state": "missing",
                    "request_id": str(request_id),
                    "binding_digest": binding_digest,
                    "storage_identity": str(storage_identity),
                }
            if record["binding_digest"] != binding_digest:
                raise error(
                    409,
                    "evaluation_request_identity_conflict",
                    "Evaluation receipt belongs to another frozen input",
                )
            return {**record, "storage_identity": str(storage_identity)}

    def seal(self, request_id, request):
        self._assert_storage(request.storage_identity)
        if request.issued_at > int(time.time()) + 60:
            raise error(
                422,
                "evaluation_request_invalid",
                "Evaluation request issue time is in the future",
            )
        with self._lock(self._stripe(request_id)):
            record = self._read(request_id)
            if record:
                self._binding_matches(record, request)
            if record is None:
                record = self._reserve(request_id, request, "sealed")
            elif record["state"] == "bound":
                record = {**record, "state": "sealed"}
                self._write(request_id, record)
            return {**record, "storage_identity": str(request.storage_identity)}
