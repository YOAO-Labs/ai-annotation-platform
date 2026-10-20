"""Authenticated, immutable installation of qualified platform-produced weights."""

from __future__ import annotations

import asyncio
import hashlib
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
from training_contract.model_return import LIBRE_FORMAT, LIBRE_RUNTIME, libre_identity
from managed_segmentation import validate_receipt as validate_segmentation_receipt


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class ManagedModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_version_id: uuid.UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classes_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


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
        elif any(value is not None for value in extra):
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


class ManagedModelStore:
    def __init__(self, root: Path):
        self.root = root
        self._locks: dict[str, asyncio.Lock] = {}

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

        if ultralytics.__version__ != expected["framework_version"]:
            raise ValueError("Unverified training/inference framework combination")
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
        if segmentation:
            from managed_segmentation import verify_runtime
            from ultralytics.nn.modules.head import Segment
            import torch

            validate_segmentation_receipt(expected)
            verify_runtime()
            architecture = model.model.yaml
            head = model.model.model[-1]
            if (
                architecture.get("scale") != "n"
                or Path(architecture.get("yaml_file", "")).stem != "yolo11n-seg"
                or type(head) is not Segment
                or head.nc != len(names)
                or not names
                or len(set(names)) != len(names)
                or any(not isinstance(name, str) or not name for name in names)
            ):
                raise ValueError("Unqualified segmentation architecture or classes")
            tensors = list(model.model.state_dict().values())
            if not tensors or any(
                not torch.isfinite(tensor).all().item() for tensor in tensors
            ):
                raise ValueError(
                    "Segmentation checkpoint has missing or non-finite state"
                )
        return model

    async def install(self, body: InstallRequest):
        version_id = str(body.model_version_id)
        self.root.mkdir(parents=True, exist_ok=True)
        expected = body.model_dump(
            mode="json", exclude={"download_url", "size_bytes"}, exclude_none=True
        )
        lock = self._locks.setdefault(version_id, asyncio.Lock())
        async with lock:
            directory = self.root / version_id
            if directory.exists():
                try:
                    receipt = await asyncio.to_thread(self.receipt, version_id)
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                else:
                    if receipt != expected:
                        raise HTTPException(
                            status_code=409,
                            detail="Installed model versions are immutable",
                        )
                    return receipt
            with tempfile.TemporaryDirectory(prefix=".install-", dir=self.root) as temp:
                staging = Path(temp) / version_id
                staging.mkdir()
                path = staging / "weights.pt"
                sha = hashlib.sha256()
                total = 0
                async with httpx.AsyncClient(
                    timeout=120, follow_redirects=False
                ) as client:
                    async with client.stream("GET", body.download_url) as response:
                        response.raise_for_status()
                        if (
                            response.headers.get("Content-Length")
                            and int(response.headers["Content-Length"])
                            != body.size_bytes
                        ):
                            raise HTTPException(
                                status_code=422, detail="Artifact size differs"
                            )
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
                    raise HTTPException(
                        status_code=422, detail="Artifact digest differs"
                    )
                try:
                    await asyncio.to_thread(self.inspect, path, expected)
                except Exception as exc:
                    raise HTTPException(
                        status_code=422,
                        detail="Checkpoint task/classes/runtime validation failed",
                    ) from exc
                with (staging / "receipt.json").open("w") as stream:
                    json.dump(expected, stream, ensure_ascii=False, sort_keys=True)
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
                fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return expected
