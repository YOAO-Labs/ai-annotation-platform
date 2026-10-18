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
_ENSURE = text(
    "SELECT aap_assets_v1.ensure_video_media("
    "CAST(:actor_id AS uuid),CAST(:item_id AS uuid),NULL,CAST(:selection AS jsonb))"
)
_FAILURES = text(
    "SELECT aap_assets_v1.read_video_failures(CAST(:actor_id AS uuid),50,0)"
)
_READ_TIMETABLE = text(
    "SELECT aap_assets_v1.read_video_timetable("
    "CAST(:actor_id AS uuid),CAST(:item_id AS uuid),NULL,NULL,NULL,"
    "CAST(:after_frame AS integer),500)"
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


@pytest.mark.parametrize("poster_error", [False, True])
async def test_metadata_freshness_does_not_require_h264_playback_copy_or_optional_poster(
    db_session, poster_error
):
    from aap_assets_media.contract import MetadataResult

    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    item_id = await _seed_video(db_session, actor_id=actor_id, dataset_id=dataset_id)
    job_id = await _seed_job(
        db_session,
        item_id=item_id,
        kind="video_metadata",
        spec={
            "kind": "video_metadata",
            "probe_timetable": False,
            "generate_poster": poster_error,
            "generate_playback": not poster_error,
        },
    )
    claim = await _claim(db_session, job_id=job_id, kind="video_metadata")
    patch = {"frame_count": 2, "fps": 30.0, "codec": "h264"}
    if poster_error:
        patch["poster_error"] = "poster encoding failed"
    result = MetadataResult(
        kind="video_metadata",
        source_identity={"etag": "source-etag", "byte_size": 100, "version_id": None},
        source_sha256="a" * 64,
        metadata_patch=patch,
        clear_errors=["probe_error", "playback_error"],
        width=1920,
        height=1080,
    )
    reply = _data(
        await db_session.scalar(
            _FINISH,
            {
                "job_id": job_id,
                "generation": claim.generation,
                "lease_token": claim.lease_token,
                "result": result.model_dump_json(),
            },
        )
    )
    assert reply["outcome"] == "published"
    row = (
        await db_session.execute(
            text(
                "SELECT media_materialized_generation,metadata FROM public.dataset_items WHERE id=:id"
            ),
            {"id": item_id},
        )
    ).one()
    assert row.media_materialized_generation == 1
    assert row.metadata["video"]["codec"] == "h264"
    assert "playback_path" not in row.metadata["video"]
    assert ("poster_error" in row.metadata["video"]) == poster_error


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


def _asset_job_spec(kind: str, index: int, snapshot: str) -> dict:
    if kind == "frame":
        return {
            "kind": "frame",
            "frame_index": index,
            "width": 512,
            "format": "webp",
            "expected_source_snapshot": snapshot,
        }
    return {
        "kind": "chunk",
        "chunk_id": index,
        "chunk_size_frames": 2,
        "expected_source_snapshot": snapshot,
    }


def _asset_slot(kind: str, index: int) -> str:
    if kind == "frame":
        return f"frame:{index}:512:webp"
    return f"chunk:{index}"


async def _insert_media_projection(
    db_session,
    *,
    item_id: uuid.UUID,
    kind: str,
    index: int,
    status: str,
    source_generation: int = 1,
    timetable_revision: int = 0,
    storage_key: str | None = None,
    byte_size: int | None = None,
    error: str | None = None,
):
    if kind == "frame":
        await db_session.execute(
            text(
                "INSERT INTO public.video_frame_cache "
                "(id,dataset_item_id,frame_index,width,format,storage_key,byte_size,status,error,"
                "source_generation,timetable_revision,created_at,updated_at) "
                "VALUES (:id,:item,:index,512,'webp',:storage_key,:byte_size,:status,:error,"
                ":source_generation,:timetable_revision,now(),now())"
            ),
            {
                "id": uuid.uuid4(),
                "item": item_id,
                "index": index,
                "storage_key": storage_key,
                "byte_size": byte_size,
                "status": status,
                "error": error,
                "source_generation": source_generation,
                "timetable_revision": timetable_revision,
            },
        )
        return
    start = index * 2
    await db_session.execute(
        text(
            "INSERT INTO public.video_chunks "
            "(id,dataset_item_id,chunk_id,start_frame,end_frame,storage_key,byte_size,status,error,"
            "source_generation,timetable_revision,created_at,updated_at) "
            "VALUES (:id,:item,:index,:start,:end,:storage_key,:byte_size,:status,:error,"
            ":source_generation,:timetable_revision,now(),now())"
        ),
        {
            "id": uuid.uuid4(),
            "item": item_id,
            "index": index,
            "start": start,
            "end": start + 1,
            "storage_key": storage_key,
            "byte_size": byte_size,
            "status": status,
            "error": error,
            "source_generation": source_generation,
            "timetable_revision": timetable_revision,
        },
    )


async def test_sparse_timetable_accepts_keyframes_and_requires_grid_rows(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    item_id = await _seed_video(db_session, actor_id=actor_id, dataset_id=dataset_id)

    valid_rows = [
        {"frame_index": index, "pts_ms": index * 10, "is_keyframe": index in {0, 2}}
        for index in (0, 2, 3, 6, 9)
    ]
    missing_grid_rows = [row for row in valid_rows if row["frame_index"] != 3]
    cases = ((valid_rows, 5, True), (missing_grid_rows, 4, False))

    for rows, staged_count, should_publish in cases:
        job_id = await _seed_job(
            db_session,
            item_id=item_id,
            kind="video_timetable",
            spec={"kind": "video_timetable", "sparse_stride": 3},
        )
        claim = await _claim(db_session, job_id=job_id, kind="video_timetable")
        staged = _data(
            await db_session.scalar(
                _STAGE,
                {
                    "job_id": job_id,
                    "generation": claim.generation,
                    "lease_token": claim.lease_token,
                    "rows": json.dumps(rows),
                },
            )
        )
        assert staged["staged_count"] == staged_count

        result = _metadata_result(claim)
        result["metadata_patch"]["frame_count"] = 10
        result["timetable_total_frames"] = 10
        result["timetable_staged_count"] = staged_count
        reply = await db_session.scalar(
            _FINISH,
            {
                "job_id": job_id,
                "generation": claim.generation,
                "lease_token": claim.lease_token,
                "result": json.dumps(result),
            },
        )
        if should_publish:
            assert _data(reply)["outcome"] == "published"
        else:
            assert reply == {
                "schema": "aap-assets-media/v1",
                "ok": False,
                "error": {"code": "invalid_request"},
            }

    state = (
        await db_session.execute(
            text(
                "SELECT item.media_timetable_revision,job.status "
                "FROM public.dataset_items AS item "
                "JOIN public.asset_media_jobs AS job ON job.dataset_item_id=item.id "
                "WHERE item.id=:item AND job.job_id=:job"
            ),
            {"item": item_id, "job": job_id},
        )
    ).one()
    assert tuple(state) == (1, "running")
    frames = (
        await db_session.execute(
            text(
                "SELECT frame_index,is_keyframe FROM public.video_frame_indices "
                "WHERE dataset_item_id=:item ORDER BY frame_index"
            ),
            {"item": item_id},
        )
    ).all()
    assert [tuple(row) for row in frames] == [
        (0, True),
        (2, True),
        (3, False),
        (6, False),
        (9, False),
    ]


async def test_timetable_probe_error_preserves_current_rows_and_is_visible(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    item_id = await _seed_video(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        video_metadata={
            "frame_count": 4,
            "fps": 30.0,
            "frame_timetable_frame_count": 4,
            "playback_path": "media-assets/previous/playback.mp4",
        },
    )
    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET media_materialized_generation=1,"
            "media_timetable_revision=1 WHERE id=:item"
        ),
        {"item": item_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO public.video_frame_indices "
            "(id,dataset_item_id,frame_index,pts_ms,is_keyframe) "
            "VALUES (:id,:item,0,0,true),(:id2,:item,3,100,false)"
        ),
        {"id": uuid.uuid4(), "id2": uuid.uuid4(), "item": item_id},
    )
    job_id = await _seed_job(
        db_session,
        item_id=item_id,
        kind="video_timetable",
        spec={"kind": "video_timetable", "sparse_stride": 3},
    )
    claim = await _claim(db_session, job_id=job_id, kind="video_timetable")
    result = _metadata_result(claim)
    result["metadata_patch"] = {"frame_timetable_error": "ffprobe failed"}
    result["timetable_total_frames"] = None
    result["timetable_staged_count"] = None
    finished = _data(
        await db_session.scalar(
            _FINISH,
            {
                "job_id": job_id,
                "generation": claim.generation,
                "lease_token": claim.lease_token,
                "result": json.dumps(result),
            },
        )
    )
    assert finished["outcome"] == "failed"
    assert (
        _data(
            await db_session.scalar(
                _FINISH,
                {
                    "job_id": job_id,
                    "generation": claim.generation,
                    "lease_token": claim.lease_token,
                    "result": json.dumps(result),
                },
            )
        )["outcome"]
        == "failed"
    )

    item = (
        await db_session.execute(
            text(
                "SELECT media_timetable_revision,media_materialized_generation,metadata "
                "FROM public.dataset_items WHERE id=:item"
            ),
            {"item": item_id},
        )
    ).one()
    assert item.media_timetable_revision == 1
    assert item.media_materialized_generation == 1
    assert item.metadata["video"]["frame_timetable_error"] == "ffprobe failed"
    assert item.metadata["video"]["frame_timetable_frame_count"] == 4
    assert (
        item.metadata["video"]["playback_path"] == "media-assets/previous/playback.mp4"
    )
    job = (
        await db_session.execute(
            text(
                "SELECT status,error_code,error_detail FROM public.asset_media_jobs "
                "WHERE job_id=:job"
            ),
            {"job": job_id},
        )
    ).one()
    assert tuple(job) == ("failed", "frame_timetable_error", "ffprobe failed")
    frames = (
        await db_session.execute(
            text(
                "SELECT frame_index,pts_ms,is_keyframe FROM public.video_frame_indices "
                "WHERE dataset_item_id=:item ORDER BY frame_index"
            ),
            {"item": item_id},
        )
    ).all()
    assert [tuple(row) for row in frames] == [(0, 0, True), (3, 100, False)]
    failures = _data(await db_session.scalar(_FAILURES, {"actor_id": actor_id}))[
        "items"
    ]
    assert len(failures) == 1
    assert failures[0]["asset_type"] == "frame_timetable"
    assert failures[0]["error"] == "ffprobe failed"


async def test_full_timetable_staging_and_500_row_reads_are_atomic_and_stable(
    db_session,
):
    total = 1503
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    item_id = await _seed_video(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        video_metadata={"frame_count": total, "fps": 30.0},
    )
    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET media_materialized_generation=1,"
            "media_timetable_revision=1 WHERE id=:item"
        ),
        {"item": item_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO public.video_frame_indices "
            "(id,dataset_item_id,frame_index,pts_ms,is_keyframe) "
            "VALUES (:id,:item,0,-20,true),(:id2,:item,1,13,false)"
        ),
        {"id": uuid.uuid4(), "id2": uuid.uuid4(), "item": item_id},
    )
    job_id = await _seed_job(
        db_session,
        item_id=item_id,
        kind="video_timetable",
        spec={"kind": "video_timetable", "sparse_stride": 1},
    )
    claim = await _claim(db_session, job_id=job_id, kind="video_timetable")
    rows = [
        {"frame_index": index, "pts_ms": index * 33, "is_keyframe": index == 0}
        for index in range(total)
    ]
    pages = [rows[start : start + 500] for start in range(0, total, 500)]
    assert [len(page) for page in pages] == [500, 500, 500, 3]
    for page_index, page in enumerate(pages):
        if page_index == 2:
            continue
        staged = _data(
            await db_session.scalar(
                _STAGE,
                {
                    "job_id": job_id,
                    "generation": claim.generation,
                    "lease_token": claim.lease_token,
                    "rows": json.dumps(page),
                },
            )
        )
    assert staged["staged_count"] == 1003
    incomplete = _metadata_result(claim)
    incomplete["metadata_patch"]["frame_count"] = total
    incomplete["timetable_total_frames"] = total
    incomplete["timetable_staged_count"] = 1003
    rejected = await db_session.scalar(
        _FINISH,
        {
            "job_id": job_id,
            "generation": claim.generation,
            "lease_token": claim.lease_token,
            "result": json.dumps(incomplete),
        },
    )
    assert rejected == {
        "schema": "aap-assets-media/v1",
        "ok": False,
        "error": {"code": "invalid_request"},
    }
    unchanged = (
        await db_session.execute(
            text(
                "SELECT media_timetable_revision FROM public.dataset_items WHERE id=:item"
            ),
            {"item": item_id},
        )
    ).scalar_one()
    old_frames = (
        await db_session.execute(
            text(
                "SELECT frame_index,pts_ms FROM public.video_frame_indices "
                "WHERE dataset_item_id=:item ORDER BY frame_index"
            ),
            {"item": item_id},
        )
    ).all()
    assert unchanged == 1
    assert [tuple(row) for row in old_frames] == [(0, -20), (1, 13)]

    restored = _data(
        await db_session.scalar(
            _STAGE,
            {
                "job_id": job_id,
                "generation": claim.generation,
                "lease_token": claim.lease_token,
                "rows": json.dumps(pages[2]),
            },
        )
    )
    assert restored["staged_count"] == total
    complete = _metadata_result(claim)
    complete["metadata_patch"]["frame_count"] = total
    complete["timetable_total_frames"] = total
    complete["timetable_staged_count"] = total
    assert (
        _data(
            await db_session.scalar(
                _FINISH,
                {
                    "job_id": job_id,
                    "generation": claim.generation,
                    "lease_token": claim.lease_token,
                    "result": json.dumps(complete),
                },
            )
        )["outcome"]
        == "published"
    )

    cursor = None
    digest = None
    read_rows = []
    observed_page_sizes = []
    while True:
        page = _data(
            await db_session.scalar(
                _READ_TIMETABLE,
                {"actor_id": actor_id, "item_id": item_id, "after_frame": cursor},
            )
        )
        assert page["total"] == total
        assert page["after_frame"] == cursor
        if digest is None:
            digest = page["snapshot_digest"]
        else:
            assert page["snapshot_digest"] == digest
        observed_page_sizes.append(len(page["frames"]))
        read_rows.extend(page["frames"])
        if page["complete"]:
            assert page["next_frame"] is None
            break
        assert page["next_frame"] == page["frames"][-1]["frame_index"]
        cursor = page["next_frame"]
    assert observed_page_sizes == [500, 500, 500, 3]
    assert [row["frame_index"] for row in read_rows] == list(range(total))


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
    assert first_ack == {"garbage_id": str(garbage.garbage_id), "completed": True}
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
    assert invalidated_ack == {
        "garbage_id": str(garbage.garbage_id),
        "completed": False,
    }
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


