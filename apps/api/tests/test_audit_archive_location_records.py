"""Saved audit targets retain content ownership without a current-bucket fallback."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid
import pytest
from sqlalchemy.exc import IntegrityError
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.db.models.dataset_asset import StorageLocation
from app.services import (
    audit_archive_storage as storage,
    audit_archive_storage_target as targets,
)
from app.services.managed_original_retirement import managed_storage_identity
from tests.test_audit_archive_storage import archive, record, reference


def external_reference(**changes):
    endpoint = "https://registered.example.test"
    return {
        **reference(),
        "schema": 2,
        "location_id": str(uuid.UUID(int=55)),
        "config_revision": 3,
        "binding_generation": 2,
        "endpoint_url": endpoint,
        "object_key": "team/图片/_aap_managed/audit-archive/"
        + storage.key_for(2026, 1, uuid.UUID(int=11)),
        "storage_identity": managed_storage_identity(endpoint, "archive-original"),
        **changes,
    }


def owner(**changes):
    item = record()
    item.id = uuid.uuid4()
    item.storage_ref = external_reference(
        object_key="team/_aap_managed/audit-archive/"
        + storage.key_for(item.year, item.month, item.id)
    )
    values = {name: getattr(item, name) for name in storage._FACTS}
    values.update(storage_ref=item.storage_ref, **changes)
    if values["state"] == "ready":
        values["published_at"] = datetime.now(timezone.utc)
    return AuditArchiveOutput(**values)


async def test_schema2_keeps_location_and_content_immutable(db_session):
    row = owner()
    saved = deepcopy(row.storage_ref)
    db_session.add(row)
    await db_session.flush()
    row.state = "ready"
    row.published_at = datetime.now(timezone.utc)
    await db_session.flush()
    assert row.storage_ref == saved
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            row.storage_ref = {**saved, "location_id": str(uuid.uuid4())}
            await db_session.flush()


@pytest.mark.parametrize(
    "field,value",
    [
        ("binding_generation", 1),
        ("config_revision", 1.5),
        ("location_id", "invalid"),
        ("object_key", "../_aap_managed/audit-archive/"),
        ("object_key", "unsafe//_aap_managed/audit-archive/"),
    ],
)
async def test_schema2_rejects_noncanonical_physical_facts(db_session, field, value):
    row = owner()
    logical = storage.key_for(row.year, row.month, row.id)
    row.storage_ref = {
        **row.storage_ref,
        field: value + logical if field == "object_key" else value,
    }
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(row)
            await db_session.flush()


async def test_current_credentials_keep_saved_address_after_rotation(monkeypatch):
    from tests.test_avatar_storage_target import connection_target
    from app.services import connector_guard

    ref = external_reference()
    location, connection = connection_target()
    location.id = uuid.UUID(ref["location_id"])
    location.config_revision = 99
    connection.revision = 100
    connection.config.update(
        endpoint=ref["endpoint_url"], bucket=ref["bucket"], base_prefix="team/图片/"
    )
    connection.secret_enc = b"rotated-encrypted-secret"

    async def get(model, _identifier, **_kwargs):
        return location if model is StorageLocation else connection

    db = SimpleNamespace(get=AsyncMock(side_effect=get))
    monkeypatch.setattr(connector_guard, "get_allowlist", AsyncMock(return_value=[]))
    row = record(storage_ref=ref)
    target = await storage.io_target(db, row)
    assert (
        target._access.secret_enc == b"rotated-encrypted-secret"
        and row.storage_ref == ref
    )


@pytest.mark.parametrize("operation", ["read", "write"])
def test_external_owner_has_no_global_client_fallback(operation):
    row, raw = archive()
    row.storage_ref = external_reference()
    import io

    with pytest.raises(storage.AuditArchiveStorageError):
        getattr(storage, operation)(
            row, *([io.BytesIO(raw)] if operation == "write" else [])
        )


def test_archive_probe_uses_saved_namespace_and_archive_error():
    from tests.test_avatar_storage_target import Objects
    from app.services.managed_s3_target import Target

    objects = Objects("version")
    target = Target(
        uuid.uuid4(),
        1,
        1,
        "https://example.test",
        "target",
        targets.managed_prefix("team"),
        purpose="audit-archive",
        _access=objects,
    )
    assert targets.probe_target(target)["conditional_write"] and not objects.objects
    assert all(
        "_aap_managed/audit-archive/" in params["Key"] for _, params in objects.calls
    )
    objects.corrupt_read = True
    with pytest.raises(storage.AuditArchiveStorageError):
        targets.probe_target(target)


def test_external_write_and_read_use_only_the_saved_target():
    import io
    from tests.test_audit_archive_storage import Client, audit_row
    from app.services.managed_s3_target import Target

    row, raw = archive([audit_row()])
    row.storage_ref = external_reference()
    client = Client(raw)
    target = Target(
        uuid.UUID(row.storage_ref["location_id"]),
        3,
        3,
        row.storage_ref["endpoint_url"],
        row.storage_ref["bucket"],
        "team/图片/_aap_managed/audit-archive/",
        purpose="audit-archive",
        _legacy_client=client,
    )
    storage.write(row, io.BytesIO(raw), target=target)
    page = storage.read(row, target=target)
    assert page["total"] == 2
    assert all(
        params["Bucket"] == row.storage_ref["bucket"]
        and params["Key"] == row.storage_ref["object_key"]
        for _, params in client.calls
    )
    assert all(body.closed for body in client.bodies)
