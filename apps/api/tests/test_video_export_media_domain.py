"""Schema-four identity, bounded metadata, migration and shared owner fences."""

import ast
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models.export_media_bundle import (
    EXPORT_MEDIA_VIDEO_CHECK,
    ExportMediaBundle as Bundle,
)
from app.services import export_media_bundles as bundles
from app.services import export_media_retirement as retirement
from app.services import export_media_storage as physical
from app.services import export_outputs, export_storage
from app.services.exporting.lidar_sources import canonical_identity
from tests import test_export_media_bundle_models as models
from tests import test_export_media_retirement as retirement_tests
from tests.test_export_media_bundles import FakeDB, digest
from tests.test_export_media_storage import Client, ok, record

REVISION = "0237_export_video_media"
legacy_db = models.owned_db


def video_inputs(
    *,
    tasks=2,
    repeat=True,
    size=512 * 1024**2 + 1,
    targets=("mot",),
    owner_kind="dataset_asset",
):
    project, shared = uuid.uuid4(), uuid.uuid4()
    data = {
        "schema": 4,
        "project_id": str(project),
        "batch_id": None,
        "task_ids": None,
        "project": {"id": str(project), "data_type": "video"},
        "targets": list(targets),
        "options": {},
        "tasks": [],
        "references": {},
        "media": [],
        "segments": [],
        "quality_runs": [],
        "probe_contract": 1,
    }
    logical = {target: {} for target in targets}
    for index in range(tasks):
        task, content = uuid.uuid4(), shared if repeat else uuid.uuid4()
        source = {
            "owner_kind": owner_kind,
            "content_id": str(content),
            "sha256": "a" * 64,
            "size_bytes": size,
            "media_type": "video",
            "item_id": str(shared) if owner_kind == "dataset_asset" else None,
            "relation": {"task_id": str(task), "project_id": str(project), "links": []},
            "bucket": "saved-originals",
            "object_key": f"originals/{content}/same.mp4",
            "location_id": str(physical.LEGACY_LOCATION_ID),
            "endpoint_url": physical._runtime_endpoint(),
            "location_revision": 1,
            "maintenance_revision": 1,
            "ownership_facts": {"id": str(content), "sha256": "a" * 64},
            "item_facts": {"file_type": "video"},
            "replica_facts": {"asset_id": str(content), "role": "managed_original"},
            "location_facts": {"id": str(physical.LEGACY_LOCATION_ID)},
        }
        data["tasks"].append(
            {
                "id": str(task),
                "project_id": str(project),
                "file_type": "video",
                "dataset_item_id": source["item_id"],
                "file_path": source["object_key"],
            }
        )
        key = digest(canonical_identity(source))
        data["media"].append(
            {
                "task_id": str(task),
                "role": "video",
                "link_id": None,
                "source_key": key,
                "source": source,
            }
        )
        for paths in logical.values():
            paths[f"tasks/{task}/same.mp4"] = key
    return project, data, logical


async def allocate(db=None, **kwargs):
    db = db or FakeDB()
    project, data, logical = video_inputs(**kwargs)
    receipt = await bundles.allocate(
        db,
        project_id=project,
        job_id=uuid.uuid4(),
        input_snapshot=data,
        input_digest=digest(data),
        logical_sources=logical,
    )
    return db, receipt, data, logical


@pytest.mark.parametrize("owner_kind", ["dataset_asset", "task_original"])
async def test_video_full_identity_deduplicates_without_losing_task_bindings(
    owner_kind,
):
    db, receipt, data, logical = await allocate(owner_kind=owner_kind)
    assert receipt.input_schema == 4 and receipt.object_count == 1
    assert receipt.size_bytes == 512 * 1024**2 + 1
    assert len(receipt.input_snapshot["media"]) == 2
    assert len(bundles.source_objects(receipt, logical)) == 1
    receipt.input_snapshot["media"].clear()
    assert len(receipt.input_snapshot["media"]) == 2
    db.commit.assert_not_awaited()
    assert data["media"][0]["source"]["sha256"] == "a" * 64