@pytest.mark.parametrize("kind", ["frame", "chunk"])
async def test_worker_failure_marks_current_projection_for_failed_only_and_listing(
    db_session, kind
):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    item_id = await _seed_video(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        video_metadata={"frame_count": 4, "fps": 30.0},
    )
    await db_session.execute(
        text(
            "UPDATE public.dataset_items "
            "SET media_materialized_generation=media_source_generation WHERE id=:item"
        ),
        {"item": item_id},
    )
    snapshot = await db_session.scalar(
        text(
            "SELECT aap_assets_v1._media_source_snapshot(item) "
            "FROM public.dataset_items AS item WHERE item.id=:item"
        ),
        {"item": item_id},
    )
    spec = _asset_job_spec(kind, 0, snapshot)
    job_id = await _seed_job(db_session, item_id=item_id, kind=kind, spec=spec)
    await db_session.execute(
        text("UPDATE public.asset_media_jobs SET slot_key=:slot WHERE job_id=:job"),
        {"slot": _asset_slot(kind, 0), "job": job_id},
    )
    await _insert_media_projection(
        db_session, item_id=item_id, kind=kind, index=0, status="pending"
    )
    claim = await _claim(db_session, job_id=job_id, kind=kind)

    failed = _data(
        await db_session.scalar(
            _FAIL,
            {
                "job_id": job_id,
                "generation": claim.generation,
                "lease_token": claim.lease_token,
                "error_code": "decode_error",
                "error_detail": "decode failed",
            },
        )
    )
    assert failed["outcome"] == "failed"
    projection = (
        await db_session.execute(
            text(
                "SELECT status,error FROM public.video_frame_cache "
                "WHERE dataset_item_id=:item AND frame_index=0"
                if kind == "frame"
                else "SELECT status,error FROM public.video_chunks "
                "WHERE dataset_item_id=:item AND chunk_id=0"
            ),
            {"item": item_id},
        )
    ).one()
    assert tuple(projection) == ("failed", "decode failed")

    selection = (
        {
            "kind": "frames",
            "frame_indices": [0],
            "width": 512,
            "format": "webp",
            "force": False,
            "failed_only": True,
        }
        if kind == "frame"
        else {
            "kind": "chunks",
            "chunk_ids": [0],
            "chunk_size_frames": 2,
            "force": False,
            "failed_only": True,
        }
    )
    assets = _data(
        await db_session.scalar(
            _ENSURE,
            {
                "actor_id": actor_id,
                "item_id": item_id,
                "selection": json.dumps(selection),
            },
        )
    )["assets"]
    assert len(assets) == 1
    assert assets[0]["status"] == "failed"
    assert assets[0]["error"] == "decode failed"

    failures = _data(await db_session.scalar(_FAILURES, {"actor_id": actor_id}))[
        "items"
    ]
    assert len(failures) == 1
    assert failures[0]["asset_type"] == kind
    assert failures[0]["error"] == "decode failed"


