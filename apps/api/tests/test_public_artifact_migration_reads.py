"""Saved public owners read exact migrated content without moving their receipts."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest

from app.db.enums import UserRole
from app.services import audit_archive_reads, audit_archive_storage
from app.services import avatar, avatar_storage, avatar_uploads
from app.services import bug_attachment_storage, bug_attachment_uploads
from app.services import storage_access
from app.services import storage_migration_artifact_reads as migrated_reads
from app.services import storage_migration_copy as copy
from app.services import storage_migration_read_publications as publications
from app.services.managed_original_retirement import managed_storage_identity
from tests.test_audit_archive_storage import archive, audit_row
from tests.test_bug_attachment_storage import RAW, record as attachment_record
from tests.test_storage_migration_artifact_reads import (
    SDK,
    Target,
    response,
    route as artifact_route,
)


def saved(kind, version="source-version"):
    now = datetime.now(timezone.utc)
    if kind == "avatar":
        raw = b"normalized avatar"
        row = SimpleNamespace(
            token="a" * 32,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            object_version_id=version,
            state="ready",
        )
        owner, purpose, key = avatar_storage, "avatars", row.token + ".webp"
    elif kind == "attachment":
        raw = RAW
        row = attachment_record(
            object_version_id=version,
            created_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=179),
            bound_report_id=uuid.UUID(int=20),
            state="ready",
        )
        owner, purpose = bug_attachment_storage, "bug-reports"
        key = owner.key_for(row.owner_id, row.id, row.mime_type)
    else:
        row, raw = archive([audit_row(1), audit_row(2)])
        row.state, row.published_at = "ready", now
        owner, purpose = audit_archive_storage, "audit-archive"
        key = owner.key_for(row.year, row.month, row.id)
    row.storage_ref = {
        "schema": 2,
        "purpose": purpose,
        "location_id": str(uuid.UUID(int=3)),
        "config_revision": 1,
        "binding_generation": 2,
        "endpoint_url": "http://source.test:9000",
        "bucket": "saved-a",
        "object_key": "tenant/_aap_managed/" + purpose + "/" + key,
        "storage_identity": managed_storage_identity(
            "http://source.test:9000", "saved-a"
        ),
    }
    resolved = owner.resolve(row)
    selected = replace(
        artifact_route(),
        source_location_id=uuid.UUID(row.storage_ref["location_id"]),
        source_endpoint_url=resolved.ref["endpoint_url"],
        source_bucket=resolved.bucket,
        source_object_key=resolved.object_key,
        source_version_id=version,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
    )
    selected = replace(
        selected,
        target_content_sha256=copy.digest(copy.content_target(response(selected))),
    )
    return row, raw, selected


class Physical(SDK):
    def __init__(self, selected, raw):
        super().__init__(selected)
        self.raw = raw

    def get_object(self, **params):
        self.calls.append(("get", params))
        body = io.BytesIO(self.raw)
        self.bodies.append(body)
        return response(self.route, Body=body, **deepcopy(self.changes))


def read(kind, client, row, selected):
    if kind == "avatar":
        return avatar.read_from_client(
            client, row, response_version_id=selected.target_version_id
        )
    if kind == "attachment":
        return bug_attachment_storage.read_from_client(
            client, row, response_version_id=selected.target_version_id
        )
    return audit_archive_storage.read_from_client(client, row, limit=1, offset=1)


READ_CASES = [
    (kind, version)
    for kind in ("avatar", "attachment")
    for version in (None, "null", "source-version")
] + [("archive", "source-version")]


@pytest.mark.parametrize("kind,version", READ_CASES)
def test_saved_owner_reads_real_b_version_without_changing_a_receipt(kind, version):
    row, raw, selected = saved(kind, version)
    original = deepcopy(row.__dict__)
    sdk = Physical(selected, raw)
    with migrated_reads.client(selected, Target(sdk)) as client:
        result = read(kind, client, row, selected)
    if kind == "avatar":
        assert result == (raw, '"' + row.sha256 + '"')
    elif kind == "attachment":
        assert result == raw
    else:
        assert result["total"] == 3
        assert [item["id"] for item in result["items"]] == [2]
    assert row.__dict__ == original
    assert sdk.calls
    assert all(
        params["Bucket"] == selected.target_bucket
        and params["Key"] == selected.target_object_key
        and params["VersionId"] == selected.target_version_id
        for _, params in sdk.calls
    )
    assert all(body.closed for body in sdk.bodies)
    assert sdk.closes == 1


@pytest.mark.parametrize(
    "kind,stage", [("avatar", 0)] + [("attachment", i) for i in range(3)]
)
def test_physical_response_version_is_checked_independently_of_saved_source(
    kind, stage
):
    row, raw, selected = saved(kind)
    sdk = Physical(selected, raw)
    original_head, original_get = sdk.head_object, sdk.get_object
    responses = 0

    def observed(call, params):
        nonlocal responses
        result = call(**params)
        if responses == stage:
            result["VersionId"] = selected.source_version_id
        responses += 1
        return result

    sdk.head_object = lambda **params: observed(original_head, params)
    sdk.get_object = lambda **params: observed(original_get, params)
    error = (
        avatar_storage.AvatarStorageError
        if kind == "avatar"
        else bug_attachment_storage.BugAttachmentStorageError
    )
    with pytest.raises(error):
        read(kind, sdk, row, selected)
    assert all(body.closed for body in sdk.bodies)
    assert all(
        params["VersionId"] == selected.source_version_id for _, params in sdk.calls
    )


@pytest.mark.parametrize("kind", ["avatar", "attachment", "archive"])
def test_mapped_read_still_hashes_complete_saved_content_and_closes_body(kind):
    row, raw, selected = saved(kind)
    sdk = Physical(selected, raw[:-1] + bytes([raw[-1] ^ 1]))
    error = {
        "avatar": avatar_storage.AvatarStorageError,
        "attachment": bug_attachment_storage.BugAttachmentStorageError,
        "archive": audit_archive_storage.AuditArchiveStorageError,
    }[kind]
    with pytest.raises(error), migrated_reads.client(selected, Target(sdk)) as client:
        read(kind, client, row, selected)
    assert sdk.bodies and all(body.closed for body in sdk.bodies)
    assert sdk.closes == 1


def mapped_entry(monkeypatch, kind, version="source-version", *, after_read=None):
    row, raw, selected = saved(kind, version)
    db = SimpleNamespace(commit=AsyncMock(), scalar=AsyncMock(return_value=row))
    lookup = AsyncMock(return_value=selected)
    final = AsyncMock()
    monkeypatch.setattr(publications, "lookup", lookup)
    monkeypatch.setattr(publications, "recheck", final)
    unavailable = AsyncMock(side_effect=AssertionError("unmapped A transport"))
    monkeypatch.setattr(storage_access, "run", unavailable)
    monkeypatch.setattr(storage_access, "check", unavailable)
    owner = {
        "avatar": avatar_storage,
        "attachment": bug_attachment_storage,
        "archive": audit_archive_storage,
    }[kind]
    monkeypatch.setattr(owner, "io_target", unavailable)
    sdk = Physical(selected, raw)
    purposes = []

    async def routed(actual_db, route, purpose, callback, **kwargs):
        assert actual_db is db and route is selected
        purposes.append(purpose)
        if kwargs.get("on_acquired") is not None:
            await kwargs["on_acquired"](SimpleNamespace(revision=77))
        with migrated_reads.client(selected, Target(sdk)) as client:
            result = callback(client)
        if after_read is not None:
            after_read(row)
        return result

    monkeypatch.setattr(migrated_reads, "run", routed)
    actor = SimpleNamespace(id=uuid.UUID(int=12), role=UserRole.SUPER_ADMIN)
    monkeypatch.setattr(
        bug_attachment_uploads, "reauthorize", AsyncMock(return_value=actor)
    )
    if kind == "avatar":

        async def call():
            return await avatar_uploads.read(db, row.token)
    elif kind == "attachment":
        report = SimpleNamespace(
            id=row.bound_report_id,
            reporter_id=row.owner_id,
            attachments=[
                bug_attachment_uploads.attachment(row).model_dump(by_alias=True)
            ],
            screenshot_url=None,
        )
        monkeypatch.setattr(bug_attachment_uploads, "capture_authority", AsyncMock())
        monkeypatch.setattr(
            bug_attachment_uploads, "locked_report", AsyncMock(return_value=report)
        )
        monkeypatch.setattr(
            bug_attachment_uploads, "_for_key", AsyncMock(return_value=row)
        )
        monkeypatch.setattr(
            bug_attachment_uploads, "_locked", AsyncMock(return_value=row)
        )

        async def call():
            return await bug_attachment_uploads.read(
                db, actor, object(), report.id, bug_attachment_uploads.object_key(row)
            )
    else:
        proof = SimpleNamespace(
            original=SimpleNamespace(key_proof=None),
            jwt=audit_archive_reads.JwtProof(
                datetime.now(timezone.utc) + timedelta(hours=1), 0, None
            ),
        )
        monkeypatch.setattr(
            audit_archive_reads, "capture_authority", AsyncMock(return_value=proof)
        )
        monkeypatch.setattr(
            audit_archive_reads, "_ready_month", AsyncMock(return_value=row)
        )

        async def call():
            return await audit_archive_reads.read_archive_rows(
                db, actor, object(), row.year, row.month, limit=1, offset=1
            )

    return SimpleNamespace(
        call=call,
        row=row,
        raw=raw,
        selected=selected,
        db=db,
        lookup=lookup,
        final=final,
        purposes=purposes,
        sdk=sdk,
        actor=actor,
    )


@pytest.mark.parametrize("kind,version", READ_CASES)
async def test_business_entry_qualifies_full_owner_and_source_version(
    monkeypatch, kind, version
):
    case = mapped_entry(monkeypatch, kind, version)
    original = deepcopy(case.row.__dict__)
    result = await case.call()
    assert result is not None
    selected = case.selected
    case.lookup.assert_awaited_once_with(
        case.db,
        source_location_id=selected.source_location_id,
        endpoint_url=selected.source_endpoint_url,
        bucket=selected.source_bucket,
        object_key=selected.source_object_key,
        sha256=case.row.sha256,
        size_bytes=case.row.size_bytes,
        source_version_id=(
            publications.ANY_VERSION
            if kind == "archive" or version is None
            else version
        ),
    )
    assert case.row.__dict__ == original
    assert case.purposes == [
        "avatar_read"
        if kind == "avatar"
        else (
            bug_attachment_uploads.read_purpose(case.row.id)
            if kind == "attachment"
            else audit_archive_reads.read_purpose(case.row.id)
        )
    ]
    if kind != "avatar":
        case.final.assert_awaited_once_with(case.db, selected, lock=True)


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("attachment", "bound_report_id", None),
        ("attachment", "state", "retired"),
        ("attachment", "sha256", "f" * 64),
        ("attachment", "expires_at", datetime(2020, 1, 1, tzinfo=timezone.utc)),
        ("archive", "state", "pending"),
        ("archive", "job_id", uuid.UUID(int=999)),
        ("archive", "published_at", None),
    ],
)
async def test_mapped_delivery_keeps_original_business_row_bookends(
    monkeypatch, kind, field, value
):
    case = mapped_entry(
        monkeypatch, kind, after_read=lambda row: setattr(row, field, value)
    )
    with pytest.raises(HTTPException):
        await case.call()
    assert case.sdk.bodies and all(body.closed for body in case.sdk.bodies)
    case.final.assert_not_awaited()


@pytest.mark.parametrize("kind", ["attachment", "archive"])
async def test_mapped_delivery_rechecks_report_visibility_and_archive_role(
    monkeypatch, kind
):
    def revoked(row):
        case.actor.id = uuid.UUID(int=999)
        case.actor.role = UserRole.ANNOTATOR

    case = mapped_entry(monkeypatch, kind, after_read=revoked)
    with pytest.raises(HTTPException) as error:
        await case.call()
    assert error.value.status_code == 403
    assert case.sdk.bodies and all(body.closed for body in case.sdk.bodies)
    case.final.assert_not_awaited()


async def test_mapped_archive_delivery_still_checks_presented_jwt_expiry(monkeypatch):
    def expired(row):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        monkeypatch.setattr(audit_archive_reads, "now", lambda: future)

    case = mapped_entry(monkeypatch, "archive", after_read=expired)
    with pytest.raises(HTTPException) as error:
        await case.call()
    assert error.value.status_code == 403
    assert error.value.detail == {"code": "audit_archive_authority_revoked"}
    assert case.sdk.bodies and all(body.closed for body in case.sdk.bodies)
    case.final.assert_not_awaited()
