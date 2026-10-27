"""Additive annotation targets preserve legacy rows and refuse destructive rollback."""

from copy import deepcopy
import uuid

import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import IntegrityError

from app.services import annotations_storage as storage
from app.services.managed_s3_target import Target
from tests.test_comment_attachment_storage_migration import create as comment
from tests.test_guide_asset_storage_migration import create as guide
from tests.test_raster_object_storage_migration import create as raster
from tests.test_task_originals import migration, original_db as original_db


async def test_migration_roundtrip_preserves_legacy_family_addresses(original_db):
    db = original_db
    rows = [await comment(db), await guide(db), await raster(db)]
    before = [deepcopy(row.storage_ref) for row in rows]
    connection = await db.connection()

    def roundtrip(sync):
        module = migration(sync, "0253")
        assert module.down_revision == "0252"
        module.downgrade()
        assert "storage_ref" not in {
            col["name"] for col in inspect(sync).get_columns("task_originals")
        }
        module.upgrade()
        assert "storage_ref" in {
            col["name"] for col in inspect(sync).get_columns("task_originals")
        }

    await connection.run_sync(roundtrip)
    for row, ref in zip(rows, before):
        await db.refresh(row)
        assert row.storage_ref == ref and ref["schema"] == 1


@pytest.mark.parametrize("family", ["comment", "guide", "raster"])
async def test_each_new_family_reference_blocks_destructive_downgrade(
    original_db, family
):
    db = original_db
    identifier, scope = uuid.uuid4(), uuid.uuid4()
    logical = (
        f"comment-attachments/{scope}/{identifier}"
        if family == "comment"
        else f"projects/{scope}/guide/{identifier}"
        if family == "guide"
        else f"raster-masks/objects/{identifier}.json"
    )
    target = Target(
        uuid.uuid4(),
        3,
        7,
        "https://objects.example.test",
        "saved-annotations",
        storage.managed_prefix("公司"),
        purpose="annotations",
    )
    ref = storage.make_ref(target, 2, target.key_prefix + logical)
    fields = {"id": identifier, "storage_ref": ref}
    if family == "comment":
        fields["annotation_id"] = scope
    elif family == "guide":
        fields["project_id"] = scope
    row = await {"comment": comment, "guide": guide, "raster": raster}[family](
        db, **fields
    )
    connection = await db.connection()
    async with connection.begin_nested() as savepoint:
        with pytest.raises(
            IntegrityError, match="Saved annotation-file responsibility"
        ):
            await connection.run_sync(lambda sync: migration(sync, "0253").downgrade())
        await savepoint.rollback()
    await db.refresh(row)
    assert row.storage_ref == ref
    await db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    changed = {**ref, "bucket": "retargeted", "storage_identity": "f" * 64}
    with pytest.raises(IntegrityError, match="immutable"):
        async with db.begin_nested():
            await db.execute(
                update(type(row))
                .where(type(row).id == row.id)
                .values(storage_ref=changed)
            )


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "forbidden"},
        {"schema": True},
        {"binding_generation": 0},
        {"key_prefix": "elsewhere/"},
        {"object_key": "elsewhere/file"},
    ],
)
async def test_closed_schema_two_check_rejects_forged_comment_addresses(
    original_db, change
):
    identifier, annotation = uuid.uuid4(), uuid.uuid4()
    logical = f"comment-attachments/{annotation}/{identifier}"
    ref = storage.legacy_ref("saved-annotations", logical) | change
    with pytest.raises(IntegrityError, match="ck_comment_attachment_storage_ref"):
        async with original_db.begin_nested():
            await comment(
                original_db, id=identifier, annotation_id=annotation, storage_ref=ref
            )


async def test_task_creation_schema_two_blocks_downgrade_before_owner_exists(
    original_db,
):
    from tests.test_task_originals import allocated

    _, allocation = await allocated(original_db)
    assert allocation["configuration"]["original_ref"]["purpose"] == "annotations"
    connection = await original_db.connection()
    async with connection.begin_nested() as savepoint:
        with pytest.raises(
            IntegrityError, match="Saved annotation-file responsibility"
        ):
            await connection.run_sync(lambda sync: migration(sync, "0253").downgrade())
        await savepoint.rollback()
    assert await original_db.scalar(text("SELECT count(*) FROM task_originals")) == 0
