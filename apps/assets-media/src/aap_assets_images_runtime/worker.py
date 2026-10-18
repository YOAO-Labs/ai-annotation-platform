"""Lease-fenced image executor and independent polling entry point."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import signal
import stat
import sys
import tempfile
import time
from typing import Any
from uuid import UUID, uuid4


from aap_assets_images.contract import (
    AdvanceImageBackfillRequest,
    Artifact,
    ClaimImageMediaRequest,
    ClaimedImageWork,
    DimensionsResult,
    DimensionsWorkSpec,
    FailImageMediaRequest,
    FinishImageMediaRequest,
    HeartbeatImageMediaRequest,
    ImageBackfillObservations,
    ImageBackfillProgress,
    ImageBackfillSourceObservation,
    ImageBackfillSpec,
    ImageClaim,
    ImageFailure,
    ImageOrphanCandidate,
    ImagePyramidManifest,
    ImageReconcileResult,
    ImageSource,
    ImageTileArtifact,
    ImageWorkOutcome,
    ImageWorkResult,
    ObjectIdentity,
    PyramidResult,
    PyramidWorkSpec,
    ReconcileImageMediaRequest,
    ThumbnailResult,
    ThumbnailWorkSpec,
    json_bytes,
    require_result_scope,
)
from aap_assets_images.geometry import (
    canonical_manifest_bytes,
    expected_tile_dimensions,
)
from aap_assets_images.policy import ImagePolicy
from aap_assets_images_runtime.processing import (
    CHILD_REQUEST_SCHEMA,
    CHILD_RESULT_SCHEMA,
    IMAGE_HEADER_BYTES,
    image_dimensions_from_bytes,
)

from .config import ImageWorkerConfig, load_config
from .database import AsyncpgImageDatabase, ImageDatabase
from .health import check_capabilities
from .storage import DownloadedSource, ImageStorage, S3ImageStorage

_BACKFILL_CALL_LIMIT = 1_000_000
_FINISH_RETRY_LIMIT = 5
_FINISH_RETRY_BASE_SECONDS = 0.05
_FINISH_RETRY_MAX_SECONDS = 0.5
_RECONCILE_BATCH_SIZE = 100
_CHILD_STDOUT_LIMIT = 16 * 1024 * 1024
_CHILD_STDERR_LIMIT = 64 * 1024
_CHILD_WATCH_INTERVAL = 0.25
_CHILD_TERMINATE_GRACE_SECONDS = 2.0
_MAX_UPLOAD_CONCURRENCY = 8
_RECONCILE_INTERVAL_SECONDS = 24 * 60 * 60
_LOGGER = logging.getLogger(__name__)

_CHILD_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_response",
        "not_eligible",
        "source_missing",
        "source_changed",
        "unsupported_dimensions",
        "resource_limit",
        "timeout",
        "color_profile_unavailable",
        "multi_page_unsupported",
        "dimension_mismatch",
        "generator_unavailable",
        "decode_failed",
        "encode_failed",
        "tile_grid_mismatch",
    }
)
_TERMINAL_FAILURES = frozenset(
    {
        "invalid_request",
        "invalid_response",
        "source_missing",
        "source_changed",
        "source_not_ready",
        "not_image",
        "not_eligible",
        "resource_budget_exceeded",
        "unsupported_dimensions",
        "resource_limit",
        "timeout",
        "color_profile_unavailable",
        "multi_page_unsupported",
        "dimension_mismatch",
        "generator_unavailable",
        "decode_failed",
        "encode_failed",
        "tile_grid_mismatch",
        "upload_verification_failed",
    }
)


class _LeaseHeartbeat:
    def __init__(
        self,
        database: ImageDatabase,
        claim: ImageClaim,
        *,
        lease_seconds: int,
        deadline: float,
    ) -> None:
        self._database = database
        self._claim = claim
        self.lease_seconds = lease_seconds
        self.deadline = deadline
        self._stopped = asyncio.Event()
        self._lost = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            result = await self._database.call(
                "heartbeat_image_media",
                HeartbeatImageMediaRequest(
                    job_id=self._claim.job_id,
                    generation=self._claim.generation,
                    lease_token=self._claim.lease_token,
                    lease_seconds=self.lease_seconds,
                ),
            )
        except asyncio.CancelledError:
            self._lost.set()
            raise
        except Exception:
            self._lost.set()
            raise ImageFailure("lease_lost") from None
        if not result.active:
            self._lost.set()
            raise ImageFailure("lease_lost")
        self._task = asyncio.create_task(self._run())

    def check(self) -> None:
        self.check_lease()
        if time.monotonic() >= self.deadline:
            raise ImageFailure("timeout")

    def check_lease(self) -> None:
        if self._lost.is_set():
            raise ImageFailure("lease_lost")

    async def _run(self) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=interval)
                    return
                except TimeoutError:
                    pass
                result = await self._database.call(
                    "heartbeat_image_media",
                    HeartbeatImageMediaRequest(
                        job_id=self._claim.job_id,
                        generation=self._claim.generation,
                        lease_token=self._claim.lease_token,
                        lease_seconds=self.lease_seconds,
                    ),
                )
                if not result.active:
                    self._lost.set()
                    return
        except asyncio.CancelledError:
            raise
        except BaseException:
            self._lost.set()

    async def close(self) -> None:
        self._stopped.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class ImageExecutor:
    """Execute accepted image work with no database or owner lock over S3 I/O."""

    def __init__(
        self,
        database: ImageDatabase,
        storage: ImageStorage,
        *,
        srgb_profile: str | Path = "/usr/share/color/icc/sRGB.icc",
        worker_id: UUID | None = None,
    ) -> None:
        self.database = database
        self.storage = storage
        self.srgb_profile = Path(srgb_profile)
        self.worker_id = worker_id or uuid4()
        self._closed = False

    async def execute_once(
        self, *, job_id: UUID | None = None
    ) -> ImageWorkOutcome | None:
        if self._closed:
            raise RuntimeError("image executor is closed")
        default_policy = ImagePolicy()
        claimed = await self.database.call(
            "claim_image_media",
            ClaimImageMediaRequest(
                worker_id=self.worker_id,
                job_id=job_id,
                lease_seconds=default_policy.lease_seconds,
                kinds=["dimensions", "thumbnail", "pyramid", "image_backfill"],
            ),
        )
        if not isinstance(claimed, ClaimedImageWork):
            raise ImageFailure("invalid_response")
        claim = claimed.claim
        if claim is None:
            return None
        policy = claim.spec.policy
        heartbeat = _LeaseHeartbeat(
            self.database,
            claim,
            lease_seconds=policy.lease_seconds,
            deadline=time.monotonic() + policy.job_timeout_seconds,
        )
        await heartbeat.start()
        try:
            if isinstance(claim.spec, ImageBackfillSpec):
                return await self._advance_backfill(claim, heartbeat)
            result = await self._execute_source_claim(claim, heartbeat)
            heartbeat.check()
            try:
                require_result_scope(claim, result)
                finish = FinishImageMediaRequest(
                    job_id=claim.job_id,
                    generation=claim.generation,
                    lease_token=claim.lease_token,
                    result=result,
                )
                _check_finish_request_budget(finish)
            except (TypeError, ValueError):
                raise ImageFailure("invalid_response") from None
            outcome = await self._finish_with_retries(finish, heartbeat)
            if not isinstance(outcome, ImageWorkOutcome):
                raise ImageFailure("invalid_response")
            return outcome
        except ImageFailure as exc:
            code = "source_missing" if exc.code == "object_missing" else exc.code
            if code not in _TERMINAL_FAILURES:
                raise
            heartbeat.check_lease()
            return await self._fail_claim(claim, code)
        finally:
            await heartbeat.close()

    async def _finish_with_retries(
        self,
        request: FinishImageMediaRequest,
        heartbeat: _LeaseHeartbeat,
    ) -> ImageWorkOutcome:
        last_contention: ImageFailure | None = None
        for attempt in range(_FINISH_RETRY_LIMIT):
            try:
                heartbeat.check()
            except ImageFailure as exc:
                if exc.code == "timeout" and last_contention is not None:
                    raise last_contention
                raise
            try:
                result = await self.database.call("finish_image_media", request)
            except ImageFailure as exc:
                if exc.code not in {"source_busy", "operation_busy"}:
                    raise
                last_contention = exc
                if attempt + 1 == _FINISH_RETRY_LIMIT:
                    raise
                base = min(
                    _FINISH_RETRY_BASE_SECONDS * (2**attempt),
                    _FINISH_RETRY_MAX_SECONDS,
                )
                delay = base + random.uniform(0, base)
                if time.monotonic() + delay >= heartbeat.deadline:
                    raise
                await asyncio.sleep(delay)
                continue
            if not isinstance(result, ImageWorkOutcome):
                raise ImageFailure("invalid_response")
            return result
        raise ImageFailure("operation_busy")

    async def _execute_source_claim(
        self, claim: ImageClaim, heartbeat: _LeaseHeartbeat
    ) -> ImageWorkResult:
        source = claim.source
        if source is None or source.object_identity is None:
            raise ImageFailure("invalid_response")
        expected_snapshot = getattr(claim.spec, "expected_source_snapshot", None)
        if expected_snapshot is None or expected_snapshot != source.source_snapshot:
            raise ImageFailure("source_changed")
        if source.source_drifted:
            raise ImageFailure("source_changed")
        if not source.upload_ready:
            raise ImageFailure("source_not_ready")
        heartbeat.check()

        if isinstance(claim.spec, DimensionsWorkSpec):
            header = await self.storage.read_source_header(source)
            heartbeat.check()
            if header.identity != source.object_identity:
                raise ImageFailure("source_changed")
            dimensions = image_dimensions_from_bytes(header.data[:IMAGE_HEADER_BYTES])
            if dimensions is not None:
                self._validate_known_dimensions(source, dimensions)
                if await self.storage.head_source(source) != source.object_identity:
                    raise ImageFailure("source_changed")
                return DimensionsResult(
                    source_identity=header.identity,
                    width=dimensions[0],
                    height=dimensions[1],
                )
            self._validate_metadata_probe_size(claim, source.object_identity)
            with tempfile.TemporaryDirectory(
                prefix="aap-image-dimensions-"
            ) as temporary:
                workspace = Path(temporary)
                workspace.chmod(0o700)
                source_path = workspace / _source_filename(source.file_name)
                download = await self._download_source(claim, source_path, heartbeat)
                output_dir = workspace / "output"
                output_dir.mkdir(mode=0o700)
                dimensions = await self._probe_full_dimensions(
                    claim, source_path, output_dir, download.sha256, heartbeat
                )
                self._validate_known_dimensions(source, dimensions)
                heartbeat.check()
                if await self.storage.head_source(source) != source.object_identity:
                    raise ImageFailure("source_changed")
                return DimensionsResult(
                    source_identity=source.object_identity,
                    source_sha256=download.sha256,
                    width=dimensions[0],
                    height=dimensions[1],
                )

        if isinstance(claim.spec, ThumbnailWorkSpec):
            source_byte_limit = min(
                claim.spec.policy.thumbnail_max_source_bytes,
                claim.spec.policy.max_source_bytes,
            )
        elif isinstance(claim.spec, PyramidWorkSpec):
            source_byte_limit = claim.spec.policy.max_source_bytes
        else:
            raise ImageFailure("invalid_response")
        identity = source.object_identity
        if identity.byte_size <= 0:
            raise ImageFailure("source_missing")
        if (
            identity.byte_size > source_byte_limit
            or identity.byte_size > claim.spec.policy.max_temp_bytes
        ):
            raise ImageFailure("resource_limit")

        thumbnail_dimensions: tuple[int, int] | None = None
        if isinstance(claim.spec, ThumbnailWorkSpec):
            header = await self.storage.read_source_header(source)
            heartbeat.check()
            if header.identity != identity:
                raise ImageFailure("source_changed")
            thumbnail_dimensions = image_dimensions_from_bytes(
                header.data[:IMAGE_HEADER_BYTES]
            )
            if thumbnail_dimensions is not None:
                self._validate_thumbnail_dimensions(claim, thumbnail_dimensions)
                self._validate_known_dimensions(source, thumbnail_dimensions)

        with tempfile.TemporaryDirectory(prefix="aap-image-job-") as temporary:
            workspace = Path(temporary)
            workspace.chmod(0o700)
            source_path = workspace / _source_filename(source.file_name)
            download = await self._download_source(claim, source_path, heartbeat)
            heartbeat.check()
            output_dir = workspace / "output"
            output_dir.mkdir(mode=0o700)
            if isinstance(claim.spec, ThumbnailWorkSpec):
                if thumbnail_dimensions is None:
                    thumbnail_dimensions = await self._probe_full_dimensions(
                        claim, source_path, output_dir, download.sha256, heartbeat
                    )
                    self._validate_thumbnail_dimensions(claim, thumbnail_dimensions)
                    self._validate_known_dimensions(source, thumbnail_dimensions)
                result = await self._thumbnail(
                    claim,
                    source_path,
                    output_dir,
                    download.sha256,
                    thumbnail_dimensions,
                    heartbeat,
                )
            else:
                result = await self._pyramid(
                    claim,
                    source_path,
                    output_dir,
                    download.sha256,
                    heartbeat,
                )
            heartbeat.check()
            if (
                _directory_size(workspace, claim.spec.policy.max_temp_bytes)
                > claim.spec.policy.max_temp_bytes
            ):
                raise ImageFailure("resource_limit")
            await self._upload_result_artifacts(claim, result, output_dir, heartbeat)
            heartbeat.check()
            if await self.storage.head_source(source) != identity:
                raise ImageFailure("source_changed")
            return result

    async def _download_source(
        self, claim: ImageClaim, destination: Path, heartbeat: _LeaseHeartbeat
    ) -> DownloadedSource:
        source = claim.source
        if source is None or source.object_identity is None:
            raise ImageFailure("invalid_response")
        downloaded = await self.storage.download_source(source, destination)
        heartbeat.check()
        if (
            downloaded.identity != source.object_identity
            or downloaded.byte_size != source.object_identity.byte_size
            or source.file_size is not None
            and downloaded.byte_size != source.file_size
            or not _is_sha256(downloaded.sha256)
        ):
            raise ImageFailure("source_changed")
        return downloaded

    def _validate_metadata_probe_size(
        self, claim: ImageClaim, identity: ObjectIdentity
    ) -> None:
        limit = min(
            claim.spec.policy.thumbnail_max_source_bytes,
            claim.spec.policy.max_source_bytes,
            claim.spec.policy.max_temp_bytes,
        )
        if identity.byte_size <= 0:
            raise ImageFailure("source_missing")
        if identity.byte_size > limit:
            raise ImageFailure("resource_limit")

    @staticmethod
    def _validate_known_dimensions(
        source: ImageSource, dimensions: tuple[int, int]
    ) -> None:
        if any(value <= 0 or value > 2_147_483_647 for value in dimensions):
            raise ImageFailure("unsupported_dimensions")
        known = _known_dimensions(source)
        if known is not None and tuple(known) != dimensions:
            raise ImageFailure("dimension_mismatch")

    @staticmethod
    def _validate_thumbnail_dimensions(
        claim: ImageClaim, dimensions: tuple[int, int]
    ) -> None:
        policy = claim.spec.policy
        width, height = dimensions
        if width > policy.max_dimension or height > policy.max_dimension:
            raise ImageFailure("unsupported_dimensions")
        if width * height > policy.max_pixels:
            raise ImageFailure("unsupported_dimensions")
        if policy.pyramid_eligible(width, height):
            raise ImageFailure("not_eligible")

    async def _probe_full_dimensions(
        self,
        claim: ImageClaim,
        source_path: Path,
        output_dir: Path,
        source_sha256: str,
        heartbeat: _LeaseHeartbeat,
    ) -> tuple[int, int]:
        assert claim.source is not None
        body = await _run_child(
            {
                "schema": CHILD_REQUEST_SCHEMA,
                "action": "dimensions",
                "source_path": str(source_path),
                "source_sha256": source_sha256,
                "expected_dimensions": _known_dimensions(claim.source),
                "policy": claim.spec.policy.model_dump(mode="json"),
                "deadline": heartbeat.deadline,
            },
            workspace=source_path.parent,
            output_dir=output_dir,
            policy=claim.spec.policy,
            heartbeat=heartbeat,
        )
        if (
            set(body)
            != {
                "schema",
                "action",
                "width",
                "height",
                "source_bytes",
                "source_sha256",
            }
            or body.get("schema") != CHILD_RESULT_SCHEMA
            or body.get("action") != "dimensions"
        ):
            raise ImageFailure("invalid_response")
        width, height = _positive_int(body["width"]), _positive_int(body["height"])
        if (
            type(body["source_bytes"]) is not int
            or body["source_bytes"] != claim.source.object_identity.byte_size
            or body["source_sha256"] != source_sha256
        ):
            raise ImageFailure("invalid_response")
        return width, height

    async def _thumbnail(
        self,
        claim: ImageClaim,
        source_path: Path,
        output_dir: Path,
        source_sha256: str,
        expected_dimensions: tuple[int, int] | None,
        heartbeat: _LeaseHeartbeat,
    ) -> ThumbnailResult:
        assert isinstance(claim.spec, ThumbnailWorkSpec)
        assert claim.source is not None and claim.output_prefix is not None
        if expected_dimensions is None:
            raise ImageFailure("invalid_response")
        output_path = output_dir / "thumbnail.webp"
        body = await _run_child(
            {
                "schema": CHILD_REQUEST_SCHEMA,
                "action": "thumbnail",
                "source_path": str(source_path),
                "source_sha256": source_sha256,
                "expected_dimensions": list(expected_dimensions),
                "policy": claim.spec.policy.model_dump(mode="json"),
                "deadline": heartbeat.deadline,
                "output_path": str(output_path),
            },
            workspace=source_path.parent,
            output_dir=output_dir,
            policy=claim.spec.policy,
            heartbeat=heartbeat,
        )
        expected_keys = {
            "schema",
            "action",
            "width",
            "height",
            "blurhash",
            "source_bytes",
            "source_sha256",
            "output_path",
            "output_bytes",
            "output_sha256",
        }
        if (
            set(body) != expected_keys
            or body.get("schema") != CHILD_RESULT_SCHEMA
            or body.get("action") != "thumbnail"
        ):
            raise ImageFailure("invalid_response")
        width = _positive_int(body["width"])
        height = _positive_int(body["height"])
        if expected_dimensions != (width, height):
            raise ImageFailure("dimension_mismatch")
        if (
            type(body["source_bytes"]) is not int
            or body["source_bytes"] != claim.source.object_identity.byte_size
            or body["source_sha256"] != source_sha256
            or body["output_path"] != str(output_path)
            or type(body["output_bytes"]) is not int
            or not isinstance(body["blurhash"], str)
            or not body["blurhash"]
            or len(body["blurhash"]) > 128
        ):
            raise ImageFailure("invalid_response")
        size = _regular_file_size(output_path)
        digest = body["output_sha256"]
        if (
            size != body["output_bytes"]
            or not _is_sha256(digest)
            or body["source_bytes"] + size > claim.spec.policy.max_temp_bytes
        ):
            raise ImageFailure("resource_limit")
        result = ThumbnailResult(
            source_identity=claim.source.object_identity,
            source_sha256=source_sha256,
            width=width,
            height=height,
            blurhash=body["blurhash"],
            artifact=Artifact(
                key=claim.output_prefix + "thumbnail.webp",
                byte_size=size,
                sha256=digest,
            ),
        )
        _check_finish_request_budget(
            FinishImageMediaRequest(
                job_id=claim.job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                result=result,
            )
        )
        return result

    async def _pyramid(
        self,
        claim: ImageClaim,
        source_path: Path,
        output_dir: Path,
        source_sha256: str,
        heartbeat: _LeaseHeartbeat,
    ) -> PyramidResult:
        assert isinstance(claim.spec, PyramidWorkSpec)
        assert claim.source is not None
        assert claim.output_prefix is not None
        assert claim.pyramid_generation is not None
        if not self.srgb_profile.is_file():
            raise ImageFailure("color_profile_unavailable")
        body = await _run_child(
            {
                "schema": CHILD_REQUEST_SCHEMA,
                "action": "pyramid",
                "source_path": str(source_path),
                "source_sha256": source_sha256,
                "expected_dimensions": _known_dimensions(claim.source),
                "policy": claim.spec.policy.model_dump(mode="json"),
                "deadline": heartbeat.deadline,
                "output_dir": str(output_dir),
                "generation": claim.pyramid_generation,
                "srgb_profile": str(self.srgb_profile),
            },
            workspace=source_path.parent,
            output_dir=output_dir,
            policy=claim.spec.policy,
            heartbeat=heartbeat,
        )
        expected_keys = {
            "schema",
            "action",
            "source_fingerprint",
            "width",
            "height",
            "max_level",
            "overview_path",
            "overview_width",
            "overview_height",
            "overview_digest",
            "manifest_path",
            "manifest_digest",
            "tile_count",
            "tiles",
            "retained_bytes",
            "temp_bytes",
        }
        if (
            set(body) != expected_keys
            or body.get("schema") != CHILD_RESULT_SCHEMA
            or body.get("action") != "pyramid"
        ):
            raise ImageFailure("invalid_response")
        if body["source_fingerprint"] != "sha256:" + source_sha256:
            raise ImageFailure("source_changed")
        manifest_path = output_dir / "manifest.json"
        overview_path = output_dir / "overview.webp"
        if body["manifest_path"] != str(manifest_path) or body["overview_path"] != str(
            overview_path
        ):
            raise ImageFailure("invalid_response")
        manifest_bytes = _read_regular_file(
            manifest_path, claim.spec.policy.max_derived_bytes
        )
        raw_manifest = _load_json_unique(manifest_bytes)
        try:
            manifest = ImagePyramidManifest.model_validate(raw_manifest)
        except Exception:
            raise ImageFailure("invalid_response") from None
        if (
            canonical_manifest_bytes(manifest.model_dump(mode="json", by_alias=True))
            != manifest_bytes
            or manifest.generation != claim.pyramid_generation
            or manifest.source_fingerprint != "sha256:" + source_sha256
            or manifest.width != _positive_int(body["width"])
            or manifest.height != _positive_int(body["height"])
            or manifest.overview.width != _positive_int(body["overview_width"])
            or manifest.overview.height != _positive_int(body["overview_height"])
            or type(body["max_level"]) is not int
            or body["max_level"] != len(manifest.levels) - 1
            or body["overview_digest"] != manifest.overview.content_digest
        ):
            raise ImageFailure("invalid_response")

        manifest_size = len(manifest_bytes)
        overview_size = _regular_file_size(overview_path)
        if not _is_sha256_prefixed(manifest.overview.content_digest):
            raise ImageFailure("invalid_response")
        if not _is_sha256_prefixed(body["manifest_digest"]):
            raise ImageFailure("invalid_response")
        if (
            body["manifest_digest"]
            != "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        ):
            raise ImageFailure("invalid_response")

        local_tiles = _validate_child_tiles(body["tiles"], output_dir, manifest)
        expected_tile_count = sum(
            level.columns * level.rows for level in manifest.levels
        )
        if (
            type(body["tile_count"]) is not int
            or body["tile_count"] != expected_tile_count
            or len(local_tiles) != expected_tile_count
        ):
            raise ImageFailure("tile_grid_mismatch")
        retained_bytes = (
            manifest_size
            + overview_size
            + sum(tile["byte_size"] for tile in local_tiles)
        )
        actual_temp_bytes = _directory_size(
            source_path.parent, claim.spec.policy.max_temp_bytes
        )
        if (
            type(body["retained_bytes"]) is not int
            or body["retained_bytes"] != retained_bytes
            or retained_bytes > claim.spec.policy.max_derived_bytes
            or type(body["temp_bytes"]) is not int
            or body["temp_bytes"] != actual_temp_bytes
            or actual_temp_bytes > claim.spec.policy.max_temp_bytes
        ):
            raise ImageFailure("resource_limit")

        prefix = claim.output_prefix
        manifest_artifact = Artifact(
            key=prefix + "manifest.json",
            byte_size=manifest_size,
            sha256=body["manifest_digest"][7:],
        )
        overview_artifact = Artifact(
            key=prefix + "overview.webp",
            byte_size=overview_size,
            sha256=manifest.overview.content_digest[7:],
        )
        tile_artifacts = [
            ImageTileArtifact(
                level=tile["level"],
                x=tile["x"],
                y=tile["y"],
                artifact=Artifact(
                    key=prefix + f"tiles/{tile['level']}/{tile['x']}/{tile['y']}.webp",
                    byte_size=tile["byte_size"],
                    sha256="0" * 64,
                ),
            )
            for tile in local_tiles
        ]
        estimate = PyramidResult(
            source_identity=claim.source.object_identity,
            source_sha256=source_sha256,
            manifest=manifest,
            manifest_artifact=manifest_artifact,
            overview_artifact=overview_artifact,
            tile_artifacts=tile_artifacts,
            tile_count=expected_tile_count,
            retained_bytes=retained_bytes,
        )
        _check_finish_request_budget(
            FinishImageMediaRequest(
                job_id=claim.job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                result=estimate,
            )
        )

        uploaded_tiles: list[ImageTileArtifact] = []
        upload_concurrency = min(
            claim.spec.policy.vips_concurrency, _MAX_UPLOAD_CONCURRENCY
        )
        for start in range(0, len(local_tiles), upload_concurrency):
            heartbeat.check()
            batch = local_tiles[start : start + upload_concurrency]

            async def upload_tile(tile: dict[str, Any]) -> ImageTileArtifact:
                key = prefix + f"tiles/{tile['level']}/{tile['x']}/{tile['y']}.webp"
                artifact = await self.storage.upload_cache(tile["path"], key)
                if artifact.key != key or artifact.byte_size != tile["byte_size"]:
                    raise ImageFailure("upload_verification_failed")
                return ImageTileArtifact(
                    level=tile["level"],
                    x=tile["x"],
                    y=tile["y"],
                    artifact=artifact,
                )

            rows = await asyncio.gather(
                *(upload_tile(tile) for tile in batch), return_exceptions=True
            )
            failure = next(
                (row for row in rows if isinstance(row, BaseException)), None
            )
            if failure is not None:
                if isinstance(failure, ImageFailure):
                    raise failure
                raise ImageFailure("storage_unavailable") from None
            uploaded_tiles.extend(
                row for row in rows if isinstance(row, ImageTileArtifact)
            )
            if len(uploaded_tiles) != start + len(batch):
                raise ImageFailure("invalid_response")
            heartbeat.check()

        uploaded_manifest = await self.storage.upload_cache(
            manifest_path, prefix + "manifest.json"
        )
        if uploaded_manifest != manifest_artifact:
            raise ImageFailure("upload_verification_failed")
        heartbeat.check()
        uploaded_overview = await self.storage.upload_cache(
            overview_path, prefix + "overview.webp"
        )
        if uploaded_overview != overview_artifact:
            raise ImageFailure("upload_verification_failed")
        return PyramidResult(
            source_identity=claim.source.object_identity,
            source_sha256=source_sha256,
            manifest=manifest,
            manifest_artifact=uploaded_manifest,
            overview_artifact=uploaded_overview,
            tile_artifacts=uploaded_tiles,
            tile_count=expected_tile_count,
            retained_bytes=retained_bytes,
        )

    async def _upload_result_artifacts(
        self,
        claim: ImageClaim,
        result: ImageWorkResult,
        output_dir: Path,
        heartbeat: _LeaseHeartbeat,
    ) -> None:
        if not isinstance(result, ThumbnailResult):
            return
        assert claim.output_prefix is not None
        output_path = output_dir / "thumbnail.webp"
        heartbeat.check()
        observed = await self.storage.upload_cache(
            output_path, claim.output_prefix + "thumbnail.webp"
        )
        if observed != result.artifact:
            raise ImageFailure("upload_verification_failed")

    async def _advance_backfill(
        self, claim: ImageClaim, heartbeat: _LeaseHeartbeat
    ) -> ImageWorkOutcome:
        # The native call first prepares a bounded candidate batch without moving
        # its cursor. S3 observations then happen without transaction-held locks.
        previous_cursor: tuple[str, int] | None = None
        for _ in range(_BACKFILL_CALL_LIMIT):
            heartbeat.check()
            request = AdvanceImageBackfillRequest(
                job_id=claim.job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                batch_size=500,
            )
            progress = await self.database.call("advance_image_backfill", request)
            if not isinstance(progress, ImageBackfillProgress):
                raise ImageFailure("invalid_response")
            heartbeat.check()
            if progress.needs_observation:
                if progress.batch_token is None or not progress.candidates:
                    raise ImageFailure("invalid_response")
                self._validate_backfill_candidates(
                    claim,
                    progress.candidates,
                    previous_cursor=previous_cursor,
                )
                observations = await self._observe_backfill_candidates(
                    progress.candidates,
                    progress.batch_token,
                    heartbeat,
                )
                request = request.model_copy(update={"observations": observations})
                progress = await self.database.call("advance_image_backfill", request)
                if not isinstance(progress, ImageBackfillProgress):
                    raise ImageFailure("invalid_response")
                heartbeat.check()
            if progress.complete:
                return ImageWorkOutcome(
                    job_id=claim.job_id,
                    generation=claim.generation,
                    outcome="published",
                )
            if (
                progress.next_owner_id is None
                or progress.next_owner_kind != claim.spec.selection.owner_kind
            ):
                raise ImageFailure("invalid_response")
            cursor = (progress.next_owner_kind, progress.next_owner_id.int)
            if previous_cursor is not None and cursor <= previous_cursor:
                raise ImageFailure("invalid_response")
            previous_cursor = cursor
        raise ImageFailure("resource_budget_exceeded")

    @staticmethod
    def _validate_backfill_candidates(
        claim: ImageClaim,
        candidates: list[ImageSource],
        *,
        previous_cursor: tuple[str, int] | None,
    ) -> None:
        if not isinstance(claim.spec, ImageBackfillSpec):
            raise ImageFailure("invalid_response")
        selection = claim.spec.selection
        keys = [(source.owner.kind, source.owner.id.int) for source in candidates]
        if (
            not keys
            or any(kind != selection.owner_kind for kind, _owner_id in keys)
            or keys != sorted(set(keys))
            or previous_cursor is not None
            and keys[0] <= previous_cursor
        ):
            raise ImageFailure("invalid_response")
        if selection.dataset_id is not None and any(
            source.dataset_id != selection.dataset_id for source in candidates
        ):
            raise ImageFailure("invalid_response")
        if selection.project_id is not None and any(
            source.project_id != selection.project_id for source in candidates
        ):
            raise ImageFailure("invalid_response")
        if selection.owner_ids:
            wanted = set(selection.owner_ids)
            if any(source.owner.id not in wanted for source in candidates):
                raise ImageFailure("invalid_response")

    async def _observe_backfill_candidates(
        self,
        candidates: list[ImageSource],
        batch_token: UUID,
        heartbeat: _LeaseHeartbeat,
    ) -> ImageBackfillObservations:
        concurrency = min(max(1, len(candidates)), _MAX_UPLOAD_CONCURRENCY)
        semaphore = asyncio.Semaphore(concurrency)

        async def observe(source: ImageSource) -> ImageBackfillSourceObservation:
            heartbeat.check()
            async with semaphore:
                try:
                    identity = await self.storage.head_source(source)
                except ImageFailure as exc:
                    if exc.code == "object_missing":
                        return ImageBackfillSourceObservation(
                            owner=source.owner,
                            source_snapshot=source.source_snapshot,
                            object_identity=None,
                            error_code="source_missing",
                        )
                    if exc.code == "source_changed":
                        return ImageBackfillSourceObservation(
                            owner=source.owner,
                            source_snapshot=source.source_snapshot,
                            object_identity=None,
                            error_code="source_changed",
                        )
                    raise
                heartbeat.check()
            if not source.upload_ready:
                raise ImageFailure("source_not_ready")
            if source.source_drifted or (
                source.file_size is not None and identity.byte_size != source.file_size
            ):
                return ImageBackfillSourceObservation(
                    owner=source.owner,
                    source_snapshot=source.source_snapshot,
                    object_identity=None,
                    error_code="source_changed",
                )
            return ImageBackfillSourceObservation(
                owner=source.owner,
                source_snapshot=source.source_snapshot,
                object_identity=identity,
                error_code=None,
            )

        observed = await asyncio.gather(
            *(observe(source) for source in candidates), return_exceptions=True
        )
        failure = next(
            (row for row in observed if isinstance(row, BaseException)), None
        )
        if failure is not None:
            if isinstance(failure, ImageFailure):
                raise failure
            raise ImageFailure("storage_unavailable") from None
        items = [
            row for row in observed if isinstance(row, ImageBackfillSourceObservation)
        ]
        if len(items) != len(candidates):
            raise ImageFailure("invalid_response")
        heartbeat.check()
        return ImageBackfillObservations(batch_token=batch_token, items=items)

    async def _fail_claim(self, claim: ImageClaim, error_code: str) -> ImageWorkOutcome:
        request = FailImageMediaRequest(
            job_id=claim.job_id,
            generation=claim.generation,
            lease_token=claim.lease_token,
            error_code=error_code,
            error_detail=error_code,
        )
        result = await self.database.call("fail_image_media", request)
        if not isinstance(result, ImageWorkOutcome):
            raise ImageFailure("invalid_response")
        return result

    async def reconcile_once(self) -> ImageReconcileResult:
        candidates: list[ImageOrphanCandidate] = []
        last_result: ImageReconcileResult | None = None

        async def submit(batch: list[ImageOrphanCandidate]) -> None:
            nonlocal last_result
            request = ReconcileImageMediaRequest(
                candidates=batch,
                batch_size=_RECONCILE_BATCH_SIZE,
            )
            result = await self.database.call("reconcile_image_media", request)
            if not isinstance(result, ImageReconcileResult):
                raise ImageFailure("invalid_response")
            last_result = result

        async for candidate in self.storage.iter_orphan_generations():
            candidates.append(candidate)
            if len(candidates) == _RECONCILE_BATCH_SIZE:
                await submit(candidates)
                candidates = []
        if candidates or last_result is None:
            await submit(candidates)
        while last_result is not None and last_result.more:
            await submit([])
        assert last_result is not None
        return last_result

    async def close(self) -> None:
        self._closed = True


async def _run_child(
    request: dict[str, Any],
    *,
    workspace: Path,
    output_dir: Path,
    policy: ImagePolicy,
    heartbeat: _LeaseHeartbeat,
) -> dict[str, Any]:
    try:
        payload = json_bytes(request)
    except (TypeError, ValueError):
        raise ImageFailure("invalid_request") from None
    if len(payload) > 8 * 1024 * 1024:
        raise ImageFailure("resource_budget_exceeded")
    environment = _child_environment()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "aap_assets_images_runtime.processing",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
    except OSError:
        raise ImageFailure("generator_unavailable") from None
    if process.stdin is None or process.stdout is None or process.stderr is None:
        await _terminate_process_group(process)
        raise ImageFailure("generator_unavailable")
    stdout_task = asyncio.create_task(
        _read_limited(process.stdout, _CHILD_STDOUT_LIMIT)
    )
    stderr_task = asyncio.create_task(
        _read_limited(process.stderr, _CHILD_STDERR_LIMIT)
    )
    wait_task = asyncio.create_task(process.wait())
    try:
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()
        while not wait_task.done():
            heartbeat.check()
            if (
                _directory_size(workspace, policy.max_temp_bytes)
                > policy.max_temp_bytes
            ):
                raise ImageFailure("resource_limit")
            for task in (stdout_task, stderr_task):
                if task.done():
                    task.result()
            await asyncio.sleep(_CHILD_WATCH_INTERVAL)
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        heartbeat.check()
        if process.returncode != 0:
            body = _load_json_unique(stderr.rstrip(b"\n"))
            code = (
                body.get("code")
                if isinstance(body, dict) and set(body) == {"code"}
                else None
            )
            if code in _CHILD_ERROR_CODES:
                raise ImageFailure(code)
            raise ImageFailure("encode_failed")
        body = _load_json_unique(stdout.rstrip(b"\n"))
        if not isinstance(body, dict):
            raise ImageFailure("invalid_response")
        if _directory_size(workspace, policy.max_temp_bytes) > policy.max_temp_bytes:
            raise ImageFailure("resource_limit")
        return body
    except BaseException:
        await _terminate_process_group(process)
        for task in (stdout_task, stderr_task, wait_task):
            if not task.done():
                with suppress(BaseException):
                    await task
        raise


async def _read_limited(reader: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    while True:
        chunk = await reader.read(min(64 * 1024, limit - len(output) + 1))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > limit:
            raise ImageFailure("resource_budget_exceeded")


def _child_environment() -> dict[str, str]:
    allowed = {
        "PATH",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=_CHILD_TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


def _load_json_unique(data: bytes) -> Any:
    def pairs(rows):
        value = {}
        for key, item in rows:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def nonfinite(_value):
        raise ValueError("nonfinite JSON")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ImageFailure("invalid_response") from None


def _known_dimensions(source: ImageSource) -> list[int] | None:
    if source.width is None or source.height is None:
        return None
    return [source.width, source.height]


def _source_filename(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if len(suffix) <= 12 and suffix.startswith(".") and suffix[1:].isalnum():
        return "source" + suffix
    return "source.bin"


def _positive_int(value: Any) -> int:
    if type(value) is not int or value <= 0 or value > 2_147_483_647:
        raise ImageFailure("invalid_response")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_sha256_prefixed(value: Any) -> bool:
    return (
        isinstance(value, str) and value.startswith("sha256:") and _is_sha256(value[7:])
    )


def _regular_file_size(path: Path) -> int:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        raise ImageFailure("invalid_response") from None
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size <= 0:
        raise ImageFailure("invalid_response")
    return info.st_size


def _read_regular_file(path: Path, limit: int) -> bytes:
    size = _regular_file_size(path)
    if size > limit:
        raise ImageFailure("resource_limit")
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError:
        raise ImageFailure("invalid_response") from None
    if len(data) != size or len(data) > limit:
        raise ImageFailure("invalid_response")
    return data


def _validate_child_tiles(
    rows: Any, output_dir: Path, manifest: ImagePyramidManifest
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ImageFailure("invalid_response")
    expected: dict[tuple[int, int, int], tuple[int, int]] = {}
    for level in manifest.levels:
        plain = {
            "level": level.level,
            "width": level.width,
            "height": level.height,
            "columns": level.columns,
            "rows": level.rows,
        }
        for y in range(level.rows):
            for x in range(level.columns):
                expected[(level.level, x, y)] = expected_tile_dimensions(plain, x, y)
    found: set[tuple[int, int, int]] = set()
    result = []
    max_level = len(manifest.levels) - 1
    for row in rows:
        keys = {"path", "level", "x", "y", "width", "height", "byte_size"}
        if not isinstance(row, dict) or set(row) != keys:
            raise ImageFailure("invalid_response")
        numeric = (row["level"], row["x"], row["y"])
        if any(type(value) is not int or value < 0 for value in numeric) or any(
            type(row[field]) is not int or row[field] <= 0
            for field in ("width", "height", "byte_size")
        ):
            raise ImageFailure("invalid_response")
        level, x, y = row["level"], row["x"], row["y"]
        key = (level, x, y)
        if key not in expected or key in found:
            raise ImageFailure("tile_grid_mismatch")
        found.add(key)
        width, height = expected[key]
        if (row["width"], row["height"]) != (width, height):
            raise ImageFailure("tile_grid_mismatch")
        dzi_level = max_level - level
        path = output_dir / "pyramid_files" / str(dzi_level) / f"{x}_{y}.webp"
        if row["path"] != str(path):
            raise ImageFailure("invalid_response")
        size = _regular_file_size(path)
        if size != row["byte_size"]:
            raise ImageFailure("tile_grid_mismatch")
        result.append({"path": path, "level": level, "x": x, "y": y, "byte_size": size})
    if found != set(expected):
        raise ImageFailure("tile_grid_mismatch")
    return result


def _directory_size(directory: Path, limit: int) -> int:
    total = 0
    try:
        for current, directories, files in os.walk(directory, followlinks=False):
            for name in directories:
                path = Path(current) / name
                if path.is_symlink():
                    raise ImageFailure("tile_grid_mismatch")
            for name in files:
                path = Path(current) / name
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ImageFailure("tile_grid_mismatch")
                total += info.st_size
                if total > limit:
                    return total
    except ImageFailure:
        raise
    except OSError:
        raise ImageFailure("encode_failed") from None
    return total


def _check_finish_request_budget(request: FinishImageMediaRequest) -> None:
    if (
        len(json_bytes(request.model_dump(mode="json", by_alias=True)))
        > 8 * 1024 * 1024
    ):
        raise ImageFailure("resource_budget_exceeded")


async def _run_worker(*, once: bool, job_id: UUID | None) -> None:
    config: ImageWorkerConfig = load_config()
    database = await _connect_worker_database(config)
    try:
        if not await check_capabilities(database.pool, "worker"):
            raise RuntimeError("Assets image worker SQL capabilities are not valid")
        try:
            storage = S3ImageStorage(config.storage)
        except Exception:
            raise RuntimeError(
                "Assets image worker storage configuration is not valid"
            ) from None
        executor = ImageExecutor(
            database,
            storage,
            srgb_profile=config.srgb_profile,
        )
        try:
            if once:
                await executor.execute_once(job_id=job_id)
                return
            next_reconcile = 0.0
            while True:
                try:
                    now = asyncio.get_running_loop().time()
                    if now >= next_reconcile:
                        await executor.reconcile_once()
                        next_reconcile = (
                            asyncio.get_running_loop().time()
                            + _RECONCILE_INTERVAL_SECONDS
                        )
                    did_work = await executor.execute_once(job_id=job_id)
                except ImageFailure as exc:
                    _LOGGER.error("Assets image worker operation failed (%s)", exc.code)
                    did_work = False
                except Exception as exc:  # noqa: BLE001 - do not log source/URL data
                    _LOGGER.error(
                        "Assets image worker iteration failed (%s)",
                        type(exc).__name__,
                    )
                    did_work = False
                if did_work is None or did_work is False:
                    await asyncio.sleep(config.poll_seconds)
        finally:
            await executor.close()
    finally:
        await database.close()


async def _healthcheck() -> None:
    config = load_config()
    database = await _connect_worker_database(config)
    try:
        if not await check_capabilities(database.pool, "worker"):
            raise SystemExit("Assets image worker SQL capabilities are not valid")
    finally:
        await database.close()


async def _connect_worker_database(config: ImageWorkerConfig) -> AsyncpgImageDatabase:
    try:
        return await AsyncpgImageDatabase.connect(
            config.database_url, allowed_roles={"worker"}
        )
    except Exception:
        raise RuntimeError("Assets image worker database connection failed") from None


async def _with_signals(coroutine) -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(coroutine)
    installed = []
    for caught_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(caught_signal, task.cancel)
            installed.append(caught_signal)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await task
    except asyncio.CancelledError:
        return
    finally:
        for caught_signal in installed:
            loop.remove_signal_handler(caught_signal)


def main() -> None:
    parser = argparse.ArgumentParser(description="Assets image media worker")
    parser.add_argument("--once", action="store_true", help="execute at most one job")
    parser.add_argument("--job-id", type=UUID, help="claim only this accepted job")
    parser.add_argument(
        "--healthcheck", action="store_true", help="verify the worker SQL login"
    )
    args = parser.parse_args()
    coroutine = (
        _healthcheck()
        if args.healthcheck
        else _run_worker(once=args.once, job_id=args.job_id)
    )
    asyncio.run(_with_signals(coroutine))


if __name__ == "__main__":
    main()
