"""Location diagnostics preserve independent, authorized, read-only evidence."""

from datetime import datetime, timedelta, timezone
import uuid
import hashlib

import pytest
from sqlalchemy import delete, event, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAsset, StorageBinding
from app.db.models.dataset_source import (
    DatasetReference,
    DatasetRegistrationRun,
    DatasetScanRun,
    DatasetScanEntry,
    DatasetSource,
    DatasetSourceItem,
    DatasetSourcePreview,
)
from app.services import dataset_source_revisions as revisions
from app.services import filesystem_storage as fs
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.directory_previews import SPEC
from app.services.storage import storage_service
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_access import managed_world as managed_world
from tests.test_dataset_source_changes import (
    accept_all_files,
    picture,
    preflight,
    rescan,
)
from tests.test_dataset_source_relocations import moved_candidate
from tests.test_dataset_source_revisions import candidate


async def read_summary(client, world, actor, query=""):
    response = await client.get(
        f"/api/v1/storage-locations/{world[4].id}/source-summary{query}",
        headers=headers(actor),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def seed_preview(db, mapping, state="ready"):
    row = DatasetSourcePreview(
        source_id=mapping.source_id,
        path_digest=mapping.path_digest,
        asset_id=mapping.asset_id,
        source_sha256=mapping.content_sha256,
        spec=SPEC,
        policy_revision=1,
        state=state,
        location_id=LEGACY_LOCATION_ID,
        bucket=storage_service.media_cache_bucket,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.fixture
async def evidence_world(db_session, content_world):
    db, world = db_session, content_world
    for name in ("gone.png", "same.png"):
        (world[0] / name).write_bytes(picture("blue"))
    world = await rescan(db, world)
    await accept_all_files(db, world)
    await ensure_legacy_bindings(db)
    mappings = list(await db.scalars(select(DatasetSourceItem)))
    previews = [
        await seed_preview(db, mappings[0]),
        await seed_preview(db, mappings[1], "failed"),
    ]
    world[5].cache_policy = "preview"
    await db.commit()
    return world, mappings, previews


async def test_original_and_preview_are_independent_and_get_has_no_source_io(
    db_session, evidence_world, httpx_client, super_admin, monkeypatch
):
    db = db_session
    world, _, _ = evidence_world
    (world[0] / "图片 空格.png").write_bytes(picture("green"))
    (world[0] / "gone.png").unlink()
    world = await rescan(db, world)
    comparison = await preflight(db, world)

    def forbidden(*args, **kwargs):
        raise AssertionError("Summary must not access storage")

    monkeypatch.setattr(fs, "probe_directory", forbidden)
    monkeypatch.setattr(fs, "scan_directory", forbidden)
    monkeypatch.setattr(storage_service, "list_objects", forbidden)
    monkeypatch.setattr(fs, "content_file", forbidden)
    monkeypatch.setattr(storage_service.client, "_make_api_call", forbidden)
    row = (await read_summary(httpx_client, world, super_admin))["items"][0][
        "diagnostics"
    ]
    ref = row["references"]
    assert ref["scope"] == "stored_reference_items" and ref["total"] == 3
    assert ref["original_counts"] == dict(
        verified=1, missing=1, changed=1, preview_only=0, unavailable=0, unverified=0
    )
    assert ref["preview_counts"] == dict(
        ready=1, failed=1, unrecorded=1, not_requested=0, blocked=0
    )
    assert (
        ref["observed_from"] <= ref["observed_through"] and ref["preview_recorded_at"]
    )
    assert row["scan"]["recent"]["complete"]
    assert row["preflight"]["recent"]["run_id"] == str(comparison.id)
    assert row["preflight"]["recent"]["complete"]
    assert str(world[0]) not in str(row)


@pytest.mark.parametrize("mismatch", ["sha", "asset", "spec", "destination"])
async def test_preview_records_must_match_current_content_and_destination(
    db_session, evidence_world, httpx_client, super_admin, mismatch
):
    world, mappings, previews = evidence_world
    preview = previews[0]
    if mismatch == "sha":
        preview.source_sha256 = "0" * 64
    elif mismatch == "asset":
        preview.asset_id = mappings[2].asset_id
    elif mismatch == "spec":
        preview.spec = "old-spec"
    elif mismatch == "destination":
        preview.bucket = "obsolete"
    await db_session.commit()
    ref = (await read_summary(httpx_client, world, super_admin))["items"][0][
        "diagnostics"
    ]["references"]
    assert ref["total"] == 3
    assert ref["preview_counts"]["ready"] == 0
    assert ref["preview_counts"]["failed"] == 1
    assert ref["preview_counts"]["unrecorded"] == 2
    assert ref["original_counts"]["verified"] == 3


async def test_legacy_preview_evidence_does_not_follow_unrelated_current_bindings(
    db_session, evidence_world, httpx_client, super_admin
):
    world, _, _ = evidence_world
    await db_session.execute(
        delete(StorageBinding).where(StorageBinding.purpose == "avatars")
    )
    await db_session.commit()
    ref = (await read_summary(httpx_client, world, super_admin))["items"][0][
        "diagnostics"
    ]["references"]
    assert ref["preview_counts"]["ready"] == 1
    assert ref["preview_counts"]["failed"] == 1


@pytest.mark.parametrize("policy", ["source", "location", "disabled", "unregistered"])
async def test_policy_and_authority_block_previews_without_fabricating_failure(
    db_session, evidence_world, httpx_client, super_admin, policy
):
    world, _, _ = evidence_world
    if policy == "source":
        world[5].cache_policy = "none"
        world[5].policy_revision += 1
    elif policy == "location":
        world[4].config = {**world[4].config, "cache_policy": "none"}
    elif policy == "disabled":
        world[4].enabled = False
    else:
        world[5].unregistered_at = datetime.now(timezone.utc)
    await db_session.commit()
    ref = (await read_summary(httpx_client, world, super_admin))["items"][0][
        "diagnostics"
    ]["references"]
    assert (
        ref["preview_counts"][
            "not_requested" if policy in ("source", "location") else "blocked"
        ]
        == 3
    )
    assert ref["preview_counts"]["failed"] == 0
    if policy == "source":
        world[5].cache_policy = "preview"
        world[5].policy_revision += 1
        await db_session.commit()
        restored = (await read_summary(httpx_client, world, super_admin))["items"][0][
            "diagnostics"
        ]["references"]
        assert restored["preview_counts"]["ready"] == 1


async def test_history_never_inherits_new_revision_preview_and_move_uses_current_head(
    db_session, content_world, httpx_client, super_admin
):
    db, world = db_session, content_world
    await accept_all_files(db, world)
    world, run, change, payload = await candidate(db, world)
    result = await revisions.accept(
        db, run.id, change["path_digest"], payload, world[2]
    )
    # Move the new identity, retaining the immutable reference's original path.
    world, run, old, new, payload = await moved_candidate(db, world)
    moved = await revisions.accept(
        db, run.id, new["path_digest"], payload, world[2], relocate=True
    )
    assert moved["item_id"] == result["item_id"]
    await ensure_legacy_bindings(db)
    mapping = await db.scalar(
        select(DatasetSourceItem).where(
            DatasetSourceItem.dataset_item_id == result["item_id"]
        )
    )
    await seed_preview(db, mapping)
    world[5].cache_policy = "preview"
    await db.commit()
    ref = (await read_summary(httpx_client, world, super_admin))["items"][0][
        "diagnostics"
    ]["references"]
    assert ref["total"] == 2
    assert (
        ref["original_counts"]["changed"] == 1
        and ref["original_counts"]["verified"] == 1
    )
    assert (
        ref["preview_counts"]["ready"] == 1 and ref["preview_counts"]["unrecorded"] == 1
    )


async def test_unverified_or_absent_asset_is_counted_without_matching_preview(
    db_session, evidence_world, httpx_client, super_admin
):
    db, (world, _, _) = db_session, evidence_world
    for with_asset in (True, False):
        item = DatasetItem(
            dataset_id=world[3].id,
            file_name="legacy.png",
            file_path=f"old/{uuid.uuid4()}",
        )
        db.add(item)
        await db.flush()
        if with_asset:
            db.add(
                DatasetAsset(
                    id=uuid.uuid4(),
                    dataset_item_id=item.id,
                    media_type="image/png",
                    verification_state="unverified",
                )
            )
        path = f"unverified-{with_asset}.png"
        digest = hashlib.sha256(path.encode()).hexdigest()
        db.add(
            DatasetScanEntry(
                run_id=world[6].id,
                path_digest=digest,
                relative_path=path,
                kind="file",
                state="observed",
                observed={},
            )
        )
        await db.flush()
        db.add(
            DatasetReference(
                dataset_item_id=item.id,
                source_id=world[5].id,
                run_id=world[6].id,
                path_digest=digest,
                source_revision=1,
                location_revision=1,
            )
        )
    await db.commit()
    ref = (await read_summary(httpx_client, world, super_admin))["items"][0][
        "diagnostics"
    ]["references"]
    assert ref["total"] == 5
    assert (
        ref["preview_counts"]["ready"] == 1 and ref["preview_counts"]["unrecorded"] == 3
    )


async def test_active_old_runs_and_last_complete_survive_new_failure_and_job_retention(
    db_session, content_world, httpx_client, super_admin
):
    db, world = db_session, content_world
    source, original = world[5:7]
    original_id = original.id
    now = datetime.now(timezone.utc)
    failed_scan = DatasetScanRun(
        source_id=source.id,
        snapshot=original.snapshot,
        status="failed",
        created_at=now + timedelta(hours=1),
        error_code="private-path-do-not-expose",
    )
    active_scan = DatasetScanRun(
        source_id=source.id,
        snapshot=original.snapshot,
        status="running",
        created_at=now - timedelta(days=1),
    )
    db.add_all([failed_scan, active_scan])
    await db.flush()
    snapshot = {**original.snapshot, "policy_revision": source.policy_revision}
    active = DatasetRegistrationRun(
        scan_run_id=original.id,
        source_id=source.id,
        status="running",
        snapshot=snapshot,
        created_at=now - timedelta(days=1),
        comparison_complete=False,
    )
    failed = DatasetRegistrationRun(
        scan_run_id=failed_scan.id,
        source_id=source.id,
        status="failed",
        snapshot=snapshot,
        created_at=now + timedelta(hours=1),
    )
    successful = DatasetRegistrationRun(
        scan_run_id=active_scan.id,
        source_id=source.id,
        status="completed",
        snapshot=snapshot,
        comparison_complete=True,
        completed_at=now,
    )
    db.add_all([active, failed, successful])
    await db.execute(delete(AsyncJob).where(AsyncJob.id == original.async_job_id))
    await db.commit()
    row = (await read_summary(httpx_client, world, super_admin))["items"][0][
        "diagnostics"
    ]
    assert row["scan"]["recent"]["run_id"] == str(active_scan.id)
    assert row["scan"]["last_complete"]["run_id"] == str(original_id)
    assert row["scan"]["last_complete"]["job_id"] is None
    assert row["preflight"]["recent"]["run_id"] == str(active.id)
    assert (
        row["preflight"]["recent"]["scan_run_id"]
        != row["scan"]["recent"]["scan_run_id"]
    )
    assert row["preflight"]["last_complete"]["run_id"] == str(successful.id)
    assert row["scan"]["recent"]["matches_current_config"]
    assert row["preflight"]["recent"]["matches_current_config"]
    assert "private-path" not in str(row) and "snapshot" not in str(row)
    source.config_revision += 1
    world[4].config_revision += 1
    await db.commit()
    stale = (await read_summary(httpx_client, world, super_admin))["items"][0][
        "diagnostics"
    ]
    assert not stale["scan"]["last_complete"]["matches_current_config"]
    assert not stale["preflight"]["recent"]["matches_current_config"]
    # Once active attempts stop, the most recently created failed attempts remain visible.
    active_scan.status = active.status = "cancelled"
    await db.commit()
    recent = (await read_summary(httpx_client, world, super_admin))["items"][0][
        "diagnostics"
    ]
    assert (
        recent["scan"]["recent"]["status"]
        == recent["preflight"]["recent"]["status"]
        == "failed"
    )
    assert recent["scan"]["last_complete"] and recent["preflight"]["last_complete"]


async def test_managed_copy_has_no_reference_counts_and_queries_are_page_bounded(
    db_session, managed_world, project_admin, httpx_client
):
    db, (world, _) = db_session, managed_world
    for index in range(4):
        db.add(
            DatasetSource(
                dataset_id=world[3].id,
                location_id=world[4].id,
                name=f"empty-{index}",
                config=world[5].config,
            )
        )
    await db.commit()
    queries = []
    engine = db.bind.sync_engine

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        single = await read_summary(httpx_client, world, project_admin, "?limit=1")
        count = len(queries)
        queries.clear()
        page = await read_summary(httpx_client, world, project_admin, "?limit=5")
        assert len(queries) <= count
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(single["items"]) == 1 and len(page["items"]) == 5
    for row in page["items"]:
        ref = row["diagnostics"]["references"]
        assert (
            ref["total"]
            == sum(ref["original_counts"].values())
            == sum(ref["preview_counts"].values())
            == 0
        )
        assert ref["observed_through"] is None