@pytest.mark.parametrize("target", sorted(bundles.VIDEO_TARGETS))
async def test_all_existing_video_targets_have_exact_original_coverage(target):
    _, receipt, _, logical = await allocate(targets=(target,))
    assert set(receipt.logical_paths["targets"]) == {target}
    assert len(bundles.source_objects(receipt, logical)) == 1


async def test_same_sha_different_saved_bucket_keeps_distinct_physical_owners():
    project, data, logical = video_inputs()
    entry = data["media"][1]
    old = entry["source_key"]
    entry["source"]["bucket"] = "another-original-bucket"
    entry["source_key"] = digest(canonical_identity(entry["source"]))
    paths = logical["mot"]
    paths[next(path for path in paths if entry["task_id"] in path)] = entry[
        "source_key"
    ]
    receipt = await bundles.allocate(
        FakeDB(),
        project_id=project,
        job_id=uuid.uuid4(),
        input_snapshot=data,
        input_digest=digest(data),
        logical_sources=logical,
    )
    mapping = bundles.source_objects(receipt, logical)
    assert len(mapping) == 2 and mapping[old] != mapping[entry["source_key"]]
    assert receipt.size_bytes == 2 * (512 * 1024**2 + 1)


@pytest.mark.parametrize(
    "fault",
    [
        "schema_bool",
        "probe_bool",
        "probe_revision",
        "reference",
        "project_type",
        "task_type",
        "role",
        "link",
        "owner",
        "owner_object",
        "media_type",
        "relation",
        "unregistered_sha",
        "bool_size",
        "empty",
        "oversize",
        "missing",
        "duplicate",
        "task_overflow",
    ],
)
async def test_invalid_video_inputs_allocate_no_responsibility(fault):
    project, data, logical = video_inputs(tasks=1)
    entry, task = data["media"][0], data["tasks"][0]
    source = entry["source"]
    if fault == "schema_bool":
        data["schema"] = True
    elif fault == "probe_bool":
        data["probe_contract"] = True
    elif fault == "probe_revision":
        data["probe_contract"] = 2
    elif fault == "reference":
        data["references"] = {task["id"]: {"size": 3}}
    elif fault == "project_type":
        data["project"]["data_type"] = "image"
    elif fault == "task_type":
        task["file_type"] = "image"
    elif fault == "role":
        entry["role"] = "image"
    elif fault == "link":
        entry["link_id"] = str(uuid.uuid4())
    elif fault == "owner":
        source["owner_kind"] = "dataset_auxiliary"
    elif fault == "owner_object":
        source["owner_kind"] = {}
    elif fault == "media_type":
        source["media_type"] = "image"
    elif fault == "relation":
        source["relation"]["project_id"] = str(uuid.uuid4())
    elif fault == "unregistered_sha":
        source["sha256"] = "b" * 64
    elif fault == "bool_size":
        source["size_bytes"] = True
    elif fault == "empty":
        source["size_bytes"] = 0
    elif fault == "oversize":
        source["size_bytes"] = physical.MAX_VIDEO_OBJECT_BYTES + 1
    elif fault == "missing":
        data["media"].clear()
    elif fault == "duplicate":
        data["media"].append(deepcopy(entry))
    else:
        data["tasks"] *= 1001
    db = FakeDB()
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.allocate(
            db,
            project_id=project,
            job_id=uuid.uuid4(),
            input_snapshot=data,
            input_digest=digest(data),
            logical_sources=logical,
        )
    assert not db.rows
    db.flush.assert_not_awaited()


async def test_video_unique_byte_and_task_budgets_are_independent():
    _, receipt, _, _ = await allocate(tasks=4, repeat=False, size=5 * 1024**3)
    assert receipt.size_bytes == 20 * 1024**3 and receipt.object_count == 4
    with pytest.raises(bundles.ExportMediaBundleError, match="budget"):
        await allocate(tasks=5, repeat=False, size=5 * 1024**3)
    _, repeated, _, _ = await allocate(tasks=1000, repeat=True, size=5 * 1024**3)
    assert repeated.object_count == 1 and repeated.size_bytes == 5 * 1024**3


