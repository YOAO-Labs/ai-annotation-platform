"""Only reviewed new bug_attachment allocations move; saved objects keep their owners."""

from datetime import datetime, timedelta, timezone
import uuid

from cryptography.fernet import Fernet
from jose import jwt
import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.core.crypto import encrypt_secret
from app.db.models.audit_log import AuditLog
from app.db.models.dataset_asset import StorageBinding
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import bug_attachment_binding_change as change
from app.services.dataset_assets import (
    LEGACY_LOCATION_ID,
    ensure_legacy_bindings,
    validate_legacy_bindings,
)
from app.services.filesystem_storage import FilesystemStorageError
from app.services.storage_location import register_connection
from tests.test_storage_locations_api import headers
from tests.test_storage_maintenance_impact import key_headers

BASE = "/api/v1/storage-locations/bindings/bug-reports"


@pytest.fixture
async def world(db_session, super_admin, monkeypatch, test_engine):
    db = db_session
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    await ensure_legacy_bindings(db)
    connection = StorageConnection(
        id=uuid.uuid4(),
        name="bug_attachment destination",
        kind="s3",
        scope="global",
        config={
            "endpoint": "http://127.0.0.1:9000",
            "bucket": "bug_attachment-target",
            "base_prefix": "company",
            "use_ssl": False,
        },
        secret_enc=encrypt_secret(
            {"access_key": "private-access", "secret_key": "private-secret"}
        ),
        created_by=super_admin[0].id,
    )
    db.add(connection)
    await db.flush()
    target = await register_connection(db, connection)
    calls = []

    def probe(target):
        calls.append(target.facts)
        return {"conditional_write": True, "full_read": True, "delete": True}

    monkeypatch.setattr(change, "probe_target", probe)
    await db.commit()
    target_id = target.id
    yield db, connection, target, calls
    await db.rollback()
    await db.bind.rollback()
    async with test_engine.begin() as cleanup:
        assert not await cleanup.scalar(
            select(StorageOperation.id).where(StorageOperation.location_id == target_id)
        )
        await cleanup.execute(
            delete(StorageMaintenanceGate).where(
                StorageMaintenanceGate.location_id == target_id
            )
        )


def payload(target, generation=1):
    return {
        "expected_generation": generation,
        "target_location_id": str(target.id),
        "target_config_revision": target.config_revision,
    }


async def preview(client, admin, target, generation=1):
    result = await client.post(
        BASE + "/change-preview",
        headers=headers(admin),
        json=payload(target, generation),
    )
    assert result.status_code == 200, result.text
    return result.json()


async def apply(client, admin, target, review, generation=1):
    return await client.patch(
        BASE,
        headers=headers(admin),
        json={**payload(target, generation), "impact_token": review["impact_token"]},
    )


async def test_probe_review_and_atomic_bug_attachment_only_switch(
    world, httpx_client, super_admin
):
    db, connection, target, calls = world
    before = {
        row.purpose: change.binding_facts(row)
        for row in await db.scalars(select(StorageBinding))
    }
    review = await preview(httpx_client, super_admin, target)
    assert len(calls) == 1
    assert review["can_apply"] and review["target"]["bucket"] == "bug_attachment-target"
    assert review["target"]["name"] == "bug_attachment destination"
    assert review["target"]["key_prefix"] == "company/_aap_managed/bug-reports/"
    assert datetime.fromisoformat(review["expires_at"]) - datetime.fromisoformat(
        review["measured_at"]
    ) == timedelta(minutes=5)
    assert "private-access" not in str(review) and "private-secret" not in str(review)
    assert (
        await db.get(StorageBinding, "bug-reports", populate_existing=True)
    ).location_id == LEGACY_LOCATION_ID
    result = await apply(httpx_client, super_admin, target, review)
    assert result.status_code == 200, result.text
    assert result.json()["generation"] == 2 and result.json()["location_id"] == str(
        target.id
    )
    assert await validate_legacy_bindings(db) == []
    for row in await db.scalars(
        select(StorageBinding).execution_options(populate_existing=True)
    ):
        if row.purpose != "bug-reports":
            assert change.binding_facts(row) == before[row.purpose]
    audit = await db.scalar(
        select(AuditLog).where(AuditLog.action == "storage_binding.update")
    )
    assert (
        audit.target_id == "bug-reports"
        and audit.detail_json["target"]["generation"] == 2
    )
    assert "impact_token" not in str(audit.detail_json) and "private-secret" not in str(
        audit.detail_json
    )


