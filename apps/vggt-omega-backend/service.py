"""Bounded transfers, CPU artifact cache and cancellation-safe model borrowing."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from aap_backend_runtime import ManagedLruPool
from PIL import Image, UnidentifiedImageError

from config import (
    CAMERA_COUNT,
    MAX_IMAGE_BYTES,
    OUTPUT_HEIGHT,
    OUTPUT_WIDTH,
    PROFILE,
    SOURCE_COMMIT,
    WEIGHTS_SHA256,
    Config,
)
from predictor import PredictionArrays, preprocessing_for
from schemas import (
    ArrayDescriptor,
    CameraOutput,
    Producer,
    ReconstructRequest,
    ReconstructResponse,
    Timings,
)


class ReconstructionError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class CachedArtifacts:
    fingerprint: str
    cameras: tuple[CameraOutput, ...]
    payloads: tuple[bytes, ...]
    inference_ms: int
    expires_at: float

    @property
    def byte_size(self) -> int:
        return sum(len(payload) for payload in self.payloads)


def request_fingerprint(request: ReconstructRequest) -> str:
    """Bind immutable inputs, excluding expiring transfer URLs and request IDs."""
    frozen = {
        "profile": request.profile,
        "input_digest": request.input_digest,
        "cameras": [camera.model_dump(exclude={"image_url"}) for camera in request.cameras],
    }
    return hashlib.sha256(
        json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def encode_array(array: np.ndarray) -> tuple[bytes, ArrayDescriptor]:
    if array.shape != (OUTPUT_HEIGHT, OUTPUT_WIDTH) or not np.isfinite(array).all():
        raise ReconstructionError(503, "invalid_model_output", "model returned an invalid array")
    stream = io.BytesIO()
    np.save(stream, np.asarray(array, dtype="<f4"), allow_pickle=False)
    payload = stream.getvalue()
    return payload, ArrayDescriptor(
        sha256=hashlib.sha256(payload).hexdigest(), byte_size=len(payload), shape=array.shape
    )


async def executor_to_completion(operation: Any, function: Any, *args: Any) -> Any:
    """Keep the pool borrower and temporary source files until the thread exits."""
    future = asyncio.get_running_loop().run_in_executor(None, function, *args)
    operation.track_future(future)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    try:
        result = future.result()
    except BaseException as exc:
        if cancelled:
            raise asyncio.CancelledError from exc
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


class ReconstructionService:
    def __init__(
        self,
        pool: ManagedLruPool,
        config: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.pool = pool
        self.config = config
        self.transport = transport
        self.lock = asyncio.Lock()
        self.cache: OrderedDict[str, CachedArtifacts] = OrderedDict()

    def clear_cache(self) -> None:
        self.cache.clear()

    def _cache_get(self, key: str, fingerprint: str) -> CachedArtifacts | None:
        now = time.monotonic()
        for expired in [key for key, value in self.cache.items() if value.expires_at <= now]:
            self.cache.pop(expired)
        cached = self.cache.get(key)
        if cached is not None:
            if cached.fingerprint != fingerprint:
                raise ReconstructionError(
                    409,
                    "idempotency_conflict",
                    "idempotency key refers to different immutable inputs",
                )
            self.cache.move_to_end(key)
        return cached

    def _cache_put(self, key: str, cached: CachedArtifacts) -> None:
        if not self.config.cache_entries or cached.byte_size > self.config.cache_bytes:
            return
        self.cache[key] = cached
        while (
            len(self.cache) > self.config.cache_entries
            or sum(value.byte_size for value in self.cache.values()) > self.config.cache_bytes
        ):
            self.cache.popitem(last=False)

    async def _download(self, client: httpx.AsyncClient, camera: Any, path: Path) -> None:
        size = 0
        digest = hashlib.sha256()
        try:
            async with client.stream("GET", camera.image_url) as response:
                if response.status_code != 200:
                    raise ReconstructionError(
                        502, "source_transfer_failed", "source image download failed"
                    )
                with path.open("wb") as stream:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_IMAGE_BYTES:
                            raise ReconstructionError(
                                413, "source_too_large", "source image exceeds the profile limit"
                            )
                        digest.update(chunk)
                        stream.write(chunk)
        except httpx.HTTPError as exc:
            raise ReconstructionError(
                502, "source_transfer_failed", "source image download failed"
            ) from exc
        if digest.hexdigest() != camera.sha256:
            raise ReconstructionError(
                422, "source_hash_mismatch", "source image digest does not match frozen input"
            )
        try:
            with Image.open(path) as image:
                if image.size != (camera.width, camera.height):
                    raise ReconstructionError(
                        422,
                        "source_dimensions_mismatch",
                        "source image dimensions do not match frozen input",
                    )
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ReconstructionError(
                422, "invalid_source_image", "source image cannot be decoded"
            ) from exc

    async def reconstruct(self, request: ReconstructRequest, operation: Any) -> ReconstructResponse:
        # An additional request receives a bounded retry response instead of an
        # invisible second queue. Pool ownership also serializes warmup/inference.
        if self.lock.locked():
            raise ReconstructionError(503, "producer_busy", "one reconstruction is already active")
        async with self.lock:
            started = time.monotonic()
            fingerprint = request_fingerprint(request)
            cached = self._cache_get(request.idempotency_key, fingerprint)
            cache_hit = cached is not None
            model_load_ms = None
            async with httpx.AsyncClient(
                timeout=self.config.transfer_timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
                limits=httpx.Limits(max_connections=1),
            ) as client:
                if cached is None:
                    try:
                        transforms = [
                            preprocessing_for(camera.width, camera.height)
                            for camera in request.cameras
                        ]
                    except ValueError as exc:
                        raise ReconstructionError(
                            422, "unsupported_image_layout", str(exc)
                        ) from exc
                    with tempfile.TemporaryDirectory(prefix="vggt-omega-") as directory:
                        paths = [
                            Path(directory) / f"{index}.image" for index in range(CAMERA_COUNT)
                        ]
                        for camera, path in zip(request.cameras, paths, strict=True):
                            await self._download(client, camera, path)
                        async with self.pool.borrow(PROFILE) as lease:
                            model_load_ms = lease.load_ms
                            prediction: PredictionArrays = await executor_to_completion(
                                operation, lease.resource.predict, paths
                            )
                        if (
                            prediction.depth.shape != (CAMERA_COUNT, OUTPUT_HEIGHT, OUTPUT_WIDTH)
                            or prediction.confidence.shape != prediction.depth.shape
                        ):
                            raise ReconstructionError(
                                503, "invalid_model_output", "model returned an invalid batch shape"
                            )
                        payloads: list[bytes] = []
                        cameras: list[CameraOutput] = []
                        for index, camera in enumerate(request.cameras):
                            depth, depth_descriptor = encode_array(prediction.depth[index])
                            confidence, confidence_descriptor = encode_array(
                                prediction.confidence[index]
                            )
                            payloads.extend((depth, confidence))
                            cameras.append(
                                CameraOutput(
                                    role=camera.role,
                                    source_sha256=camera.sha256,
                                    source_width=camera.width,
                                    source_height=camera.height,
                                    depth=depth_descriptor,
                                    confidence=confidence_descriptor,
                                    preprocessing=transforms[index],
                                )
                            )
                        cached = CachedArtifacts(
                            fingerprint,
                            tuple(cameras),
                            tuple(payloads),
                            prediction.inference_ms,
                            time.monotonic() + self.config.cache_ttl,
                        )
                        # Retain only bounded CPU bytes so a failed PUT can retry
                        # fresh staging URLs without retaining CUDA tensors or URLs.
                        self._cache_put(request.idempotency_key, cached)
                for index, target in enumerate(request.upload_targets):
                    for offset, destination in enumerate((target.depth, target.confidence)):
                        try:
                            response = await client.put(
                                destination.url,
                                content=cached.payloads[index * 2 + offset],
                                headers=destination.headers,
                            )
                            if response.status_code not in {200, 201, 204}:
                                raise ReconstructionError(
                                    502,
                                    "artifact_transfer_failed",
                                    "staging artifact upload failed",
                                )
                        except httpx.HTTPError as exc:
                            raise ReconstructionError(
                                502, "artifact_transfer_failed", "staging artifact upload failed"
                            ) from exc
            return ReconstructResponse(
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                input_digest=request.input_digest,
                profile=request.profile,
                producer=Producer(source_commit=SOURCE_COMMIT, weights_sha256=WEIGHTS_SHA256),
                cameras=list(cached.cameras),
                timings=Timings(
                    model_load_ms=model_load_ms,
                    inference_ms=cached.inference_ms,
                    total_ms=int((time.monotonic() - started) * 1000),
                ),
                cache_hit=cache_hit,
            )
