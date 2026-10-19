"""Owner-aware fixtures for legacy video API regressions.

The API now reads and schedules through the Assets media owner. These helpers
bind its native client to the test SAVEPOINT and seed only through the frozen
owner capabilities, while a small in-memory object store supplies source HEADs,
cache objects and signed URLs.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID, uuid4

import pytest
from aap_assets_media import contract as c
from app.db.models.asset_media import AssetMediaJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.task import Task
from app.services.assets_media import SqlAlchemyMediaDatabase
from sqlalchemy import select


class FakeMediaStorage:
    def __init__(self) -> None:
        self.sources: dict[str, bytes] = {}
        self.cache: dict[str, bytes] = {}

    def register_source(self, key: str, body: bytes) -> c.ObjectIdentity:
        self.sources[key] = body
        return c.ObjectIdentity(
            etag=hashlib.sha256(body).hexdigest(), byte_size=len(body)
        )

    async def head_source(self, key: str) -> c.ObjectIdentity:
        body = self.sources.get(key)
        if body is None:
            raise c.MediaFailure("object_missing")
        return c.ObjectIdentity(
            etag=hashlib.sha256(body).hexdigest(), byte_size=len(body)
        )

    async def head_cache(self, key: str) -> c.ObjectIdentity | None:
        body = self.cache.get(key)
        if body is None:
            return None
        return c.ObjectIdentity(
            etag=hashlib.sha256(body).hexdigest(), byte_size=len(body)
        )

    def publish(self, key: str, body: bytes) -> c.Artifact:
        self.cache[key] = body
        return c.Artifact(
            key=key,
            byte_size=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )

    async def sign_source(self, key: str, expires_in: int = 3600) -> str:
        return f"http://storage.local/{key}"

    async def sign_cache(self, key: str, expires_in: int = 3600) -> str:
        return f"http://storage.local/{key}"


class MediaOwnerTestBinding:
    """A same-session Assets provider and deterministic S3 double."""

    def __init__(self, db, dispatcher) -> None:
        self.db = db
        self.dispatcher = dispatcher
        self.storage = FakeMediaStorage()
        self.native = SqlAlchemyMediaDatabase(db, commit=False)
        self.dispatched: set[UUID] = set()
        self.item_ids: set[UUID] = set()

    async def wake(self, job_id: UUID | None, _generation: int) -> None:
        query = select(AssetMediaJob).where(AssetMediaJob.status == "pending")
        if job_id is not None:
            query = query.where(AssetMediaJob.job_id == job_id)
        rows = list(
            (await self.db.scalars(query.order_by(AssetMediaJob.accepted_at))).all()
        )
        grouped: dict[tuple[UUID, str], list[dict[str, Any]]] = {}
        for row in rows:
            if row.job_id in self.dispatched or row.dataset_item_id is None:
                continue
            self.dispatched.add(row.job_id)
            spec = row.spec
            grouped.setdefault((row.dataset_item_id, row.kind), []).append(spec)
        for (item_id, kind), specs in grouped.items():
            if kind == "chunk":
                self.dispatcher.dispatch_video_chunks(
                    item_id, sorted(int(spec["chunk_id"]) for spec in specs)
                )
            elif kind == "frame":
                self.dispatcher.dispatch_video_frames(
                    item_id,
                    sorted(
                        (
                            {
                                "frame_index": int(spec["frame_index"]),
                                "width": int(spec["width"]),
                                "format": str(spec["format"]),
                            }
                            for spec in specs
                        ),
                        key=lambda frame: frame["frame_index"],
                    ),
                )
            elif kind in {"video_metadata", "video_timetable"}:
                self.dispatcher.dispatch_video_metadata(item_id)

    async def _source(self, item, actor_id: UUID) -> c.MediaSource:
        request = c.ReadSourceRequest(
            actor_id=actor_id, dataset_item_id=item.id, task_id=None
        )
        source = await self.native.call("read_media_source", request)
        if source.object_identity is None:
            identity = await self.storage.head_source(source.file_path)
            source = await self.native.call(
                "observe_media_source",
                c.ObserveSourceRequest(
                    **request.model_dump(),
                    source_snapshot=source.source_snapshot,
                    object_identity=identity,
                ),
            )
        if source.source_drifted or source.object_identity is None:
            raise c.MediaFailure("source_changed")
        return source

    async def seed_video_item(self, item, actor_id: UUID) -> c.MediaSource:
        """Publish current metadata/timetable and portable playback references."""
        self.item_ids.add(item.id)
        source_bytes = f"video-fixture:{item.id}".encode()
        identity = self.storage.register_source(item.file_path, source_bytes)
        item.file_size = identity.byte_size
        item.content_hash = hashlib.sha256(source_bytes).hexdigest()
        item.width = item.width or 1920
        item.height = item.height or 1080
        source = await self._source(item, actor_id)
        accepted = await self.native.call(
            "schedule_video_media",
            c.ScheduleWorkRequest(
                operation_id=None,
                dataset_item_id=item.id,
                spec=c.MetadataSpec(
                    probe_timetable=False,
                    expected_source_snapshot=source.source_snapshot,
                ),
            ),
        )
        claim = await self._claim(accepted.work.job_id, "video_metadata")
        if claim.source is None or claim.source.object_identity != identity:
            raise c.MediaFailure("source_changed")
        metadata = (item.metadata_ or {}).get("video") or {}
        frame_count = int(metadata.get("frame_count") or 90)
        fps = float(metadata.get("fps") or 30)
        duration_ms = int(
            metadata.get("duration_ms") or round(frame_count / fps * 1000)
        )
        width, height = int(item.width or 1920), int(item.height or 1080)
        playback_key = f"{claim.output_prefix}playback.mp4"
        poster_key = f"{claim.output_prefix}poster.webp"
        playback = self.storage.publish(playback_key, b"fixture playback")
        poster = self.storage.publish(poster_key, b"fixture poster")
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        metadata_patch = {
            "codec": metadata.get("codec") or "h264",
            "fps": fps,
            "frame_count": frame_count,
            "width": width,
            "height": height,
            "duration_ms": duration_ms,
            "playback_path": playback_key,
            "playback_codec": "h264",
            "poster_frame_path": poster_key,
        }
        result = c.MetadataResult(
            kind="video_metadata",
            source_identity=identity,
            source_sha256=source_sha256,
            metadata_patch=metadata_patch,
            clear_errors=[
                "probe_error",
                "playback_error",
                "poster_error",
                "frame_timetable_error",
            ],
            width=width,
            height=height,
            poster=poster,
            playback=playback,
        )
        await self.native.call(
            "finish_video_media",
            c.FinishRequest(
                job_id=claim.job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                result=result,
            ),
        )
        return await self._source(item, actor_id)

    async def _claim(self, job_id: UUID, kind: str) -> c.MediaClaim:
        claimed = await self.native.call(
            "claim_video_media",
            c.ClaimRequest(worker_id=uuid4(), job_id=job_id, kinds=[kind]),
        )
        if claimed.claim is None:
            raise c.MediaFailure("invalid_response")
        return claimed.claim

    async def _ensure_job(self, item, actor_id: UUID, spec) -> UUID:
        source = await self._source(item, actor_id)
        task_id = await self.db.scalar(
            select(Task.id)
            .where(Task.dataset_item_id == item.id)
            .order_by(Task.created_at)
            .limit(1)
        )
        if isinstance(spec, c.ChunkSpec):
            selection = c.ChunkSelection(
                chunk_ids=[spec.chunk_id],
                chunk_size_frames=spec.chunk_size_frames,
                expected_source_snapshot=source.source_snapshot,
            )
        else:
            selection = c.FrameSelection(
                frame_indices=[spec.frame_index],
                width=spec.width,
                format=spec.format,
                expected_source_snapshot=source.source_snapshot,
            )
        state = await self.native.call(
            "ensure_video_media",
            c.AssetsRequest(
                actor_id=actor_id,
                dataset_item_id=item.id,
                task_id=task_id,
                selection=selection,
            ),
        )
        asset = next(
            (candidate for candidate in state.assets if candidate.kind == spec.kind),
            None,
        )
        if asset is None or asset.job_id is None:
            raise c.MediaFailure("invalid_response")
        return asset.job_id

    async def seed_chunk(
        self,
        item,
        actor_id: UUID,
        chunk_id: int,
        *,
        status: str = "pending",
        error: str = "chunk failed",
        chunk_size_frames: int = 60,
        start_pts_ms: int | None = None,
        end_pts_ms: int | None = None,
        generation_mode: str = "smart_copy",
        diagnostics: dict[str, Any] | None = None,
        pre_dispatched: bool = True,
    ) -> UUID:
        job_id = await self._ensure_job(
            item,
            actor_id,
            c.ChunkSpec(
                chunk_id=chunk_id,
                chunk_size_frames=chunk_size_frames,
            ),
        )
        if status == "pending":
            if pre_dispatched:
                self.dispatched.add(job_id)
            return job_id
        claim = await self._claim(job_id, "chunk")
        if status == "failed":
            await self.native.call(
                "fail_video_media",
                c.FailRequest(
                    job_id=job_id,
                    generation=claim.generation,
                    lease_token=claim.lease_token,
                    error_code="chunk_failed",
                    error_detail=error,
                ),
            )
            return job_id
        if status != "ready" or claim.source is None or claim.output_prefix is None:
            raise ValueError(f"unsupported chunk fixture status: {status}")
        artifact = self.storage.publish(
            f"{claim.output_prefix}chunk.mp4", b"fixture chunk bytes"
        )
        full_diagnostics = {
            "source_codec": "h264",
            "output_codec": "h264",
            "width": 1920,
            "height": 1080,
            "keyframe_aligned": True,
            "start_byte_offset": 100,
            "end_byte_offset": 9000,
            "smart_copy_eligible": True,
            "fallback_reason": None,
            **(diagnostics or {}),
        }
        await self.native.call(
            "finish_video_media",
            c.FinishRequest(
                job_id=job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                result=c.ChunkResult(
                    source_identity=claim.source.object_identity,
                    source_sha256=hashlib.sha256(
                        self.storage.sources[item.file_path]
                    ).hexdigest(),
                    artifact=artifact,
                    generation_mode=generation_mode,
                    diagnostics=full_diagnostics,
                    start_pts_ms=start_pts_ms,
                    end_pts_ms=end_pts_ms,
                ),
            ),
        )
        return job_id

    async def seed_frame(
        self,
        item,
        actor_id: UUID,
        frame_index: int,
        *,
        width: int = 512,
        format: str = "webp",
        status: str = "pending",
        error: str = "frame failed",
        pre_dispatched: bool = True,
    ) -> UUID:
        job_id = await self._ensure_job(
            item,
            actor_id,
            c.FrameSpec(
                frame_index=frame_index,
                width=width,
                format=format,
            ),
        )
        if status == "pending":
            if pre_dispatched:
                self.dispatched.add(job_id)
            return job_id
        claim = await self._claim(job_id, "frame")
        if status == "failed":
            await self.native.call(
                "fail_video_media",
                c.FailRequest(
                    job_id=job_id,
                    generation=claim.generation,
                    lease_token=claim.lease_token,
                    error_code="frame_failed",
                    error_detail=error,
                ),
            )
            return job_id
        if status != "ready" or claim.source is None or claim.output_prefix is None:
            raise ValueError(f"unsupported frame fixture status: {status}")
        extension = "jpeg" if format == "jpeg" else "webp"
        artifact = self.storage.publish(
            f"{claim.output_prefix}frame.{extension}", b"fixture frame bytes"
        )
        await self.native.call(
            "finish_video_media",
            c.FinishRequest(
                job_id=job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                result=c.FrameResult(
                    source_identity=claim.source.object_identity,
                    source_sha256=hashlib.sha256(
                        self.storage.sources[item.file_path]
                    ).hexdigest(),
                    artifact=artifact,
                ),
            ),
        )
        return job_id

    async def seed_failed_metadata(
        self, item, actor_id: UUID, *, error: str = "ffprobe failed"
    ) -> UUID:
        source = await self._source(item, actor_id)
        accepted = await self.native.call(
            "schedule_video_media",
            c.ScheduleWorkRequest(
                operation_id=None,
                dataset_item_id=item.id,
                spec=c.MetadataSpec(expected_source_snapshot=source.source_snapshot),
                force=True,
            ),
        )
        claim = await self._claim(accepted.work.job_id, "video_metadata")
        await self.native.call(
            "fail_video_media",
            c.FailRequest(
                job_id=claim.job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                error_code="probe_failed",
                error_detail=error,
            ),
        )
        return claim.job_id

    async def run_timetable(
        self,
        job_id: UUID,
        *,
        frame_rows: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> None:
        if error is None:
            job = await self.db.get(AssetMediaJob, job_id)
            item = await self.db.get(DatasetItem, job.dataset_item_id)
            dataset = await self.db.get(Dataset, item.dataset_id)
            await self._source(item, dataset.created_by)
        claim = await self._claim(job_id, "video_timetable")
        if error is not None:
            await self.native.call(
                "fail_video_media",
                c.FailRequest(
                    job_id=job_id,
                    generation=claim.generation,
                    lease_token=claim.lease_token,
                    error_code="timetable_failed",
                    error_detail=error,
                ),
            )
            return
        frame_count = int(claim.source.video_metadata.get("frame_count") or 0)
        rows = [
            c.FrameEntry.model_validate(row)
            for row in (
                frame_rows
                if frame_rows is not None
                else [
                    {
                        "frame_index": index,
                        "pts_ms": index * 33,
                        "is_keyframe": index in {0, 2},
                        "pict_type": "I" if index in {0, 2} else "P",
                        "byte_offset": index * 10,
                    }
                    for index in (0, 2, frame_count - 1)
                    if 0 <= index < frame_count
                ]
            )
        ]
        if rows:
            await self.native.call(
                "stage_video_timetable",
                c.StageTimetableRequest(
                    job_id=job_id,
                    generation=claim.generation,
                    lease_token=claim.lease_token,
                    rows=rows,
                ),
            )
        await self.native.call(
            "finish_video_media",
            c.FinishRequest(
                job_id=job_id,
                generation=claim.generation,
                lease_token=claim.lease_token,
                result=c.MetadataResult(
                    kind="video_timetable",
                    source_identity=claim.source.object_identity,
                    source_sha256=hashlib.sha256(
                        self.storage.sources[claim.source.file_path]
                    ).hexdigest(),
                    metadata_patch={},
                    clear_errors=["frame_timetable_error"],
                    timetable_total_frames=frame_count if rows else None,
                    timetable_staged_count=len(rows) if rows else None,
                ),
            ),
        )

    async def remove_seeded_jobs(self) -> None:
        if self.item_ids:
            await self.db.execute(
                AssetMediaJob.__table__.delete().where(
                    AssetMediaJob.dataset_item_id.in_(self.item_ids)
                )
            )


@pytest.fixture
async def media_owner(db_session, media_dispatcher, monkeypatch):
    """Bind explicit consumer tests to same-SAVEPOINT Assets facts and fake S3."""
    from app.services import assets_media

    binding = MediaOwnerTestBinding(db_session, media_dispatcher)
    monkeypatch.setattr(
        assets_media,
        "_IndependentMediaDatabase",
        lambda _session_factory=None: assets_media.SqlAlchemyMediaDatabase(
            db_session, commit=False
        ),
    )
    monkeypatch.setattr(
        assets_media, "media_storage", lambda **_kwargs: binding.storage
    )
    monkeypatch.setattr(assets_media, "_wake", binding.wake)
    db_session.info["media_owner_test_binding"] = binding
    try:
        yield binding
    finally:
        await binding.remove_seeded_jobs()
        db_session.info.pop("media_owner_test_binding", None)
