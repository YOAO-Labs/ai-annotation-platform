"""Authenticated, immutable installation of platform-produced detection weights."""

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
    framework: Literal["ultralytics"]
    framework_version: str = Field(min_length=1, max_length=50)
    task: Literal["detection"]
    size_bytes: int = Field(gt=0, le=2 * 1024**3)
    download_url: str = Field(max_length=10000)

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
        return self


def authenticate(request: Request):
    expected = os.environ.get("YOLO_MANAGED_MODELS_TOKEN", "")
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
        import ultralytics
        from ultralytics import YOLO

        if ultralytics.__version__ != expected["framework_version"]:
            raise ValueError("Unverified training/inference framework combination")
        model = YOLO(str(path))
        model.to("cpu")
        if model.task != "detect" or not isinstance(model.names, dict):
            raise ValueError("Expected a native detection checkpoint")
        names = [model.names[i] for i in range(len(model.names))]
        if names != expected["classes"] or digest(names) != expected["classes_digest"]:
            raise ValueError(
                "Checkpoint native classes differ from the registered order"
            )
        return model

    async def install(self, body: InstallRequest):
        version_id = str(body.model_version_id)
        self.root.mkdir(parents=True, exist_ok=True)
        expected = body.model_dump(mode="json", exclude={"download_url", "size_bytes"})
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
