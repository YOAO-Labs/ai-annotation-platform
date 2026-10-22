"""Authenticated, immutable installation of qualified platform-produced weights."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import importlib
import json
import os
import secrets
import tempfile
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from aap_backend_runtime.backend_auth import backend_credential
from training_contract.model_return import (
    LIBRE_FORMAT,
    LIBRE_RUNTIME,
    libre_identity,
    ultralytics_identity,
)
from training_contract.inference_runtime import inference_runtime
from training_contract.models import model_spec
from training_contract.native_architecture import (
    validate_architecture_yaml,
    matches_architecture_signature,
)
from managed_segmentation import validate_receipt as validate_segmentation_receipt


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def runtime_receipt(receipt):
    """Decorate a response without changing the immutable on-disk training receipt."""
    framework = importlib.import_module(receipt["framework"])
    try:
        runtime = inference_runtime(receipt, framework.__version__)
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="Unqualified training/inference runtime combination"
        ) from exc
    return {
        **receipt,
        "inference_runtime": runtime,
    }


class ManagedModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_version_id: uuid.UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classes_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    inference_runtime: dict | None = None


class InstallRequest(ManagedModelRef):
    classes: list[str] = Field(min_length=1, max_length=1000)
    framework: Literal["ultralytics", "libreyolo"]
    framework_version: str = Field(min_length=1, max_length=50)
    task: Literal["detection", "segmentation"]
    size_bytes: int = Field(gt=0, le=2 * 1024**3)
    download_url: str = Field(max_length=10000)
    model_family: str | None = None
    model_size: str | None = None
    artifact_format: str | None = None
    artifact_size_bytes: int | None = None
    runtime_qualification: dict | None = None
    output_contract: str | None = None

    @model_validator(mode="after")
    def validate_identity(self):
        if self.inference_runtime is not None:
            raise ValueError(
                "Inference identity is not part of the immutable training artifact"
            )
        url = urlsplit(self.download_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
        ):
            raise ValueError("Expected a platform HTTP object URL")
        if digest(self.classes) != self.classes_digest or any(
            not name or len(name) > 120 for name in self.classes
        ):
            raise ValueError("Class identity mismatch")
        extra = (
            self.model_family,
            self.model_size,
            self.artifact_format,
            self.artifact_size_bytes,
            self.runtime_qualification,
        )
        if self.task == "segmentation":
            validate_segmentation_receipt(self.model_dump(mode="json"))
        elif self.output_contract is not None:
            raise ValueError("Unexpected output contract for detection install")
        elif self.framework == "libreyolo":
            identity = libre_identity(f"LibreYOLO9{self.model_size}")
            if (
                self.framework_version != "1.6.0"
                or any(getattr(self, key) != value for key, value in identity.items())
                or self.artifact_size_bytes != self.size_bytes
            ):
                raise ValueError("Unqualified native managed identity")
        elif self.framework_version == "8.4.183":
            validate_detection_receipt(self.model_dump(mode="json"))
        elif self.framework_version != "8.4.177" or any(
            value is not None for value in extra
        ):
            raise ValueError("Unexpected qualification fields for legacy install")
        return self


def authenticate(request: Request):
    expected = backend_credential("yolo")
    supplied = request.headers.get("Authorization", "")
    if len(expected) < 32 or not secrets.compare_digest(supplied, f"Bearer {expected}"):
        raise HTTPException(
            status_code=403, detail="Controlled model authentication failed"
        )


def file_digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate_detection_receipt(receipt):
    expected = ultralytics_identity(
        f"{receipt.get('model_family')}{receipt.get('model_size')}",
        framework_version=receipt.get("framework_version"),
    )
    if (
        receipt.get("task") != "detection"
        or receipt.get("framework") != "ultralytics"
        or receipt.get("framework_version") != "8.4.183"
        or any(receipt.get(key) != value for key, value in expected.items())
        or receipt.get("artifact_format") is not None
        or receipt.get("artifact_size_bytes") is not None
        or receipt.get("output_contract") is not None
    ):
        raise ValueError("Unqualified detection receipt")


def verify_native_runtime(receipt):
    runtime = receipt["runtime_qualification"]
    for name in ("ultralytics", "torch", "torchvision"):
        if importlib.import_module(name).__version__ != runtime[name]:
            raise ValueError("Unqualified native inference runtime")
    for name, expected in runtime.get("source_sha256", {}).items():
        source = Path(importlib.import_module(name).__file__).read_bytes()
        if hashlib.sha256(source).hexdigest() != expected:
            raise ValueError("Native inference source identity changed")


def _reference_model(base_model, classes):
    import torch
    from ultralytics.nn.tasks import DetectionModel, SegmentationModel

    source_classes = None
    if isinstance(base_model, tuple):
        base_model, source_classes = base_model
    native_type = SegmentationModel if base_model.endswith("-seg") else DetectionModel
    with torch.random.fork_rng(devices=[]), torch.device("cpu"), torch.no_grad():
        model = native_type(base_model + ".yaml", nc=classes, verbose=False)
        if source_classes is not None:
            source = native_type(base_model + ".yaml", nc=source_classes, verbose=False)
            model.load(source, verbose=False)
        return model


class ManagedModelStore:
    def __init__(self, root: Path):
        self.root = root
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def _process_lock(self, namespace: str, identity: str):
        """Serialize local processes without blocking the event loop or threads.

        Lock files remain in place: unlinking them permits another process to
        acquire a different inode for the same logical lock. Cancellation closes
        the descriptor before it can be reused; no background flock outlives it.
        All installation paths acquire version before digest, never the reverse.
        """
        lock = self._locks.setdefault(f"{namespace}:{identity}", asyncio.Lock())
        async with lock:
            directory = self.root / ".locks" / namespace
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            (self.root / ".locks").chmod(0o700)
            descriptor = os.open(
                directory / f"{identity}.lock", os.O_CREAT | os.O_RDWR, 0o600
            )
            try:
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
                yield
            finally:
                os.close(descriptor)

    @staticmethod
    def _sync_directory(path):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _matching_receipt(directory, expected):
        """A damaged artifact never authorizes rebinding its installation ID."""
        try:
            if not directory.is_dir() or directory.is_symlink():
                raise ValueError("Invalid installation directory")
            with (directory / "receipt.json").open("rb") as source:
                raw = source.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024 or json.loads(raw.decode("utf-8")) != expected:
                raise ValueError("Installed identity differs")
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=409,
                detail="Installed model versions are immutable or require receipt recovery",
            ) from exc
        return raw

    @staticmethod
    def _valid_blob(path, body):
        try:
            return bool(
                not path.is_symlink()
                and path.is_file()
                and path.stat().st_size == body.size_bytes
                and file_digest(path) == body.sha256
            )
        except OSError:
            return False

    @staticmethod
    async def _download_blob(body, path):
        sha = hashlib.sha256()
        total = 0
        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
            async with client.stream("GET", body.download_url) as response:
                response.raise_for_status()
                if (
                    response.headers.get("Content-Length")
                    and int(response.headers["Content-Length"]) != body.size_bytes
                ):
                    raise HTTPException(status_code=422, detail="Artifact size differs")
                with path.open("wb") as stream:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > body.size_bytes:
                            raise HTTPException(
                                status_code=422, detail="Artifact size exceeded"
                            )
                        sha.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
        if total != body.size_bytes or sha.hexdigest() != body.sha256:
            raise HTTPException(status_code=422, detail="Artifact digest differs")

    async def _inspect_for_install(self, path, expected):
        try:
            await asyncio.to_thread(self.inspect, path, expected)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail="Checkpoint task/classes/runtime validation failed",
            ) from exc

    async def _materialize_blob(self, body, path, expected):
        # Private artifacts never enter the public /model-assets bind. The blob
        # directory belongs to this environment's existing managed state volume.
        blobs = self.root / ".blobs" / "sha256"
        blobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / ".blobs").chmod(0o700)
        blob = blobs / f"{body.sha256}.pt"
        async with self._process_lock("sha256", body.sha256):
            if await asyncio.to_thread(self._valid_blob, blob, body):
                os.link(blob, path)
                # Byte reuse is not another installation's task, class or native
                # runtime qualification. Inspect every new installation identity.
                await self._inspect_for_install(path, expected)
            else:
                await self._download_blob(body, path)
                await self._inspect_for_install(path, expected)
                path.chmod(0o400)
                # Never modify an inode that another retained version may link.
                os.replace(path, blob)
                self._sync_directory(blobs)
                os.link(blob, path)

    def receipt(self, version_id: str, *, verify=True):
        version_id = str(uuid.UUID(version_id))
        directory = self.root / version_id
        try:
            receipt = json.loads((directory / "receipt.json").read_text())
            path = directory / "weights.pt"
            if receipt["model_version_id"] != version_id or not path.is_file():
                raise ValueError("Installed identity mismatch")
            if verify and file_digest(path) != receipt["sha256"]:
                raise ValueError("Installed artifact digest mismatch")
            if receipt.get("task") == "segmentation":
                validate_segmentation_receipt(receipt)
                if digest(receipt["classes"]) != receipt["classes_digest"]:
                    raise ValueError("Installed segmentation classes differ")
            elif receipt.get("output_contract") is not None:
                raise ValueError("Unexpected detection output contract")
            elif (
                receipt.get("framework") == "ultralytics"
                and receipt.get("framework_version") == "8.4.183"
            ):
                validate_detection_receipt(receipt)
            if receipt.get("framework") == "libreyolo" and (
                receipt.get("framework_version") != "1.6.0"
                or receipt.get("model_family") != "yolo9"
                or receipt.get("model_size") not in {"t", "s", "m", "c"}
                or receipt.get("artifact_format") != LIBRE_FORMAT
                or receipt.get("artifact_size_bytes") != path.stat().st_size
                or receipt.get("runtime_qualification") != LIBRE_RUNTIME
                or digest(receipt["classes"]) != receipt["classes_digest"]
            ):
                raise ValueError("Installed native qualification differs")
            return receipt
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(
                status_code=409, detail="Controlled model is missing or invalid"
            ) from exc

    def resolve(self, version_id: str, sha256: str):
        receipt = self.receipt(version_id)
        if receipt["sha256"] != sha256:
            raise FileNotFoundError("Controlled model weight digest differs")
        return self.root / str(uuid.UUID(version_id)) / "weights.pt", receipt

    @staticmethod
    def inspect(path, expected):
        if expected["framework"] == "libreyolo":
            from libreyolo_adapter import LibreDetectionModel

            return LibreDetectionModel(path, expected)
        import ultralytics
        from ultralytics import YOLO

        runtime = inference_runtime(expected, ultralytics.__version__)
        qualified_native = ultralytics.__version__ == "8.4.183"
        model = YOLO(str(path))
        model.to("cpu")
        segmentation = expected["task"] == "segmentation"
        if model.task != ("segment" if segmentation else "detect") or not isinstance(
            model.names, dict
        ):
            raise ValueError("Expected a native task-matched checkpoint")
        if any(type(index) is not int for index in model.names) or set(
            model.names
        ) != set(range(len(model.names))):
            raise ValueError("Checkpoint names must have contiguous native indexes")
        names = [model.names[i] for i in range(len(model.names))]
        if names != expected["classes"] or digest(names) != expected["classes_digest"]:
            raise ValueError(
                "Checkpoint native classes differ from the registered order"
            )
        if segmentation or qualified_native:
            from ultralytics.nn.modules import head as native_heads
            from ultralytics.nn.tasks import yaml_model_load

            family, scale = expected.get("model_family"), expected.get("model_size")
            if expected["framework_version"] == "8.4.177" and not segmentation:
                family, scale = "yolo11", model.model.yaml.get("scale")
            base_model = f"{family}{scale}{'-seg' if segmentation else ''}"
            spec = model_spec("ultralytics", base_model)
            architecture = model.model.yaml
            head = model.model.model[-1]
            validate_architecture_yaml(
                architecture, spec, yaml_model_load(spec.yaml_filename)
            )
            if (
                type(head) is not getattr(native_heads, spec.head)
                or head.nc != len(names)
                or (getattr(head, "one2one_cv2", None) is not None) != spec.end2end
                or spec.end2end
                and head.reg_max != 1
                or not matches_architecture_signature(
                    model.model,
                    spec.id,
                    head.nc,
                    _reference_model,
                    ultralytics.__version__,
                )
            ):
                raise ValueError(
                    "Unqualified native model family, scale, head or inference branch"
                )
            if not segmentation:
                if expected["framework_version"] == "8.4.183":
                    validate_detection_receipt(expected)
                verify_native_runtime(
                    {"runtime_qualification": runtime["qualification"]}
                )
        if segmentation:
            from managed_segmentation import verify_runtime

            validate_segmentation_receipt(expected)
            verify_runtime(ultralytics.__version__)
            if (
                not names
                or len(set(names)) != len(names)
                or any(not isinstance(name, str) or not name for name in names)
            ):
                raise ValueError("Unqualified segmentation architecture or classes")
        if segmentation or qualified_native:
            import torch

            tensors = list(model.model.state_dict().values())
            if not tensors or any(
                not torch.isfinite(tensor).all().item() for tensor in tensors
            ):
                raise ValueError("Native checkpoint has missing or non-finite state")
        return model

    async def install(self, body: InstallRequest):
        version_id = str(body.model_version_id)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        expected = body.model_dump(
            mode="json", exclude={"download_url", "size_bytes"}, exclude_none=True
        )
        async with self._process_lock("versions", version_id):
            directory = self.root / version_id
            raw_receipt = None
            if directory.exists() or directory.is_symlink():
                raw_receipt = self._matching_receipt(directory, expected)
                try:
                    receipt = await asyncio.to_thread(self.receipt, version_id)
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                else:
                    return receipt
            with tempfile.TemporaryDirectory(prefix=".install-", dir=self.root) as temp:
                staging = Path(temp) / version_id
                staging.mkdir()
                path = staging / "weights.pt"
                await self._materialize_blob(body, path, expected)
                with (staging / "receipt.json").open("wb") as stream:
                    stream.write(
                        raw_receipt
                        if raw_receipt is not None
                        else json.dumps(
                            expected, ensure_ascii=False, sort_keys=True
                        ).encode()
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                damaged = Path(temp) / "damaged"
                if directory.exists():
                    os.replace(directory, damaged)
                try:
                    os.replace(staging, directory)
                except Exception:
                    if damaged.exists():
                        os.replace(damaged, directory)
                    raise
                self._sync_directory(self.root)
            return expected
