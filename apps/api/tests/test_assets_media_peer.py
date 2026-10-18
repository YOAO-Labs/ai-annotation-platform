"""Worker LOGIN interoperability and transaction ownership for media state."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_assets_media.contract import MediaClaim
from app.db.models.asset_media import AssetMediaJob
from app.db.models.dataset import DatasetItem
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]

_WORKER_URL = "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL"


async def test_restricted_worker_login_uses_native_state_without_table_access(
    test_engine,
):
    raw_url = os.environ.get(_WORKER_URL)
    if not raw_url:
        pytest.fail(f"the module-data verifier did not provide {_WORKER_URL}")
    try:
        worker_url = make_url(raw_url)
    except Exception:
        pytest.fail("the restricted worker connection URL is invalid", pytrace=False)
    if not (
        worker_url.username == "asset_media_worker_client"
        and worker_url.password
        and worker_url.host == "127.0.0.1"
        and worker_url.port is not None
        and worker_url.database
        and worker_url.database.endswith("_test")
        and test_engine.url.database == worker_url.database
    ):
        pytest.fail("media worker LOGIN URL did not target the owned test database")
    async with test_engine.connect() as owner:
        if await owner.scalar(text("SELECT current_database()")) != worker_url.database:
            pytest.fail("owner connection did not target the worker test database")

    # Verify the exact verifier-owned container and loopback binding before seeding.
    _verify_postgres(worker_url)

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id: uuid.UUID | None = None
    dataset_id: uuid.UUID | None = None
    item_id, job_id = uuid.uuid4(), uuid.uuid4()
    worker_engine = create_async_engine(worker_url, hide_parameters=True)
    try:
        async with maker.begin() as owner:
            actor_id = await _seed_actor(owner)
            dataset_id = await _seed_dataset(
                owner, actor_id=actor_id, data_type="video"
            )
            owner.add(
                DatasetItem(
                    id=item_id,
                    dataset_id=dataset_id,
                    file_name="worker-boundary.mp4",
                    file_path=f"source/{item_id}.mp4",
                    file_type="video",
                    file_size=100,
                    content_hash="a" * 64,
                    metadata_={},
                )
            )
            owner.add(
                AssetMediaJob(
                    job_id=job_id,
                    kind="video_timetable",
                    dataset_item_id=item_id,
                    slot_key=f"video_timetable:{job_id.hex}",
                    generation=1,
                    spec={"kind": "video_timetable", "sparse_stride": 1},
                    status="pending",
                    source_generation=1,
                    timetable_revision=0,
                    actor_id=None,
                    progress={},
                    attempt_count=0,
                )
            )
            await owner.flush()

        async with worker_engine.connect() as worker:
            assert await worker.scalar(text("SELECT current_user")) == (
                "asset_media_worker_client"
            )
            claim_reply = await worker.scalar(
                text(
                    "SELECT aap_assets_v1.claim_video_media("
                    "CAST(:worker_id AS uuid),CAST(:job_id AS uuid),120,"
                    "ARRAY['video_timetable']::text[])"
                ),
                {"worker_id": uuid.uuid4(), "job_id": job_id},
            )
            claim_data = claim_reply["data"]
            assert claim_reply["ok"] is True
            claim = MediaClaim.model_validate(claim_data["claim"])
            assert claim.job_id == job_id and claim.source is not None
            await worker.commit()

            # The SQL call owns no commit: staging is rolled back by this caller.
            transaction = await worker.begin()
            staged = await worker.scalar(
                text(
                    "SELECT aap_assets_v1.stage_video_timetable("
                    "CAST(:job_id AS uuid),CAST(:generation AS bigint),"
                    "CAST(:lease_token AS uuid),CAST(:rows AS jsonb))"
                ),
                {
                    "job_id": job_id,
                    "generation": claim.generation,
                    "lease_token": claim.lease_token,
                    "rows": json.dumps(
                        [
                            {
                                "frame_index": 0,
                                "pts_ms": -50,
                                "is_keyframe": True,
                                "pict_type": "I",
                                "byte_offset": 0,
                            }
                        ]
                    ),
                },
            )
            assert staged["ok"] is True
            await transaction.rollback()

        async with worker_engine.connect() as worker:
            with pytest.raises(DBAPIError) as denied:
                await worker.execute(
                    text("SELECT count(*) FROM public.asset_media_jobs")
                )
            assert getattr(denied.value.orig, "sqlstate", None) == "42501"
            await worker.rollback()

        async with test_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_media_timetable_staging "
                        "WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
                == 0
            )
            assert (
                await observer.scalar(
                    text(
                        "SELECT status FROM public.asset_media_jobs WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
                == "running"
            )
    finally:
        await worker_engine.dispose()
        if dataset_id is not None and actor_id is not None:
            async with test_engine.begin() as cleanup:
                await cleanup.execute(
                    text(
                        "DELETE FROM public.asset_media_timetable_staging WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
                await cleanup.execute(
                    text("DELETE FROM public.asset_media_jobs WHERE job_id=:job"),
                    {"job": job_id},
                )
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