@pytest.mark.parametrize("schema", [1, 2, 3])
def test_legacy_physical_profiles_never_accept_video_sized_objects(schema):
    row = record(input_schema=schema)
    entry = next(iter(row.inventory["objects"].values()))
    entry["size_bytes"] = physical.MAX_OBJECT_BYTES + 1
    with pytest.raises(physical.ExportMediaStorageError):
        physical.validate_inventory(row)
    assert physical.object_limit(row) == 256 * 1024**2


@pytest.mark.parametrize("scalar,document", [(4, 3), (3, 4), (True, 4), (4.0, 4)])
def test_conflicting_or_noninteger_scalar_never_selects_a_larger_profile(
    scalar, document
):
    row = record(input_schema=scalar, input_snapshot={"schema": document})
    with pytest.raises(physical.ExportMediaStorageError):
        physical.validate_inventory(row)


def test_video_listing_and_retirement_support_large_receipts_without_loading_bytes():
    row = record(input_schema=4)
    key, receipt = next(iter(row.inventory["objects"].items()))
    receipt["size_bytes"] = 5 * 1024**3
    client = Client()
    client.add(key, b"placeholder for listing-only test")
    client.list_objects_v2 = lambda **_: ok(
        Contents=[{"Key": key, "Size": receipt["size_bytes"]}]
        if client.rows[key]
        else [],
        IsTruncated=False,
    )
    assert physical.list_objects(client, row) == {key: 5 * 1024**3}
    assert physical.delete_inventory(client, row) == 1
    assert not client.rows[key]
    assert not any(name in {"get", "head"} for name, _ in client.calls)


@pytest.mark.parametrize("limit", ["object", "total", "count", "zero"])
def test_video_physical_inventory_has_its_own_limits(limit):
    row = record(input_schema=4)
    key = next(iter(row.inventory["objects"]))
    if limit == "object":
        row.inventory["objects"][key]["size_bytes"] = 5 * 1024**3 + 1
    elif limit == "zero":
        row.inventory["objects"][key]["size_bytes"] = 0
    else:
        row.inventory["objects"] = {
            physical.prefix_for(row.id) + str(uuid.uuid4()) + ".bin": {
                "sha256": "a" * 64,
                "size_bytes": 5 * 1024**3 if limit == "total" else 1,
            }
            for _ in range(5 if limit == "total" else 1001)
        }
    with pytest.raises(physical.ExportMediaStorageError):
        physical.validate_inventory(row)


def _function_sql(name):
    path = Path(__file__).parents[1] / "alembic/versions/0234_export_media_bundles.py"
    tree = ast.parse(path.read_text())
    return next(
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and "CREATE FUNCTION " + name + "()" in call.args[0].value
    )


def test_migration_preserves_every_other_guard_and_roundtrips():
    migration = models._migration(None, REVISION)
    empty = models._migration(None, "0236_export_media_empty_raw")
    guard = empty._definition(_function_sql("guard_export_media_bundle"), upgrade=True)
    pair = _function_sql("check_export_media_pair")
    assert migration.down_revision == "0236"
    assert migration.VIDEO_CHECK == EXPORT_MEDIA_VIDEO_CHECK
    for definition, replacements in [
        (guard, migration._GUARD_REPLACEMENTS),
        (pair, migration._PAIR_REPLACEMENTS),
    ]:
        changed = migration._definition(definition, replacements, upgrade=True)
        assert migration._definition(changed, replacements, upgrade=False) == definition
        for invalid in [None, "missing", definition + definition, changed]:
            with pytest.raises(RuntimeError, match="Unexpected"):
                migration._definition(invalid, replacements, upgrade=True)