@pytest.mark.parametrize(
    "mutation",
    ["target_revision", "connection_revision", "generation", "gate"],
)
async def test_confirm_rejects_changed_review(
    world, httpx_client, super_admin, annotator, mutation
):
    db, connection, target, _ = world
    review = await preview(httpx_client, super_admin, target)
    if mutation == "target_revision":
        target.config_revision += 1
    elif mutation == "connection_revision":
        connection.revision += 1
    elif mutation == "generation":
        (await db.get(StorageBinding, "bug-reports")).generation += 1
    elif mutation == "gate":
        (await db.get(StorageMaintenanceGate, target.id)).revision += 1
    await db.commit()
    result = await apply(httpx_client, super_admin, target, review)
    assert result.status_code == 409, result.text
    assert (
        await db.get(StorageBinding, "bug-reports", populate_existing=True)
    ).location_id == LEGACY_LOCATION_ID


async def test_unknown_historical_attachment_remains_rejected_without_blocking_switch(
    world, httpx_client, super_admin, annotator
):
    from app.db.models.bug_report import BugReport
    from app.services.bug_attachment_uploads import _for_key
    from fastapi import HTTPException

    db, _, target, _ = world
    unknown = f"bug-report-attachments/{annotator[0].id}/{uuid.uuid4()}.png"
    db.add(
        BugReport(
            id=uuid.uuid4(),
            display_id="BUG-B8-UNKNOWN",
            reporter_id=annotator[0].id,
            user_role=annotator[0].role,
            route="/test",
            title="Unknown original",
            description="Unowned remains unowned",
            screenshot_url=unknown,
            attachments=[],
        )
    )
    await db.commit()
    with pytest.raises(HTTPException) as refused:
        await _for_key(db, unknown)
    assert refused.value.detail["code"] == "bug_attachment_unknown_owner"
    await db.commit()
    review = await preview(httpx_client, super_admin, target)
    assert review["can_apply"] and "unlocated_users" not in review["history"]
    result = await apply(httpx_client, super_admin, target, review)
    assert result.status_code == 200, result.text
    with pytest.raises(HTTPException):
        await _for_key(db, unknown)


