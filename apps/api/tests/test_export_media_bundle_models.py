"""Migration-backed delivery ownership, one-shot publication and restore fences.

The verified disposable database fixture owns each temporary schema. No provider
I/O or application services are used; all schema and row artifacts are removed.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.export_media_bundle import (
    EXPORT_MEDIA_DOCUMENTS_CHECK,
    EXPORT_MEDIA_STORAGE_REF_CHECK,
    ExportMediaBundle as Bundle,
)
from app.db.models.export_output import ExportOutput


def _migration(connection, revision="0234_export_media_bundles"):
    path = Path(__file__).parents[1] / "alembic/versions" / (revision + ".py")
    spec = importlib.util.spec_from_file_location(
        "bundle_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if connection is not None:
        module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def owned_db(test_engine):
    schema = "export_media_bundle_test_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE export_artifacts (id uuid PRIMARY KEY)")
            )
            await connection.run_sync(
                lambda sync: _migration(sync, "0224_export_output_storage").upgrade()
            )
            await connection.run_sync(lambda sync: _migration(sync).upgrade())
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def _values(**changes):
    identifier = changes.get("id", uuid.uuid4())
    created = changes.get("created_at", datetime.now(timezone.utc))
    prefix = f"lidar-export-media/objects/{identifier}/"
    key = prefix + str(uuid.uuid4()) + ".bin"
    return {
        "id": identifier,
        "project_id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "zip_output_id": uuid.uuid4(),
        "input_snapshot": {"schema": 1, "private": {"tasks": []}},
        "input_digest": "a" * 64,
        "logical_paths": {
            "schema": 1,
            "targets": {"pointmask": {"velodyne/a.pcd": key}},
        },
        "inventory": {
            "schema": 1,
            "objects": {key: {"sha256": "b" * 64, "size_bytes": 11}},
        },
        "object_count": 1,
        "size_bytes": 11,
        "storage_ref": {
            "schema": 1,
            "purpose": "export",
            "location_id": "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7",
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": "http://saved-export.test:9000",
            "bucket": "saved-export-media",
            "key_prefix": prefix,
            "storage_identity": "c" * 64,
        },
        "state": "building",
        "created_at": created,
        "expires_at": created + timedelta(hours=168),
        **changes,
    }


async def _new(db, **changes):
    row = Bundle(**_values(**changes))
    db.add(row)
    await db.flush()
    return row


async def _zip(db, bundle=None, **changes):
    identifier = changes.get("id", bundle.zip_output_id if bundle else uuid.uuid4())
    project_id = changes.get(
        "project_id", bundle.project_id if bundle else uuid.uuid4()
    )
    job_id = changes.get("job_id", bundle.job_id if bundle else uuid.uuid4())
    media = changes.get("media", "image")
    created = datetime.now(timezone.utc)
    row = ExportOutput(
        **{
            "id": identifier,
            "project_id": project_id,
            "job_id": job_id,
            "media_bundle_id": bundle.id if bundle else None,
            "media": media,
            "format": "pointmask",
            "file_count": 1,
            "sha256": "d" * 64,
            "size_bytes": 29,
            "storage_ref": {
                "schema": 1,
                "purpose": "export",
                "location_id": "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7",
                "config_revision": 1,
                "binding_generation": 1,
                "endpoint_url": "http://saved-export.test:9000",
                "bucket": "saved-export-zip",
                "object_key": f"{media}/{project_id}/{job_id}/{identifier}.zip",
                "storage_identity": "e" * 64,
            },
            "state": "pending",
            "created_at": created,
            "expires_at": bundle.expires_at
            if bundle
            else created + timedelta(hours=168),
            **changes,
        }
    )
    db.add(row)
    await db.flush()
    return row


async def _constraints(db):
    await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await db.execute(text("SET CONSTRAINTS ALL DEFERRED"))


async def _claim(db, row):
    row.lease_token = uuid.uuid4()
    row.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=3600)
    await db.flush()


async def _publish(db, row, output):
    await _claim(db, row)
    row.state, row.published_at = "ready", datetime.now(timezone.utc)
    await db.flush()
    output.state = "ready"
    await db.flush()
    await _constraints(db)


async def _reject_update(db, row, **values):
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                update(type(row))
                .where(type(row).id == row.id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await _constraints(db)


def test_models_and_frozen_migration_use_the_same_checks():
    module = _migration(None)
    assert module.revision == "0234" and module.down_revision == "0233"
    latest = _migration(None, "0248_export_location_bindings")
    assert module._REF_CHECK.strip() == latest._OLD_1.strip()
    assert latest._NEW_1.strip() == EXPORT_MEDIA_STORAGE_REF_CHECK.strip()
    assert module._DOCUMENTS_CHECK.strip() == EXPORT_MEDIA_DOCUMENTS_CHECK.strip()
    assert not Bundle.__table__.foreign_keys
    foreign_key = next(iter(ExportOutput.__table__.c.media_bundle_id.foreign_keys))
    assert foreign_key.target_fullname == "export_media_bundles.id"
    assert foreign_key.ondelete == "RESTRICT"


async def test_owner_survives_absent_business_records_with_exact_expected_inventory(
    owned_db,
):
    row = await _new(owned_db)
    await _constraints(owned_db)
    await owned_db.refresh(row)
    assert row.lease_token is None and row.published_at is None
    assert row.object_count == 1 and row.size_bytes == 11
    assert row.input_snapshot["private"] == {"tasks": []}
    assert row.expires_at - row.created_at == timedelta(hours=168)


async def test_empty_roundtrip_leaves_legacy_zip_owners_unassociated(owned_db):
    output = await _zip(owned_db)
    await _constraints(owned_db)
    connection = await owned_db.connection()

    def roundtrip(sync):
        schema = sync.scalar(text("SELECT current_schema()"))
        foreign_keys = inspect(sync).get_foreign_keys("export_outputs", schema=schema)
        assert foreign_keys[0]["options"]["ondelete"] == "RESTRICT"
        _migration(sync).downgrade()
        assert not inspect(sync).has_table("export_media_bundles", schema=schema)
        _migration(sync).upgrade()

    await connection.run_sync(roundtrip)
    assert (
        await owned_db.scalar(
            text("SELECT media_bundle_id FROM export_outputs WHERE id=:id"),
            {"id": output.id},
        )
        is None
    )


async def test_downgrade_cannot_erase_even_unclaimed_responsibility(owned_db):
    await _new(owned_db)
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="Cannot discard"):
        await connection.run_sync(lambda sync: _migration(sync).downgrade())


@pytest.mark.parametrize("state", ["ready", "retiring", "deleted"])
async def test_new_owner_cannot_skip_allocation(owned_db, state):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await _new(owned_db, state=state)


@pytest.mark.parametrize(
    "field,value",
    [
        ("lease_token", uuid.uuid4()),
        ("lease_expires_at", datetime.now(timezone.utc) + timedelta(hours=1)),
        ("published_at", datetime.now(timezone.utc)),
    ],
)
async def test_owner_cannot_arrive_with_execution_or_publication_facts(
    owned_db, field, value
):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await _new(owned_db, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "1"),
        ("purpose", "media-cache"),
        ("location_id", str(uuid.uuid4())),
        ("config_revision", 2),
        ("binding_generation", 2),
        ("endpoint_url", "http://user:secret@host"),
        ("endpoint_url", "http://host/path"),
        ("bucket", "bucket/path"),
        ("key_prefix", f"lidar-export-media/objects/{uuid.uuid4()}/"),
        ("storage_identity", "g" * 64),
        ("unexpected", "value"),
    ],
)
async def test_saved_binding_shape_and_owner_namespace_are_strict(
    owned_db, field, value
):
    values = _values()
    values["storage_ref"][field] = value
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            owned_db.add(Bundle(**values))
            await owned_db.flush()


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_key",
        "user_path",
        "invalid_sha",
        "bool_size",
        "fractional_size",
        "huge_size",
        "extra_receipt_field",
        "wrong_count",
        "wrong_total",
        "missing_inventory",
    ],
)
async def test_expected_inventory_has_exact_members_and_accounting(owned_db, mutation):
    values = _values()
    objects = values["inventory"]["objects"]
    key = next(iter(objects))
    if mutation == "foreign_key":
        objects["foreign/" + str(uuid.uuid4()) + ".bin"] = objects.pop(key)
    elif mutation == "user_path":
        objects[values["storage_ref"]["key_prefix"] + "photo.jpeg"] = objects.pop(key)
    elif mutation == "invalid_sha":
        objects[key]["sha256"] = "g" * 64
    elif mutation == "bool_size":
        objects[key]["size_bytes"] = True
    elif mutation == "fractional_size":
        objects[key]["size_bytes"] = 1.25
    elif mutation == "huge_size":
        objects[key]["size_bytes"] = 256 * 1024**2 + 1
    elif mutation == "extra_receipt_field":
        objects[key]["unfrozen"] = True
    elif mutation == "wrong_count":
        values["object_count"] = 2
    elif mutation == "wrong_total":
        values["size_bytes"] = 12
    elif mutation == "missing_inventory":
        values["inventory"] = None
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            owned_db.add(Bundle(**values))
            await owned_db.flush()


async def test_lease_is_claimed_once_and_cannot_renew_or_clear(owned_db):
    row = await _new(owned_db)
    await _claim(owned_db, row)
    await _reject_update(owned_db, row, lease_token=uuid.uuid4())
    await _reject_update(
        owned_db, row, lease_expires_at=row.lease_expires_at + timedelta(seconds=1)
    )
    await _reject_update(owned_db, row, lease_token=None, lease_expires_at=None)


async def test_lease_cannot_exceed_one_hour(owned_db):
    row = await _new(owned_db)
    await _reject_update(
        owned_db,
        row,
        lease_token=uuid.uuid4(),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )


async def test_expired_lease_cannot_publish_with_a_backdated_publication(owned_db):
    row = await _new(owned_db)
    await _zip(owned_db, row)
    await owned_db.execute(
        text(
            "UPDATE export_media_bundles SET lease_token=:token, "
            "lease_expires_at=clock_timestamp() + INTERVAL '20 milliseconds' WHERE id=:id"
        ),
        {"token": uuid.uuid4(), "id": row.id},
    )
    await owned_db.execute(text("SELECT pg_sleep(0.03)"))
    with pytest.raises(IntegrityError, match="Expired export media cannot publish"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(Bundle)
                .where(Bundle.id == row.id)
                .values(
                    state="ready",
                    published_at=row.created_at,
                )
                .execution_options(synchronize_session=False)
            )


async def test_combined_media_budget_and_exact_zero_inventory(owned_db):
    empty = _values(
        inventory={"schema": 1, "objects": {}}, object_count=0, size_bytes=0
    )
    owned_db.add(Bundle(**empty))
    await owned_db.flush()
    values = _values(object_count=97, size_bytes=97 * 256 * 1024**2)
    prefix = values["storage_ref"]["key_prefix"]
    values["inventory"]["objects"] = {
        prefix + str(uuid.uuid4()) + ".bin": {
            "sha256": "b" * 64,
            "size_bytes": 256 * 1024**2,
        }
        for _ in range(97)
    }
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            owned_db.add(Bundle(**values))
            await owned_db.flush()


async def test_two_bundle_attempts_cannot_claim_the_same_zip_identity(owned_db):
    row = await _new(owned_db)
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await _new(owned_db, zip_output_id=row.zip_output_id)


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "project_id",
        "job_id",
        "zip_output_id",
        "input_snapshot",
        "input_digest",
        "logical_paths",
        "inventory",
        "object_count",
        "size_bytes",
        "created_at",
        "expires_at",
    ],
)
async def test_frozen_inputs_accounting_and_deadline_cannot_change(owned_db, field):
    row = await _new(owned_db)
    value = deepcopy(getattr(row, field))
    if isinstance(value, uuid.UUID):
        value = uuid.uuid4()
    elif isinstance(value, dict):
        value["changed"] = True
    elif isinstance(value, datetime):
        value += timedelta(seconds=1)
    elif isinstance(value, int):
        value += 1
    else:
        value = "f" * 64
    await _reject_update(owned_db, row, **{field: value})


@pytest.mark.parametrize("mismatch", ["zip_id", "project", "job", "expiry", "video"])
async def test_pair_relationship_is_checked_when_deferred_constraints_run(
    owned_db, mismatch
):
    row = await _new(owned_db)
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            changes = {}
            if mismatch == "zip_id":
                changes["id"] = uuid.uuid4()
            elif mismatch == "project":
                changes["project_id"] = uuid.uuid4()
            elif mismatch == "job":
                changes["job_id"] = uuid.uuid4()
            elif mismatch == "expiry":
                changes["expires_at"] = row.expires_at - timedelta(seconds=1)
            else:
                changes["media"] = "video"
            await _zip(owned_db, row, **changes)
            await _constraints(owned_db)


@pytest.mark.parametrize("only", ["zip", "media"])
async def test_half_publication_cannot_commit(owned_db, only):
    row = await _new(owned_db)
    output = await _zip(owned_db, row)
    await _claim(owned_db, row)
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            if only == "zip":
                await owned_db.execute(
                    update(ExportOutput)
                    .where(ExportOutput.id == output.id)
                    .values(state="ready")
                )
            else:
                await owned_db.execute(
                    update(Bundle)
                    .where(Bundle.id == row.id)
                    .values(state="ready", published_at=datetime.now(timezone.utc))
                )
            await _constraints(owned_db)


async def test_publication_is_joint_and_association_is_permanent(owned_db):
    row = await _new(owned_db)
    output = await _zip(owned_db, row)
    await _publish(owned_db, row, output)
    await _reject_update(owned_db, output, media_bundle_id=None)
    await _reject_update(owned_db, row, published_at=None)
    await _reject_update(owned_db, row, state="building")


async def test_published_media_survives_zip_retirement_and_removal(owned_db):
    row = await _new(owned_db)
    output = await _zip(owned_db, row)
    await _publish(owned_db, row, output)
    output.state, output.retired_at = "retired", datetime.now(timezone.utc)
    await owned_db.flush()
    await owned_db.delete(output)
    await owned_db.flush()
    row.state, row.retired_at = "retiring", datetime.now(timezone.utc)
    await owned_db.flush()
    await _constraints(owned_db)
    assert row.published_at is not None
    await _reject_update(owned_db, row, state="deleted")
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text("DELETE FROM export_media_bundles WHERE id=:id"), {"id": row.id}
            )


async def test_unpublished_retirement_and_tombstone_do_not_discard_inputs(owned_db):
    row = await _new(owned_db)
    row.state, row.retired_at = "retiring", datetime.now(timezone.utc)
    await owned_db.flush()
    row.state = "deleted"
    await owned_db.flush()
    assert row.published_at is None and row.input_snapshot["private"] == {"tasks": []}
    await _reject_update(owned_db, row, state="building", retired_at=None)


async def test_restore_maps_only_physical_bucket_and_identity_including_tombstones(
    owned_db,
):
    row = await _new(owned_db)
    row.state, row.retired_at = "retiring", datetime.now(timezone.utc)
    await owned_db.flush()
    row.state = "deleted"
    await owned_db.flush()
    restored = {
        **row.storage_ref,
        "bucket": "restored-export-media",
        "storage_identity": "f" * 64,
    }
    await _reject_update(owned_db, row, storage_ref=restored)
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await owned_db.execute(
        update(Bundle).where(Bundle.id == row.id).values(storage_ref=restored)
    )
    await owned_db.flush()
    await _reject_update(
        owned_db,
        row,
        storage_ref={**restored, "endpoint_url": "http://another.test:9000"},
    )
    await _reject_update(owned_db, row, input_snapshot={"changed": True})
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='off'"))