@pytest.mark.parametrize("kind", ["frame", "chunk"])
async def test_worker_failure_preserves_ready_old_output_and_newer_source_projection(
    db_session, kind
):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    item_id = await _seed_video(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        video_metadata={"frame_count": 4, "fps": 30.0},
    )
    snapshot = await db_session.scalar(
        text(
            "SELECT aap_assets_v1._media_source_snapshot(item) "
            "FROM public.dataset_items AS item WHERE item.id=:item"
        ),
        {"item": item_id},
    )
    spec = _asset_job_spec(kind, 0, snapshot)
    job_id = await _seed_job(db_session, item_id=item_id, kind=kind, spec=spec)
    await db_session.execute(
        text("UPDATE public.asset_media_jobs SET slot_key=:slot WHERE job_id=:job"),
        {"slot": _asset_slot(kind, 0), "job": job_id},
    )
    old_key = "media-assets/previous-ready-output"
    await _insert_media_projection(
        db_session,
        item_id=item_id,
        kind=kind,
        index=0,
        status="pending",
        storage_key=old_key,
        byte_size=100,
    )
    claim = await _claim(db_session, job_id=job_id, kind=kind)
    failed = _data(
        await db_session.scalar(
            _FAIL,
            {
                "job_id": job_id,
                "generation": claim.generation,
                "lease_token": claim.lease_token,
                "error_code": "refresh_error",
                "error_detail": "refresh failed",
            },
        )
    )
    assert failed["outcome"] == "failed"

    projection_table = "video_frame_cache" if kind == "frame" else "video_chunks"
    projection_id = "frame_index" if kind == "frame" else "chunk_id"
    preserved = (
        await db_session.execute(
            text(
                f"SELECT status,error,storage_key,source_generation "
                f"FROM public.{projection_table} "
                f"WHERE dataset_item_id=:item AND {projection_id}=0"
            ),
            {"item": item_id},
        )
    ).one()
    assert tuple(preserved) == ("ready", None, old_key, 1)

    changed_item_id = await _seed_video(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        video_metadata={"frame_count": 4, "fps": 30.0},
    )
    changed_snapshot = await db_session.scalar(
        text(
            "SELECT aap_assets_v1._media_source_snapshot(item) "
            "FROM public.dataset_items AS item WHERE item.id=:item"
        ),
        {"item": changed_item_id},
    )
    changed_spec = _asset_job_spec(kind, 0, changed_snapshot)
    changed_job_id = await _seed_job(
        db_session, item_id=changed_item_id, kind=kind, spec=changed_spec
    )
    await db_session.execute(
        text("UPDATE public.asset_media_jobs SET slot_key=:slot WHERE job_id=:job"),
        {"slot": _asset_slot(kind, 0), "job": changed_job_id},
    )
    await _insert_media_projection(
        db_session,
        item_id=changed_item_id,
        kind=kind,
        index=0,
        status="pending",
    )
    changed_claim = await _claim(db_session, job_id=changed_job_id, kind=kind)
    current_key = "media-assets/current-source-output"
    await db_session.execute(
        text(
            "UPDATE public.dataset_items SET media_source_generation=2 WHERE id=:item"
        ),
        {"item": changed_item_id},
    )
    await db_session.execute(
        text(
            f"UPDATE public.{projection_table} SET status='ready',storage_key=:key,"
            "byte_size=100,source_generation=2 WHERE dataset_item_id=:item AND "
            f"{projection_id}=0"
        ),
        {"key": current_key, "item": changed_item_id},
    )
    stale_scope = _data(
        await db_session.scalar(
            _FAIL,
            {
                "job_id": changed_job_id,
                "generation": changed_claim.generation,
                "lease_token": changed_claim.lease_token,
                "error_code": "source_changed",
                "error_detail": "source changed during attempt",
            },
        )
    )
    assert stale_scope["outcome"] == "failed"
    current = (
        await db_session.execute(
            text(
                f"SELECT status,error,storage_key,source_generation "
                f"FROM public.{projection_table} "
                f"WHERE dataset_item_id=:item AND {projection_id}=0"
            ),
            {"item": changed_item_id},
        )
    ).one()
    assert tuple(current) == ("ready", None, current_key, 2)


