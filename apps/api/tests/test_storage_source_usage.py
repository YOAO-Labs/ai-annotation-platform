"""Source scale uses accepted evidence, not current storage or page contents."""

from copy import deepcopy
from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import delete, event, select

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset
from app.db.models.dataset_source import (
    DatasetScanEntry,
    DatasetSourceItem,
    DatasetScanRun,
    DatasetSourceRevision,
)
from app.schemas.dataset_source import DatasetSourceCreate
from app.services import dataset_scans as scans, filesystem_storage as fs
from app.services.dataset_references import register_entry
from app.services import dataset_source_revisions as revisions
from app.services.storage import storage_service
from app.services.storage_source_usage import read_usage
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_changes import accept_all_files, rescan
from tests.test_dataset_source_relocations import moved_candidate
from tests.test_dataset_source_revisions import candidate
from tests.test_directory_policies import object_store as object_store
from tests.test_s3_references import s3_world as s3_world, scan as s3_scan
from tests.test_sftp_references import sftp_world as sftp_world, scan as sftp_scan


async def new_source(db, actor, location, original, *, relative_path=None, mode=None):
    dataset = Dataset(
        display_id=f"usage-{uuid.uuid4().hex[:10]}",
        name="Second source dataset",
        created_by=actor.id,
    )
    db.add(dataset)
    await db.flush()
    source = await scans.create_source(
        db,
        DatasetSourceCreate(
            dataset_id=dataset.id,
            location_id=location.id,
            name="Second source",
            relative_path=relative_path
            if relative_path is not None
            else original.config["relative_path"],
            node_id=original.config["node_id"],
            cache_policy="none",
            ownership_mode=mode or original.ownership_mode,
        ),
        actor,
    )
    return dataset, source


async def duplicate_fs(db, world, **kwargs):
    dataset, source = await new_source(db, world[2], world[4], world[5], **kwargs)
    second = await rescan(db, (*world[:3], dataset, world[4], source, *world[6:]))
    await accept_all_files(db, second)
    await db.commit()
    return second


async def incomplete_legacy_head(db, world, bad):
    """Insert incomplete old facts once; immutable published receipts stay intact."""
    dataset, source = await new_source(db, world[2], world[4], world[5])
    original = await db.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == world[6].id, DatasetScanEntry.kind == "file"
        )
    )
    snapshot = deepcopy(world[6].snapshot)
    receipt = deepcopy(original.verified_content)
    if bad == "receipt":
        receipt = None
    elif bad == "sha":
        receipt["sha256"] = "f" * 64
    elif bad == "size":
        receipt["size_bytes"] = str(len(world[1]))
    elif bad == "observation":
        receipt["observation"] = []
    elif bad == "host":
        snapshot.pop("scan_host_id")
    elif bad == "numeric_host":
        snapshot["scan_host_id"] = int("1" * 64)
    elif bad == "anchor":
        snapshot["anchor"]["root"] = {"wrong": 1}
    elif bad == "path":
        snapshot["config"]["relative_path"] = ["bad"]
    run = DatasetScanRun(
        id=uuid.uuid4(),
        source_id=source.id,
        snapshot=snapshot,
        status="completed",
        complete=True,
        completed_at=world[6].completed_at,
    )
    db.add(run)
    await db.flush()
    db.add(
        DatasetScanEntry(
            run_id=run.id,
            path_digest=original.path_digest,
            relative_path=original.relative_path + "/"
            if bad == "path_tail"
            else original.relative_path,
            kind="file",
            state="observed",
            observed=deepcopy(original.observed),
            verified_content=receipt,
        )
    )
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="old.png",
        file_path=f"legacy/{uuid.uuid4()}",
        file_type="image",
        file_size=len(world[1]),
    )
    db.add(item)
    await db.flush()
    asset = DatasetAsset(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        media_type="image",
        size_bytes=len(world[1]),
        verification_state="verified",
        checksum_algorithm="sha256",
        checksum_value=original.verified_content["sha256"],
    )
    db.add(asset)
    await db.flush()
    revision = DatasetSourceRevision(
        id=uuid.uuid4(),
        source_id=source.id,
        path_digest=original.path_digest,
        run_id=run.id,
        revision_number=1,
        item_id_at_accept=item.id,
        asset_id_at_accept=asset.id,
        dataset_item_id=item.id,
        asset_id=asset.id,
        content_sha256=asset.checksum_value,
        ownership_mode="reference",
        owns_item=True,
        accepted_at=datetime.now(timezone.utc),
    )
    db.add(revision)
    await db.flush()
    db.add(
        DatasetSourceItem(
            current_revision_id=revision.id,
            source_id=source.id,
            path_digest=original.path_digest,
            run_id=run.id,
            dataset_item_id=item.id,
            asset_id=asset.id,
            content_sha256=asset.checksum_value,
            ownership_mode="reference",
            owns_item=True,
        )
    )
    await db.commit()


@pytest.fixture
async def accepted(db_session, content_world):
    await accept_all_files(db_session, content_world)
    await db_session.commit()
    return content_world


