"""Current business collector against owned native state and real Mask bytes."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling_mask_gc import contract as c
from app.application.raster_mask_retention import RasterMaskRetentionWorkflow
from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services import mask_gc_collection as collection
from app.services.mask_gc import IndependentMaskGcDatabase, begin_mask_content_write
from tests.test_asset_storage_cleanup_runtime import _object_exists
from tests.test_mask_content_live import _owned_resources
from tests.test_mask_gc_preview_authority import _snapshot


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_MASK_CONTENT_ACCEPTANCE") != "1",
        reason="requires verifier-owned native state and Mask storage",
    ),
]


@asynccontextmanager
async def _inventory(test_engine, *, retained=2, orphans=7):
    resources, bucket, _, s3, _, cleanup_credentials = _owned_resources(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id, project_id, task_id = uuid4(), uuid4(), uuid4()
    prefix = uuid4().hex[:16]
    digests = [prefix + f"{index:048x}" for index in range(retained + orphans)]
    keys = [f"raster-masks/sha256/{d[:2]}/{d[2:4]}/{d}.json" for d in digests]
    existing_keys = {
        obj["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix="raster-masks/sha256/"
        )
        for obj in page.get("Contents", [])
    }
    try:

        def publish(key):
            s3.put_object(
                Bucket=bucket, Key=key, Body=b"{}", ContentType="application/json"
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(publish, keys))
        async with maker.begin() as db:
            db.add(
                User(
                    id=actor_id,
                    email=f"gc-{actor_id.hex}@example.test",
                    name="GC owner",
                    password_hash="owned-test",
                    role="project_admin",
                    is_active=True,
                )
            )
            await db.flush()
            db.add(
                Project(
                    id=project_id,
                    display_id=f"P-GC-{prefix[:12]}",
                    name="GC inventory",
                    type_label="Image",
                    type_key="image-seg",
                    owner_id=actor_id,
                )
            )
            await db.flush()
            db.add(
                Task(
                    id=task_id,
                    project_id=project_id,
                    display_id=f"T-GC-{prefix[:12]}",
                    file_name="mask.png",
                    file_path=f"gc/{prefix}.png",
                    file_type="image",
                    status="pending",
                )
            )
            await db.flush()
            await begin_mask_content_write(db, keys[:retained])
            db.add(
                Annotation(
                    task_id=task_id,
                    project_id=project_id,
                    user_id=actor_id,
                    source="manual",
                    annotation_type="bbox",
                    tool_unit_id="region",
                    class_name="object",
                    is_active=True,
                    was_cancelled=False,
                    version=1,
                    geometry={
                        "type": "bbox",
                        "mask_sources": [
                            {"object_key": key} for key in keys[:retained]
                        ],
                    },
                )
            )
        yield {
            "resources": resources,
            "bucket": bucket,
            "s3": s3,
            "cleanup_credentials": cleanup_credentials,
            "maker": maker,
            "retained": keys[:retained],
            "orphans": keys[retained:],
            "scan_count": len(existing_keys | set(keys)),
        }
    finally:
        async with maker.begin() as db:
            for table, field, value in (
                ("annotations", "task_id", task_id),
                ("tasks", "id", task_id),
                ("projects", "id", project_id),
                ("users", "id", actor_id),
            ):
                await db.execute(
                    text(f"DELETE FROM {table} WHERE {field}=:value"), {"value": value}
                )
        for key in keys:
            s3.delete_object(Bucket=bucket, Key=key)
        # These facts belong to this disposable verifier, whose final destruction
        # also owns journal receipts and the native rows. Never target shared data.
        s3.close()


class _FixtureAgeAuthority(IndependentMaskGcDatabase):
    """S3 cannot backdate fixtures; advance only this owned destructive pass."""

    async def call(self, operation, request):
        result = await super().call(operation, request)
        if operation == "claim_mask_gc_sweep" and result.sweep is not None:
            cutoff = datetime.now(timezone.utc) + timedelta(days=1)
            async with self.session_factory.begin() as db:
                changed = await db.execute(
                    text(
                        "UPDATE mask_gc_sweeps SET cutoff=:cutoff WHERE sweep_id=:sweep AND worker_id=:worker"
                    ),
                    {
                        "cutoff": cutoff,
                        "sweep": result.sweep.sweep_id,
                        "worker": request.worker_id,
                    },
                )
                assert changed.rowcount == 1
            return c.ClaimMaskGcSweepResult(
                sweep=result.sweep.model_copy(update={"cutoff": cutoff})
            )
        return result


async def test_business_collector_previews_without_mutation_and_reconciles_bounded_deletes(
    test_engine,
    monkeypatch,
    record_property,
):
    monkeypatch.setattr(settings, "labeling_mask_gc_url", "")
    monkeypatch.setattr(settings, "labeling_mask_gc_token", "")
    monkeypatch.setattr(settings, "assets_mask_content_url", "")
    monkeypatch.setattr(settings, "assets_mask_cleanup_url", "")
    async with _inventory(test_engine) as world:
        from aap_assets_mask_content.gc_storage import S3MaskGcStore

        @contextmanager
        def bytes_owner(_storage):
            yield S3MaskGcStore(world["s3"], world["bucket"])

        monkeypatch.setattr(collection, "mask_gc_storage_owner", bytes_owner)
        async with world["maker"]() as db:
            before = await _snapshot(db)
        prefix = "raster-mask-gc/v1/"
        journals_before = (
            world["s3"]
            .list_objects_v2(Bucket=world["bucket"], Prefix=prefix)
            .get("KeyCount", 0)
        )
        preview = await collection.collect_raster_masks(
            world["maker"], c.RunRequest(dry_run=True)
        )
        assert preview.scan_complete is True and preview.scanned == world["scan_count"]
        assert preview.eligible == 0 and preview.referenced >= 2
        async with world["maker"]() as db:
            assert await _snapshot(db) == before
        assert (
            world["s3"]
            .list_objects_v2(Bucket=world["bucket"], Prefix=prefix)
            .get("KeyCount", 0)
            == journals_before
        )

        monkeypatch.setattr(
            collection, "IndependentMaskGcDatabase", _FixtureAgeAuthority
        )
        started = time.monotonic()
        totals = []
        for _ in range(3):
            async with world["maker"]() as db:
                result = await RasterMaskRetentionWorkflow(
                    db, session_factory=world["maker"]
                ).purge_unreferenced_raster_masks(delete_limit=3)
            assert result["errors"] == 0, result
            assert result["delete_claims"] <= 3
            totals.append(result["deleted"])
        assert sum(totals) == 7
        assert all(
            _object_exists(world["s3"], world["bucket"], key)
            for key in world["retained"]
        )
        assert not any(
            _object_exists(world["s3"], world["bucket"], key)
            for key in world["orphans"]
        )
        async with world["maker"]() as db:
            assert (
                await db.execute(
                    text(
                        "SELECT count(*) FROM mask_gc_key_epochs WHERE object_key=ANY(:keys) AND active_operation_id IS NOT NULL"
                    ),
                    {"keys": world["orphans"]},
                )
            ).scalar_one() == 0
        record_property("delete_budget", 3)
        record_property("cycle_deleted", str(totals))
        record_property("collection_seconds", time.monotonic() - started)
        record_property("controlled_fixture_sweep_cutoff", True)