async def test_stale_fail_lease_cannot_change_current_projection(db_session):
    actor_id = await _seed_actor(db_session)
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id, data_type="video")
    item_id = await _seed_video(
        db_session,
        actor_id=actor_id,
        dataset_id=dataset_id,
        video_metadata={"frame_count": 4, "fps": 30.0},
    )
    snapshot = await db_session.scalar(
        text(
            "SELECT aap_assets_v1._media_source_snapshot(item) "
            "FROM public.dataset_items AS item WHERE item.id=:item"
        ),
        {"item": item_id},
    )
    job_id = await _seed_job(
        db_session,
        item_id=item_id,
        kind="frame",
        spec=_asset_job_spec("frame", 0, snapshot),
    )
    await db_session.execute(
        text("UPDATE public.asset_media_jobs SET slot_key=:slot WHERE job_id=:job"),
        {"slot": _asset_slot("frame", 0), "job": job_id},
    )
    await _insert_media_projection(
        db_session, item_id=item_id, kind="frame", index=0, status="pending"
    )
    claim = await _claim(db_session, job_id=job_id, kind="frame")
    await db_session.execute(
        text(
            "UPDATE public.asset_media_jobs SET generation=2,status='pending',"
            "lease_token=NULL,lease_expires_at=NULL,last_lease_token=NULL,last_outcome=NULL "
            "WHERE job_id=:job"
        ),
        {"job": job_id},
    )

    stale = _data(
        await db_session.scalar(
            _FAIL,
            {
                "job_id": job_id,
                "generation": claim.generation,
                "lease_token": claim.lease_token,
                "error_code": "decode_error",
                "error_detail": "late failure",
            },
        )
    )
    assert stale["outcome"] == "lease_lost"
    projection = (
        await db_session.execute(
            text(
                "SELECT status,error,source_generation FROM public.video_frame_cache "
                "WHERE dataset_item_id=:item AND frame_index=0"
            ),
            {"item": item_id},
        )
    ).one()
    assert tuple(projection) == ("pending", None, 1)


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
