"""Database constraints preserve new location and provider receipt facts."""

from copy import deepcopy
from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.avatar_upload import AvatarUpload
from app.services import avatar_uploads
from tests.test_avatar_storage import external_reference


def owner(**changes):
    token = uuid.uuid4().hex
    ref = external_reference(object_key=f"team/_aap_managed/avatars/{token}.webp")
    return AvatarUpload(
        **{
            "token": token,
            "owner_id": uuid.uuid4(),
            "storage_ref": ref,
            "sha256": "a" * 64,
            "size_bytes": 11,
            "state": "pending",
            **changes,
        }
    )


async def test_new_location_record_keeps_its_prefix_and_version_after_retirement(
    db_session,
):
    row = owner()
    db_session.add(row)
    await db_session.flush()
    saved = deepcopy(row.storage_ref)
    receipt = await avatar_uploads._save_write_version(
        db_session, avatar_uploads._snapshot(row), "actual-provider-version"
    )
    assert receipt.object_version_id == "actual-provider-version"
    await db_session.refresh(row)
    row.state = "ready"
    await db_session.flush()
    row.state, row.retired_at = "retired", datetime.now(timezone.utc)
    await db_session.flush()
    assert (
        row.storage_ref == saved and row.object_version_id == "actual-provider-version"
    )


@pytest.mark.parametrize("change", ["replace", "clear", "late"])
async def test_version_receipt_cannot_be_replaced_cleared_or_added_after_ready(
    db_session, change
):
    row = owner()
    db_session.add(row)
    await db_session.flush()
    if change != "late":
        row.object_version_id = "first-actual-version"
        await db_session.flush()
    else:
        row.state = "ready"
        await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            row.object_version_id = None if change == "clear" else "another-version"
            await db_session.flush()


async def test_new_location_reference_remains_immutable(db_session):
    row = owner()
    db_session.add(row)
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            row.storage_ref = {**row.storage_ref, "binding_generation": 3}
            await db_session.flush()


@pytest.mark.parametrize(
    "field,value",
    [
        ("binding_generation", 1),
        ("binding_generation", True),
        ("config_revision", 1.5),
        ("location_id", "not-a-registered-uuid"),
        ("object_key", "../_aap_managed/avatars/"),
        ("object_key", "unsafe//_aap_managed/avatars/"),
    ],
)
async def test_database_rejects_noncanonical_new_location_facts(
    db_session, field, value
):
    row = owner()
    row.storage_ref = {
        **row.storage_ref,
        field: value + row.token + ".webp" if field == "object_key" else value,
    }
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(row)
            await db_session.flush()
