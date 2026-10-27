"""New report attachment targets retain immutable physical facts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy.exc import IntegrityError
from app.db.models.bug_attachment_upload import BugAttachmentUpload
from app.db.models.dataset_asset import StorageLocation
from app.services import bug_attachment_storage as storage
from app.services import bug_attachment_storage_target as targets
from app.services.managed_original_retirement import managed_storage_identity
from tests.test_bug_attachment_storage import RAW, record, reference, KEY


def external_reference(**changes):
    endpoint = "https://registered.example.test"
    return {
        **reference(),
        "schema": 2,
        "location_id": str(uuid.UUID(int=55)),
        "config_revision": 3,
        "binding_generation": 2,
        "endpoint_url": endpoint,
        "object_key": "team/图片/_aap_managed/bug-reports/" + KEY,
        "storage_identity": managed_storage_identity(endpoint, "attachments-original"),
        **changes,
    }


def owner(**changes):
    identifier, actor = uuid.uuid4(), uuid.uuid4()
    logical = storage.key_for(actor, identifier, "image/png")
    created = datetime.now(timezone.utc)
    return BugAttachmentUpload(
        **{
            "id": identifier,
            "owner_id": actor,
            "storage_ref": external_reference(
                object_key="team/_aap_managed/bug-reports/" + logical
            ),
            "sha256": "a" * 64,
            "size_bytes": 11,
            "mime_type": "image/png",
            "file_name": "test.png",
            "created_at": created,
            "expires_at": created + timedelta(days=180),
            "state": "pending",
            **changes,
        }
    )


async def test_schema2_keeps_location_version_and_state(db_session):
    row = owner()
    saved = deepcopy(row.storage_ref)
    db_session.add(row)
    await db_session.flush()
    row.object_version_id = "actual-provider-version"
    await db_session.flush()
    row.state = "ready"
    await db_session.flush()
    row.state, row.retired_at = "retired", datetime.now(timezone.utc)
    await db_session.flush()
    assert (
        row.storage_ref == saved and row.object_version_id == "actual-provider-version"
    )


@pytest.mark.parametrize("change", ["replace", "clear", "late"])
async def test_provider_receipt_is_write_once_while_pending(db_session, change):
    row = owner()
    db_session.add(row)
    await db_session.flush()
    if change == "late":
        row.state = "ready"
    else:
        row.object_version_id = "first-version"
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            row.object_version_id = None if change == "clear" else "second-version"
            await db_session.flush()


@pytest.mark.parametrize(
    "field,value",
    [
        ("binding_generation", 1),
        ("config_revision", 1.5),
        ("location_id", "invalid"),
        ("object_key", "../_aap_managed/bug-reports/"),
        ("object_key", "unsafe//_aap_managed/bug-reports/"),
    ],
)
async def test_schema2_rejects_noncanonical_physical_facts(db_session, field, value):
    row = owner()
    logical = storage.key_for(row.owner_id, row.id, row.mime_type)
    row.storage_ref = {
        **row.storage_ref,
        field: value + logical if field == "object_key" else value,
    }
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(row)
            await db_session.flush()


async def test_current_credentials_do_not_rewrite_saved_physical_identity(monkeypatch):
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


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_external_owner_has_no_global_client_fallback(operation):
    row = record(storage_ref=external_reference())
    with pytest.raises(storage.BugAttachmentStorageError):
        getattr(storage, operation)(row, *([RAW] if operation == "write" else []))


def test_bug_probe_uses_its_own_namespace_and_error_type():
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
        purpose="bug-reports",
        _access=objects,
    )
    assert targets.probe_target(target)["conditional_write"]
    assert not objects.objects
    assert all(
        "_aap_managed/bug-reports/" in params["Key"] for _, params in objects.calls
    )
    objects.corrupt_read = True
    with pytest.raises(storage.BugAttachmentStorageError):
        targets.probe_target(target)