async def test_two_datasets_share_one_file_with_one_sql_and_no_source_io(
    db_session, accepted, httpx_client, super_admin, monkeypatch
):
    db, world = db_session, accepted
    await duplicate_fs(db, world)

    def forbidden(*args, **kwargs):
        pytest.fail("Source usage must not access storage")

    monkeypatch.setattr(fs, "scan_directory", forbidden)
    monkeypatch.setattr(fs, "content_file", forbidden)
    monkeypatch.setattr(storage_service.client, "_make_api_call", forbidden)
    statements = []
    await db.connection()

    def seen(*args):
        statements.append(args[2])

    event.listen(db.bind.sync_engine, "before_cursor_execute", seen)
    try:
        usage = await read_usage(db, world[4], world[2])
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", seen)
    assert len(statements) == 1
    assert usage["reference_count"] == 2
    assert usage["file_count"] == usage["duplicate_reference_count"] == 1
    assert usage["total_size_bytes"] == len(world[1])
    assert usage["coverage"] == "complete" and not usage["is_estimate"]
    url = f"/api/v1/storage-locations/{world[4].id}/source-summary"
    first = (
        await httpx_client.get(url + "?limit=1", headers=headers(super_admin))
    ).json()
    second = (
        await httpx_client.get(url + "?limit=1&offset=1", headers=headers(super_admin))
    ).json()
    assert first["items"][0]["source_id"] != second["items"][0]["source_id"]
    for key in (
        "reference_count",
        "file_count",
        "known_size_bytes",
        "total_size_bytes",
    ):
        assert first["usage"][key] == second["usage"][key] == usage[key]
    public = str(first["usage"])
    assert not any(
        token in public
        for token in [str(world[0]), "anchor", "bucket", "endpoint", "图片"]
    )


async def test_nested_sources_and_equal_content_at_distinct_paths(db_session, accepted):
    db, world = db_session, accepted
    nested = world[0] / "nested"
    nested.mkdir()
    (nested / "same.png").write_bytes(world[1])
    world = await rescan(db, world)
    await accept_all_files(db, world)
    await duplicate_fs(db, world, relative_path="source/nested")
    usage = await read_usage(db, world[4], world[2])
    assert usage["reference_count"] == 3 and usage["file_count"] == 2
    assert usage["duplicate_reference_count"] == 1
    assert usage["total_size_bytes"] == 2 * len(world[1])


async def test_revision_settings_and_inactive_state_preserve_saved_scale(
    db_session, accepted
):
    db, world = db_session, accepted
    world[4].config_revision += 1
    world[5].config_revision += 1
    world[4].enabled = False
    world[5].enabled = False
    world[5].unregistered_at = datetime.now(timezone.utc)
    (world[0] / "图片 空格.png").unlink()
    await db.commit()
    usage = await read_usage(db, world[4], world[2])
    assert usage["inactive_reference_count"] == 1
    assert usage["total_size_bytes"] == len(world[1])
    assert usage["observed_from"] is not None


async def test_empty_and_deleted_heads_do_not_reappear_from_history(
    db_session, content_world
):
    db, world = db_session, content_world
    assert (await read_usage(db, world[4], world[2]))["total_size_bytes"] == 0
    await accept_all_files(db, world)
    await db.execute(delete(DatasetSourceItem))
    await db.commit()
    result = await read_usage(db, world[4], world[2])
    assert result["reference_count"] == result["total_size_bytes"] == 0
    assert result["coverage"] == "complete"


@pytest.mark.parametrize(
    "bad",
    [
        "receipt",
        "sha",
        "size",
        "observation",
        "host",
        "anchor",
        "path",
        "path_tail",
        "numeric_host",
    ],
)
async def test_unknown_duplicate_does_not_disappear_from_coverage(
    db_session, accepted, bad
):
    db, world = db_session, accepted
    await incomplete_legacy_head(db, world, bad)
    usage = await read_usage(db, world[4], world[2])
    assert usage["reference_count"] == 2
    assert usage["total_size_bytes"] is None
    assert usage["unknown_reference_count"] >= 1


async def test_incomparable_hosts_are_unknown_not_two_physical_files(
    db_session, accepted
):
    db, world = db_session, accepted
    second = await duplicate_fs(db, world)
    second[6].snapshot = {**second[6].snapshot, "scan_host_id": "b" * 64}
    await db.commit()
    result = await read_usage(db, world[4], world[2])
    assert result["total_size_bytes"] is None
    assert result["file_count"] == 0 and result["unknown_reference_count"] == 2
    assert result["incomparable_file_count"] == 1


async def test_different_paths_do_not_make_incomparable_mounts_comparable(
    db_session, accepted
):
    db, world = db_session, accepted
    (world[0] / "nested").mkdir()
    (world[0] / "nested" / "other.png").write_bytes(world[1])
    second = await duplicate_fs(db, world, relative_path="source/nested")
    second[6].snapshot = {**second[6].snapshot, "scan_host_id": "b" * 64}
    await db.commit()
    result = await read_usage(db, world[4], world[2])
    assert result["total_size_bytes"] is None and result["file_count"] == 0
    assert result["incomparable_file_count"] == result["unknown_reference_count"] == 2