@pytest.mark.parametrize("exists", [True, False])
def test_downgrade_checks_all_video_responsibilities_before_replacing_functions(
    monkeypatch, exists
):
    migration = models._migration(None, REVISION)
    connection = SimpleNamespace(scalar=Mock(return_value=exists))
    executed, replaced = [], Mock()
    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(
            get_bind=lambda: connection,
            execute=executed.append,
            drop_constraint=Mock(),
        ),
    )
    monkeypatch.setattr(migration, "_replace", replaced)
    if exists:
        with pytest.raises(RuntimeError, match="video media responsibility"):
            migration.downgrade()
        replaced.assert_not_called()
    else:
        migration.downgrade()
        assert replaced.call_count == 2
    assert len(executed) == 1 and "ACCESS EXCLUSIVE" in executed[0]
    sql = str(connection.scalar.call_args.args[0])
    assert "input_snapshot->'schema' = '4'" in sql
    assert "state" not in sql and "object_count" not in sql


@pytest.fixture
async def video_db(legacy_db):
    connection = await legacy_db.connection()
    for revision in ["0236_export_media_empty_raw", REVISION]:
        await connection.run_sync(
            lambda sync, r=revision: models._migration(sync, r).upgrade()
        )
    await legacy_db.commit()
    yield legacy_db


async def test_database_video_projection_and_pair_publish_atomically(video_db):
    db = video_db
    _, receipt, _, _ = await allocate(db, tasks=1, size=5 * 1024**3)
    await db.commit()
    claimed = await bundles.claim(db, receipt.id)
    await db.commit()
    owner = await db.get(Bundle, receipt.id)
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await models._zip(db, owner, media="image", created_at=owner.created_at)
            await models._constraints(db)
    output = await models._zip(db, owner, media="video", created_at=owner.created_at)
    await db.commit()
    await bundles.publish(db, claimed)
    output.state = "ready"
    await db.commit()
    assert output.expires_at == owner.expires_at and owner.published_at is not None
    await db.close()
    locked = await retirement.lock_owner(db, receipt.id, inventory=True)
    assert locked.input_schema == 4
    assert {"input_snapshot", "logical_paths"} <= inspect(locked).unloaded
    retired = retirement.snapshot(locked)
    assert retired.input_schema == 4 and physical.object_limit(retired) == 5 * 1024**3


@pytest.mark.parametrize("state", ["building", "deleted"])
async def test_database_even_empty_video_owner_prevents_downgrade(video_db, state):
    db = video_db
    _, receipt, _, _ = await allocate(db, tasks=0)
    await db.commit()
    if state == "deleted":
        await db.execute(
            update(Bundle)
            .where(Bundle.id == receipt.id)
            .values(
                state="retiring",
                retired_at=bundles.now(),
            )
        )
        await db.execute(
            update(Bundle).where(Bundle.id == receipt.id).values(state="deleted")
        )
        await db.commit()
    connection = await db.connection()
    with pytest.raises(RuntimeError, match="video media responsibility"):
        await connection.run_sync(
            lambda sync: models._migration(sync, REVISION).downgrade()
        )
    await db.rollback()


async def test_database_schema_four_cannot_raise_legacy_caps_or_restore_immutable_facts(
    video_db,
):
    db = video_db
    legacy = models._values()
    next(iter(legacy["inventory"]["objects"].values()))["size_bytes"] = (
        256 * 1024**2 + 1
    )
    legacy["size_bytes"] = 256 * 1024**2 + 1
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(Bundle(**legacy))
            await db.flush()
    _, receipt, _, _ = await allocate(db, tasks=1)
    await db.commit()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                update(Bundle)
                .where(Bundle.id == receipt.id)
                .values(input_snapshot={"schema": 3})
            )
    await db.rollback()