@pytest.mark.parametrize("failure", ["expired", "tampered", "actor"])
async def test_confirmation_is_signed_expiring_and_actor_bound(
    world, httpx_client, super_admin, project_admin, failure
):
    _, _, target, _ = world
    review = await preview(httpx_client, super_admin, target)
    if failure == "tampered":
        review["impact_token"] = "invalid"
    else:
        data = jwt.decode(
            review["impact_token"],
            settings.secret_key,
            algorithms=["HS256"],
            audience=change.AUDIENCE,
        )
        if failure == "expired":
            data["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        else:
            data["review"] = "another-admin-review"
        review["impact_token"] = jwt.encode(
            data, settings.secret_key, algorithm="HS256"
        )
    result = await apply(httpx_client, super_admin, target, review)
    assert result.status_code == 409


async def test_probe_failure_leaves_binding_unchanged(
    world, httpx_client, super_admin, monkeypatch
):
    db, _, target, _ = world

    def denied(target):
        raise FilesystemStorageError("source_forbidden")

    monkeypatch.setattr(change, "probe_target", denied)
    result = await httpx_client.post(
        BASE + "/change-preview", headers=headers(super_admin), json=payload(target)
    )
    assert result.status_code == 409, result.text
    assert (
        await db.get(StorageBinding, "bug-reports", populate_existing=True)
    ).generation == 1


@pytest.mark.parametrize("unsettled", [False, True])
async def test_unexpected_probe_failure_is_safe_and_keeps_binding(
    world, httpx_client, super_admin, monkeypatch, unsettled
):
    db, _, target, _ = world

    async def failed(*args, **kwargs):
        failure = RuntimeError("private transport credentials")
        if unsettled:
            change.storage_access.mark_unsettled(failure)
        raise failure

    monkeypatch.setattr(change.storage_access, "run", failed)
    response = await httpx_client.post(
        BASE + "/change-preview", headers=headers(super_admin), json=payload(target)
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == (
        "storage_operation_unsettled"
        if unsettled
        else "bug_attachment_storage_unavailable"
    )
    assert "private" not in response.text
    assert (
        await db.get(StorageBinding, "bug-reports", populate_existing=True)
    ).generation == 1


async def test_candidates_are_paginated_and_only_global_s3(
    world, httpx_client, super_admin
):
    db, connection, target, _ = world
    connection.scope = "owner"
    await db.commit()
    result = await httpx_client.get(
        BASE + "/candidates?limit=1&offset=0", headers=headers(super_admin)
    )
    assert result.status_code == 200 and result.json()["total"] == 1
    assert result.json()["items"][0]["location_id"] == str(LEGACY_LOCATION_ID)
    connection.scope = "global"
    await db.commit()
    second = await httpx_client.get(
        BASE + "/candidates?limit=1&offset=1", headers=headers(super_admin)
    )
    assert second.json()["total"] == 2 and len(second.json()["items"]) == 1


async def test_nonadministrator_cannot_probe_or_apply(
    world, httpx_client, project_admin
):
    _, _, target, calls = world
    for method, suffix, body in [
        ("get", "/candidates", None),
        ("post", "/change-preview", payload(target)),
        ("patch", "", {**payload(target), "impact_token": "x"}),
    ]:
        result = await httpx_client.request(
            method,
            BASE + suffix,
            headers=headers(project_admin),
            **({"json": body} if body else {}),
        )
        assert result.status_code == 403
    assert not calls


async def test_read_key_cannot_probe_and_confirmation_is_key_bound(
    world, httpx_client, super_admin
):
    db, _, target, calls = world
    _, read_auth = await key_headers(db, super_admin[0], ["datasets:read"])
    assert (
        await httpx_client.get(BASE + "/candidates", headers=read_auth)
    ).status_code == 200
    denied = await httpx_client.post(
        BASE + "/change-preview", headers=read_auth, json=payload(target)
    )
    assert denied.status_code == 403 and not calls
    key, write_auth = await key_headers(db, super_admin[0], ["datasets:write"])
    review = await httpx_client.post(
        BASE + "/change-preview", headers=write_auth, json=payload(target)
    )
    assert review.status_code == 200, review.text
    _, other_auth = await key_headers(db, super_admin[0], ["datasets:write"])
    changed = await httpx_client.patch(
        BASE,
        headers=other_auth,
        json={**payload(target), "impact_token": review.json()["impact_token"]},
    )
    assert changed.status_code == 409
    key.revoked_at = datetime.now(timezone.utc)
    await db.commit()
    revoked = await httpx_client.patch(
        BASE,
        headers=write_auth,
        json={**payload(target), "impact_token": review.json()["impact_token"]},
    )
    assert revoked.status_code == 401


@pytest.mark.parametrize("state", ["pending", "ready", "retired"])
async def test_saved_attachment_protects_location_after_binding_moves(world, state):
    from app.services.storage_location import location_is_referenced
    from tests.test_bug_attachment_location_records import owner

    db, _, target, _ = world
    row = owner(
        state=state,
        retired_at=datetime.now(timezone.utc) if state == "retired" else None,
    )
    row.storage_ref = {**row.storage_ref, "location_id": str(target.id)}
    db.add(row)
    await db.flush()
    assert await location_is_referenced(db, target.id)