async def test_revalidation_and_move_use_current_head_not_original_reference(
    db_session, accepted
):
    db, world = db_session, accepted
    world, run, change, payload = await candidate(db, world, "red")
    await revisions.accept(
        db, run.id, change["path_digest"], payload, world[2], revalidate=True
    )
    world, run, _, new, payload = await moved_candidate(db, world)
    await revisions.accept(
        db, run.id, new["path_digest"], payload, world[2], relocate=True
    )
    await db.commit()
    usage = await read_usage(db, world[4], world[2])
    assert usage["reference_count"] == usage["file_count"] == 1
    assert usage["total_size_bytes"] == len(world[1])


async def test_conflicting_current_heads_exclude_group_but_not_other_files(
    db_session, accepted
):
    db, world = db_session, accepted
    (world[0] / "separate.png").write_bytes(world[1])
    world = await rescan(db, world)
    await accept_all_files(db, world)
    await duplicate_fs(db, world)
    world, run, change, payload = await candidate(db, world)
    await revisions.accept(db, run.id, change["path_digest"], payload, world[2])
    await db.commit()
    usage = await read_usage(db, world[4], world[2])
    assert usage["reference_count"] == 4
    assert usage["conflict_file_count"] == 1 and usage["unknown_reference_count"] == 2
    assert usage["file_count"] == 1 and usage["known_size_bytes"] == len(world[1])
    assert usage["total_size_bytes"] is None and usage["coverage"] == "partial"


async def test_scope_is_applied_before_aggregation(db_session, accepted, project_admin):
    db, world = db_session, accepted
    second = await duplicate_fs(db, world)
    world[4].config = {**world[4].config, "scope": "global"}
    world[3].created_by = project_admin[0].id
    await db.commit()
    limited = await read_usage(db, world[4], project_admin[0])
    all_sources = await read_usage(db, world[4], world[2])
    assert limited["reference_count"] == 1 and all_sources["reference_count"] == 2
    assert limited["duplicate_reference_count"] == 0
    assert limited["total_size_bytes"] == len(world[1])
    assert second[3].created_by != project_admin[0].id


async def test_managed_copy_heads_are_source_scale_too(
    db_session, content_world, object_store
):
    db, world = db_session, content_world
    world[5].ownership_mode = "managed_copy"
    await db.commit()
    await accept_all_files(db, world)
    await duplicate_fs(db, world, mode="managed_copy")
    result = await read_usage(db, world[4], world[2])
    assert result["reference_count"] == 2 and result["file_count"] == 1
    assert result["total_size_bytes"] == len(world[1])


@pytest.mark.parametrize(
    "first_version,second_version,copies",
    [("first-version", "second-version", 2), (None, "null", 1)],
)
async def test_s3_exact_keys_and_versions_have_distinct_identity(
    db_session, s3_world, first_version, second_version, copies
):
    db = db_session
    memory, actor, dataset, location, source = s3_world
    keys = ["a//b.png", "a/./b.png", "a/../b.png", "a\\b.png", " edge +%2F.png "]
    memory.objects = {f"root/collection/{key}": memory.data for key in keys}
    memory.version = first_version
    run = await s3_scan(db, s3_world)
    entries = list(
        await db.scalars(
            select(DatasetScanEntry).where(
                DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
            )
        )
    )
    for entry in entries:
        await register_entry(
            db, run.id, entry.path_digest, actor, node_id="worker:content@test"
        )
    memory.version = second_version
    dataset2, source2 = await new_source(db, actor, location, source)
    second = await s3_scan(db, (memory, actor, dataset2, location, source2))
    for entry in await db.scalars(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == second.id, DatasetScanEntry.kind == "file"
        )
    ):
        await register_entry(
            db, second.id, entry.path_digest, actor, node_id="worker:content@test"
        )
    await db.commit()
    calls = len(memory.calls)
    result = await read_usage(db, location, actor)
    assert result["file_count"] == copies * len(keys)
    assert result["total_size_bytes"] == copies * len(keys) * len(memory.data)
    assert result["includes_versions"] and len(memory.calls) == calls


async def test_sftp_qualified_addresses_deduplicate(db_session, sftp_world):
    db = db_session
    root, memory, actor, dataset, location, source = sftp_world
    for current in [source, (await new_source(db, actor, location, source))[1]]:
        run = await sftp_scan(db, (root, memory, actor, dataset, location, current))
        for entry in await db.scalars(
            select(DatasetScanEntry).where(
                DatasetScanEntry.run_id == run.id,
                DatasetScanEntry.kind == "file",
                DatasetScanEntry.state == "observed",
            )
        ):
            await register_entry(
                db, run.id, entry.path_digest, actor, node_id="worker:content@test"
            )
    await db.commit()
    result = await read_usage(db, location, actor)
    assert result["reference_count"] == 2 * result["file_count"]
    assert result["duplicate_reference_count"] == result["file_count"] > 0
    assert result["coverage"] == "complete"
