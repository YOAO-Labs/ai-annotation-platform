"""Assets-owned video state, lease and publication regressions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_media.contract import GarbageBatch, MediaClaim, MediaSource
from app.db.models.asset_media import AssetMediaJob
from app.db.models.dataset import DatasetItem
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset

pytestmark = pytest.mark.asyncio

_CLAIM = text(
    "SELECT aap_assets_v1.claim_video_media("
    "CAST(:worker_id AS uuid),CAST(:job_id AS uuid),120,"
    "ARRAY[:kind]::text[])"
)
_STAGE = text(
    "SELECT aap_assets_v1.stage_video_timetable("
    "CAST(:job_id AS uuid),CAST(:generation AS bigint),"
    "CAST(:lease_token AS uuid),CAST(:rows AS jsonb))"
)
_FINISH = text(
    "SELECT aap_assets_v1.finish_video_media("
    "CAST(:job_id AS uuid),CAST(:generation AS bigint),"
    "CAST(:lease_token AS uuid),CAST(:result AS jsonb))"
)
_FAIL = text(
    "SELECT aap_assets_v1.fail_video_media("
    "CAST(:job_id AS uuid),CAST(:generation AS bigint),"
    "CAST(:lease_token AS uuid),:error_code,:error_detail)"
)
_CLAIM_GARBAGE = text(
    "SELECT aap_assets_v1.claim_video_media_garbage(CAST(:worker_id AS uuid),120,20)"
)
_COMPLETE_GARBAGE = text(
    "SELECT aap_assets_v1.complete_video_media_garbage("
    "CAST(:garbage_id AS uuid),CAST(:lease_token AS uuid))"
)
_ADVANCE_BACKFILL = text(
    "SELECT aap_assets_v1.advance_video_backfill("
    "CAST(:job_id AS uuid),CAST(:generation AS bigint),"
    "CAST(:lease_token AS uuid),1)"
)
_RESOLVE_INTERNAL_SOURCE = text(
    "SELECT aap_assets_v1.resolve_internal_media_source("
    "CAST(:item AS uuid),CAST(:snapshot AS text),CAST(:identity AS jsonb))"
)


def _data(reply: dict) -> dict:
    assert set(reply) == {"schema", "ok", "data"}
    assert reply["schema"] == "aap-assets-media/v1"
    assert reply["ok"] is True
    assert isinstance(reply["data"], dict)
    return reply["data"]


async def _seed_video(
    db_session,
    *,
    actor_id: uuid.UUID,
    dataset_id: uuid.UUID,
    video_metadata: dict | None = None,
):
    item_id = uuid.uuid4()
    db_session.add(
        DatasetItem(
            id=item_id,
            dataset_id=dataset_id,
            file_name=f"video-{item_id.hex[:8]}.mp4",
            file_path=f"source/{item_id}.mp4",
            file_type="video",
            file_size=100,
            content_hash="a" * 64,
            metadata_={
                "video": {
                    "legacy_extension": {"keep": True},
                    **(video_metadata or {}),
                }
            },
        )
    )
    await db_session.flush()
    return item_id


async def _seed_job(
    db_session,
    *,
    item_id: uuid.UUID,
    kind: str,
    spec: dict,
    job_id: uuid.UUID | None = None,
):
    job_id = job_id or uuid.uuid4()
    db_session.add(
        AssetMediaJob(
            job_id=job_id,
            kind=kind,
            dataset_item_id=item_id,
            slot_key=f"{kind}:{job_id.hex}",
            generation=1,
            spec=spec,
            status="pending",
            source_generation=1,
            timetable_revision=0,
            actor_id=None,
            progress={},
            attempt_count=0,
        )
    )
    await db_session.flush()
    return job_id


async def _claim(db_session, *, job_id: uuid.UUID, kind: str):
    reply = await db_session.scalar(
        _CLAIM,
        {"worker_id": uuid.uuid4(), "job_id": job_id, "kind": kind},
    )
    return MediaClaim.model_validate(_data(reply)["claim"])


async def _claim_prefix(db_session):
    reply = await db_session.scalar(_CLAIM_GARBAGE, {"worker_id": uuid.uuid4()})
    batch = GarbageBatch.model_validate(_data(reply))
    assert batch.items
    return batch.items[0]


def _metadata_result(claim: MediaClaim) -> dict:
    return {
        "kind": "video_timetable",
        "source_identity": {
            "etag": "source-etag",
            "byte_size": 100,
            "version_id": None,
        },
        "source_sha256": "a" * 64,
        "metadata_patch": {"frame_count": 2, "fps": 30.0, "codec": "h264"},
        "clear_errors": [],
        "width": 1920,
        "height": 1080,
        "timetable_total_frames": 2,
        "timetable_staged_count": 2,
        "poster": None,
        "playback": None,
    }


async def test_timetable_publish_and_late_finish_retain_attempt_prefix(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    item_id = await _seed_video(db_session, actor_id=actor_id, dataset_id=dataset_id)
    job_id = await _seed_job(
        db_session,
        item_id=item_id,
        kind="video_timetable",
        spec={"kind": "video_timetable", "sparse_stride": 1},
    )
    claim = await _claim(db_session, job_id=job_id, kind="video_timetable")
    rows = [
        {
            "frame_index": 0,
            "pts_ms": -25,
            "is_keyframe": True,
            "pict_type": "I",
            "byte_offset": 0,
        },
        {
            "frame_index": 1,
            "pts_ms": 0,
            "is_keyframe": False,
            "pict_type": "P",
            "byte_offset": 10,
        },
    ]
    stage_params = {
        "job_id": job_id,
        "generation": claim.generation,
        "lease_token": claim.lease_token,
        "rows": json.dumps(rows),
    }
    staged = _data(await db_session.scalar(_STAGE, stage_params))
    assert staged["staged_count"] == 2
    assert _data(await db_session.scalar(_STAGE, stage_params))["staged_count"] == 2

    result = _metadata_result(claim)
    finish_params = {
        "job_id": job_id,
        "generation": claim.generation,
        "lease_token": claim.lease_token,
        "result": json.dumps(result),
    }
    finished = _data(await db_session.scalar(_FINISH, finish_params))
    assert finished["outcome"] == "published"
    frames = (
        await db_session.execute(
            text(
                "SELECT frame_index,pts_ms,is_keyframe FROM public.video_frame_indices "
                "WHERE dataset_item_id=:item ORDER BY frame_index"
            ),
            {"item": item_id},
        )
    ).all()
    assert [tuple(row) for row in frames] == [(0, -25, True), (1, 0, False)]
    state = (
        await db_session.execute(
            text(
                "SELECT item.media_timetable_revision,item.media_materialized_generation,"
                "job.status,job.progress FROM public.dataset_items AS item "
                "JOIN public.asset_media_jobs AS job ON job.dataset_item_id=item.id "
                "WHERE item.id=:item AND job.job_id=:job"
            ),
            {"item": item_id, "job": job_id},
        )
    ).one()
    assert tuple(state) == (
        1,
        0,
        "ready",
        {
            "timetable_rows": 2,
            "timetable_total_frames": 2,
        },
    )

    garbage = await _claim_prefix(db_session)
    assert garbage.prefix == claim.output_prefix
    first_ack = _data(
        await db_session.scalar(
            _COMPLETE_GARBAGE,
            {"garbage_id": garbage.garbage_id, "lease_token": garbage.lease_token},
        )
    )
    assert first_ack == {"garbage_id": garbage.garbage_id, "completed": True}
    retained = (
        await db_session.execute(
            text(
                "SELECT completed_at,not_before,last_ack_token,lease_token "
                "FROM public.asset_media_garbage WHERE garbage_id=:id"
            ),
            {"id": garbage.garbage_id},
        )
    ).one()
    assert retained.completed_at is None
    assert retained.not_before > datetime.now(UTC)
    assert retained.last_ack_token == garbage.lease_token
    assert retained.lease_token == garbage.lease_token
    assert (
        _data(
            await db_session.scalar(
                _COMPLETE_GARBAGE,
                {"garbage_id": garbage.garbage_id, "lease_token": garbage.lease_token},
            )
        )["completed"]
        is True
    )

    replay = _data(await db_session.scalar(_FINISH, finish_params))
    assert replay["outcome"] == "replayed"
    invalidated_ack = _data(
        await db_session.scalar(
            _COMPLETE_GARBAGE,
            {"garbage_id": garbage.garbage_id, "lease_token": garbage.lease_token},
        )
    )
    assert invalidated_ack == {"garbage_id": garbage.garbage_id, "completed": False}
    next_sweep = await _claim_prefix(db_session)
    assert next_sweep.garbage_id == garbage.garbage_id
    assert next_sweep.lease_token != garbage.lease_token


async def test_failed_attempt_late_fail_reopens_prefix_sweep(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    item_id = await _seed_video(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        video_metadata={"frame_count": 1, "fps": 30.0},
    )
    job_id = await _seed_job(
        db_session,
        item_id=item_id,
        kind="frame",
        spec={"kind": "frame", "frame_index": 0, "width": 512, "format": "webp"},
    )
    claim = await _claim(db_session, job_id=job_id, kind="frame")
    params = {
        "job_id": job_id,
        "generation": claim.generation,
        "lease_token": claim.lease_token,
        "error_code": "decode_error",
        "error_detail": "decode failed",
    }
    failed = _data(await db_session.scalar(_FAIL, params))
    assert failed["outcome"] == "failed"
    garbage = await _claim_prefix(db_session)
    acknowledged = _data(
        await db_session.scalar(
            _COMPLETE_GARBAGE,
            {"garbage_id": garbage.garbage_id, "lease_token": garbage.lease_token},
        )
    )
    assert acknowledged["completed"] is True

    replay = _data(await db_session.scalar(_FAIL, params))
    assert replay["outcome"] == "replayed"
    old_ack = _data(
        await db_session.scalar(
            _COMPLETE_GARBAGE,
            {"garbage_id": garbage.garbage_id, "lease_token": garbage.lease_token},
        )
    )
    assert old_ack["completed"] is False
    refreshed = await _claim_prefix(db_session)
    assert refreshed.garbage_id == garbage.garbage_id
    assert refreshed.lease_token != garbage.lease_token


async def test_backfill_pages_keep_source_snapshot_separate_from_materialization(
    db_session,
):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    first_id, second_id = sorted(
        (
            await _seed_video(db_session, actor_id=actor_id, dataset_id=dataset_id),
            await _seed_video(db_session, actor_id=actor_id, dataset_id=dataset_id),
        )
    )
    cutoff = datetime.now(UTC) + timedelta(days=1)
    selection = {
        "dataset_id": None,
        "dataset_item_ids": [],
        "all_items": True,
        "only_missing": True,
        "accepted_before": None,
    }
    job_id = uuid.uuid4()
    db_session.add(
        AssetMediaJob(
            job_id=job_id,
            kind="video_backfill",
            dataset_item_id=None,
            slot_key=f"video_backfill:{job_id.hex}",
            generation=1,
            spec={"kind": "video_backfill", "selection": selection},
            status="pending",
            source_generation=0,
            timetable_revision=0,
            actor_id=None,
            progress={},
            attempt_count=0,
            backfill_selection=selection,
            backfill_cutoff=cutoff,
        )
    )
    await db_session.flush()

    claim = await _claim(db_session, job_id=job_id, kind="video_backfill")
    assert claim.source is None and claim.output_prefix is None
    first = _data(
        await db_session.scalar(
            _ADVANCE_BACKFILL,
            {
                "job_id": job_id,
                "generation": claim.generation,
                "lease_token": claim.lease_token,
            },
        )
    )
    assert first["scheduled"] == 1 and first["complete"] is False
    assert first["next_item_id"] == str(first_id)

    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET media_materialized_generation="
            "media_source_generation WHERE id=:item"
        ),
        {"item": first_id},
    )
    second_claim = await _claim(db_session, job_id=job_id, kind="video_backfill")
    second = _data(
        await db_session.scalar(
            _ADVANCE_BACKFILL,
            {
                "job_id": job_id,
                "generation": second_claim.generation,
                "lease_token": second_claim.lease_token,
            },
        )
    )
    assert second["scheduled"] == 2 and second["complete"] is True
    assert second["next_item_id"] == str(second_id)
    scheduled = (
        (
            await db_session.execute(
                text(
                    "SELECT dataset_item_id FROM public.asset_media_jobs "
                    "WHERE kind='video_metadata' AND dataset_item_id IN (:first,:second) "
                    "ORDER BY dataset_item_id"
                ),
                {"first": first_id, "second": second_id},
            )
        )
        .scalars()
        .all()
    )
    assert scheduled == [first_id, second_id]


async def test_legacy_md5_hint_is_not_compared_to_worker_sha256(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    item_id = uuid.uuid4()
    db_session.add(
        DatasetItem(
            id=item_id,
            dataset_id=dataset_id,
            file_name="legacy-md5.mp4",
            file_path=f"legacy/{item_id}.mp4",
            file_type="video",
            file_size=100,
            content_hash="b" * 32,
            metadata_={},
        )
    )
    await db_session.flush()
    snapshot = await db_session.scalar(
        text(
            "SELECT aap_assets_v1._media_source_snapshot(item) "
            "FROM public.dataset_items AS item WHERE item.id=:item"
        ),
        {"item": item_id},
    )
    identity = {"etag": "legacy-etag", "byte_size": 100, "version_id": None}
    reply = await db_session.scalar(
        text(
            "SELECT aap_assets_v1._media_reconcile_source("
            "CAST(:item AS uuid),:snapshot,CAST(:identity AS jsonb),:observed_sha)"
        ),
        {
            "item": item_id,
            "snapshot": snapshot,
            "identity": json.dumps(identity),
            "observed_sha": "c" * 64,
        },
    )
    data = _data(reply)
    assert data["source_drifted"] is False
    assert data["source_generation"] == 1
    assert data["content_hash"] == "b" * 32
    after = await db_session.scalar(
        text(
            "SELECT aap_assets_v1._media_source_snapshot(item) "
            "FROM public.dataset_items AS item WHERE item.id=:item"
        ),
        {"item": item_id},
    )
    assert after == snapshot


async def test_internal_source_reads_all_item_types_without_item_lock(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id: uuid.UUID | None = None
    dataset_id: uuid.UUID | None = None
    item_id = uuid.uuid4()
    identity = {"etag": "image-head", "byte_size": 12, "version_id": None}
    snapshot: str | None = None
    try:
        async with maker.begin() as owner:
            actor_id = await _seed_actor(owner)
            dataset_id = await _seed_dataset(
                owner, actor_id=actor_id, data_type="image"
            )
            owner.add(
                DatasetItem(
                    id=item_id,
                    dataset_id=dataset_id,
                    file_name="source-image.jpg",
                    file_path=f"source/{item_id}.jpg",
                    file_type="image",
                    file_size=12,
                    metadata_={},
                )
            )
            await owner.flush()
            snapshot = await owner.scalar(
                text(
                    "SELECT aap_assets_v1._media_source_snapshot(item) "
                    "FROM public.dataset_items AS item WHERE item.id=:item"
                ),
                {"item": item_id},
            )

        lock_connection = await test_engine.connect()
        lock_transaction = await lock_connection.begin()
        reader = await test_engine.connect()
        reader_transaction = await reader.begin()
        try:
            await lock_connection.execute(
                text("SELECT id FROM public.dataset_items WHERE id=:item FOR UPDATE"),
                {"item": item_id},
            )
            reply = await asyncio.wait_for(
                reader.scalar(
                    _RESOLVE_INTERNAL_SOURCE,
                    {"item": item_id, "snapshot": None, "identity": None},
                ),
                timeout=3,
            )
            source = MediaSource.model_validate(_data(reply))
            assert source.dataset_item_id == item_id
            assert source.file_type == "image"
            assert source.object_identity is None
        finally:
            await reader_transaction.rollback()
            await lock_transaction.rollback()
            await reader.close()
            await lock_connection.close()

        async with test_engine.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.dataset_items SET media_object_identity="
                    "CAST(:identity AS jsonb) WHERE id=:item"
                ),
                {"item": item_id, "identity": json.dumps(identity)},
            )
            snapshot = await owner.scalar(
                text(
                    "SELECT aap_assets_v1._media_source_snapshot(item) "
                    "FROM public.dataset_items AS item WHERE item.id=:item"
                ),
                {"item": item_id},
            )

        lock_connection = await test_engine.connect()
        lock_transaction = await lock_connection.begin()
        reader = await test_engine.connect()
        reader_transaction = await reader.begin()
        try:
            await lock_connection.execute(
                text("SELECT id FROM public.dataset_items WHERE id=:item FOR UPDATE"),
                {"item": item_id},
            )
            reply = await asyncio.wait_for(
                reader.scalar(
                    _RESOLVE_INTERNAL_SOURCE,
                    {
                        "item": item_id,
                        "snapshot": snapshot,
                        "identity": json.dumps(identity),
                    },
                ),
                timeout=3,
            )
            source = MediaSource.model_validate(_data(reply))
            assert source.file_type == "image"
            assert source.object_identity is not None
            assert source.object_identity.etag == "image-head"
        finally:
            await reader_transaction.rollback()
            await lock_transaction.rollback()
            await reader.close()
            await lock_connection.close()

        async with test_engine.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.dataset_items SET media_object_identity=NULL "
                    "WHERE id=:item"
                ),
                {"item": item_id},
            )
            snapshot = await owner.scalar(
                text(
                    "SELECT aap_assets_v1._media_source_snapshot(item) "
                    "FROM public.dataset_items AS item WHERE item.id=:item"
                ),
                {"item": item_id},
            )

        lock_connection = await test_engine.connect()
        lock_transaction = await lock_connection.begin()
        reader = await test_engine.connect()
        reader_transaction = await reader.begin()
        try:
            await lock_connection.execute(
                text("SELECT id FROM public.dataset_items WHERE id=:item FOR UPDATE"),
                {"item": item_id},
            )
            reply = await asyncio.wait_for(
                reader.scalar(
                    _RESOLVE_INTERNAL_SOURCE,
                    {
                        "item": item_id,
                        "snapshot": snapshot,
                        "identity": json.dumps(identity),
                    },
                ),
                timeout=3,
            )
            assert reply["schema"] == "aap-assets-media/v1"
            assert reply["ok"] is False
            assert reply["error"]["code"] == "source_busy"
        finally:
            await reader_transaction.rollback()
            await lock_transaction.rollback()
            await reader.close()
            await lock_connection.close()

        async with test_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text(
                        "SELECT media_object_identity FROM public.dataset_items WHERE id=:item"
                    ),
                    {"item": item_id},
                )
                is None
            )
    finally:
        if actor_id is not None and dataset_id is not None:
            async with test_engine.begin() as cleanup:
                await cleanup.execute(
                    text("DELETE FROM public.dataset_items WHERE id=:item"),
                    {"item": item_id},
                )
                await cleanup.execute(
                    text("DELETE FROM public.datasets WHERE id=:dataset"),
                    {"dataset": dataset_id},
                )
                await cleanup.execute(
                    text("DELETE FROM public.users WHERE id=:actor"),
                    {"actor": actor_id},
                )