@pytest.mark.parametrize("fault", ["object", "total", "count", "role", "probe", "zero"])
async def test_database_schema_four_enforces_budgets_and_binding_shape(video_db, fault):
    fake, receipt, _, _ = await allocate(tasks=4, repeat=False, size=5 * 1024**3)
    pending = fake.rows[receipt.id]
    values = {
        column.name: deepcopy(getattr(pending, column.name))
        for column in Bundle.__table__.columns
    }
    objects = values["inventory"]["objects"]
    first = next(iter(objects.values()))
    if fault == "object":
        first["size_bytes"] += 1
    elif fault == "total":
        objects[physical.prefix_for(receipt.id) + str(uuid.uuid4()) + ".bin"] = {
            "sha256": "a" * 64,
            "size_bytes": 1,
        }
    elif fault == "count":
        objects.clear()
        objects.update(
            {
                physical.prefix_for(receipt.id) + str(uuid.uuid4()) + ".bin": {
                    "sha256": "a" * 64,
                    "size_bytes": 1,
                }
                for _ in range(1001)
            }
        )
    elif fault == "role":
        values["input_snapshot"]["media"][0]["role"] = "image"
    elif fault == "probe":
        values["input_snapshot"]["probe_contract"] = True
    else:
        first["size_bytes"] = 0
    values["object_count"] = len(objects)
    values["size_bytes"] = sum(entry["size_bytes"] for entry in objects.values())
    with pytest.raises(IntegrityError):
        async with video_db.begin_nested():
            video_db.add(Bundle(**values))
            await video_db.flush()


async def test_database_schema_four_keeps_legacy_zero_byte_inventory_semantics(
    video_db,
):
    values = models._values()
    next(iter(values["inventory"]["objects"].values()))["size_bytes"] = 0
    values["size_bytes"] = 0
    row = Bundle(**values)
    video_db.add(row)
    await video_db.flush()
    assert (
        await video_db.scalar(select(Bundle.input_schema).where(Bundle.id == row.id))
        == 1
    )


@pytest.mark.parametrize("fault", [None, "image", "lidar", "both", "boolean", "scalar"])
async def test_retirement_replacement_accepts_only_exact_exclusive_video_proof(fault):
    owner = retirement_tests.owner()
    replacement = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=owner.project_id,
        job_id=owner.job_id,
        input_digest="c" * 64,
        input_schema=4,
    )
    proof = {"schema": 4, "bundle_id": str(replacement.id), "input_digest": "c" * 64}
    payload = {"video_media": proof}
    if fault == "image":
        payload = {"image_media": proof}
    elif fault == "lidar":
        payload = {"lidar_media": proof}
    elif fault == "both":
        payload["image_media"] = proof
    elif fault == "boolean":
        proof["schema"] = True
    elif fault == "scalar":
        replacement.input_schema = 4.0
    job = SimpleNamespace(
        kind="export", status="running", project_id=owner.project_id, payload=payload
    )
    assert await retirement.attempt_ended(
        retirement_tests.PolicyDB(job, replacement), owner
    ) is (fault is None)


async def test_small_scalar_claim_fence_checks_schema_again():
    db, receipt, _, _ = await allocate(tasks=1)
    claimed = await bundles.claim(db, receipt.id)
    assert (await bundles.check_claim(db, claimed)).input_schema == 4
    db.rows[receipt.id].input_schema = 3
    with pytest.raises(bundles.ExportMediaBundleError):
        await bundles.check_claim(db, claimed)


async def test_creation_checks_video_pair_profile_without_a_current_bucket_guess():
    db, receipt, _, _ = await allocate(tasks=0)
    owner = db.rows[receipt.id]
    owner.lease_token = uuid.uuid4()
    owner.lease_expires_at = bundles.now() + timedelta(minutes=5)
    facts = dict(
        bundle_id=owner.id,
        identifier=owner.zip_output_id,
        project_id=owner.project_id,
        job_id=owner.job_id,
        media="video",
        started=owner.created_at,
        expires=owner.expires_at,
    )
    export_outputs._creation_valid(owner, **facts)
    with pytest.raises(export_storage.ExportStorageError):
        export_outputs._creation_valid(owner, **{**facts, "media": "image"})


async def test_database_projection_works_on_original_0234_schema_without_new_columns(
    legacy_db,
):
    row = await models._new(legacy_db)
    await legacy_db.commit()
    projected = await legacy_db.scalar(
        select(Bundle.input_schema).where(Bundle.id == row.id)
    )
    assert type(projected) is int and projected == 1
    await legacy_db.close()
    loaded = await retirement.lock_owner(legacy_db, row.id, inventory=True)
    assert loaded.input_schema == 1
    assert {"input_snapshot", "logical_paths"} <= inspect(loaded).unloaded
