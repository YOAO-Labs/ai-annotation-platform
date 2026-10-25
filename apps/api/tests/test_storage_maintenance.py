"""Reviewed controls govern one location and never grant physical cleanup."""

from datetime import datetime, timedelta, timezone
import uuid

from jose import jwt
import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.db.models.audit_log import AuditLog
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.services import storage_access as access, storage_maintenance as svc
from tests.test_storage_locations_api import headers
from tests.test_storage_maintenance_targets import (
    change_confirmation_target,
    saved_avatar,
)
from tests.test_storage_maintenance_impact import (
    forbid_source_io,
    key_headers,
    location,
    source,
    statements,
)


@pytest.fixture
async def where(db_session, test_engine):
    row = await location(db_session)
    identity = row.id
    yield row
    await db_session.rollback()
    # The shared fixture owns an outer Connection transaction beyond Session
    # SAVEPOINTs; release its locks before independent ledger cleanup.
    await db_session.bind.rollback()
    async with test_engine.begin() as connection:
        await connection.execute(
            delete(StorageOperation).where(StorageOperation.location_id == identity)
        )
        await connection.execute(delete(Gate).where(Gate.location_id == identity))


@pytest.fixture
async def enabled(monkeypatch):
    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", True)


def path(where):
    return f"/api/v1/storage-locations/{where.id}/maintenance"


async def preview(client, where, actor, operation="drain", revision=1, *, auth=None):
    response = await client.post(
        path(where) + "-preview",
        headers=auth or headers(actor),
        json={"operation": operation, "expected_revision": revision},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def apply(client, where, actor, reviewed, *, auth=None, **overrides):
    return await client.post(
        path(where),
        headers=auth or headers(actor),
        json={
            "operation": reviewed["operation"],
            "expected_revision": reviewed["revision"],
            "impact_token": reviewed["impact_token"],
            **overrides,
        },
    )


async def test_disabled_protocol_reports_state_but_cannot_control(
    httpx_client, db_session, super_admin, where, monkeypatch
):
    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", False)
    response = await httpx_client.get(path(where), headers=headers(super_admin))
    assert response.status_code == 200
    result = response.json()
    assert result["state"] == "active" and result["revision"] == 1
    assert result["protocol_enabled"] is False
    assert result["physical_cleanup_permission"] is False
    assert result["coverage"] == "registered_location_access_only"
    for suffix, body in [("-preview", {}), ("", {"impact_token": "unqualified"})]:
        response = await httpx_client.post(
            path(where) + suffix,
            headers=headers(super_admin),
            json={"operation": "drain", "expected_revision": 1, **body},
        )
        assert response.status_code == 409
        assert (
            response.json()["detail"]["code"]
            == "storage_maintenance_protocol_unqualified"
        )
    # Rollout guard controls only the UI/API operations, not durable recording.
    async with await access.acquire(db_session, where.id, "rollout_read"):
        assert (await svc.snapshot(db_session, where.id))["open_operations"] == 1


@pytest.mark.parametrize(
    "change", ["replacement", "state", "binding", "unknown", "polling"]
)
async def test_saved_target_review_tracks_selected_responsibilities(
    httpx_client, db_session, super_admin, where, enabled, monkeypatch, change
):
    row = await saved_avatar(db_session, where)
    await db_session.commit()
    forbid_source_io(monkeypatch)
    reviewed = await preview(httpx_client, where, super_admin)
    assert reviewed["impact"]["counts"]["target_avatars"] == 1
    await change_confirmation_target(db_session, where, row, change)
    response = await apply(httpx_client, where, super_admin, reviewed)
    if change in {"unknown", "polling"}:
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "draining"
    else:
        assert response.status_code == 409, response.text
        assert (
            response.json()["detail"]["code"] == "storage_maintenance_preview_changed"
        )
        assert (await svc.snapshot(db_session, where.id))["state"] == "active"


async def test_preview_offline_read_only_and_cycle_increments_only_maintenance_revision(
    httpx_client, db_session, super_admin, where, enabled, monkeypatch
):
    await source(db_session, where)
    await db_session.commit()
    forbid_source_io(monkeypatch)
    with statements(db_session) as sql:
        reviewed = await preview(httpx_client, where, super_admin)
    assert not any(
        statement.lstrip()
        .upper()
        .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"))
        for statement in sql
    )
    assert sum("jsonb_array_elements" in statement for statement in sql) == 1
    assert reviewed["target_state"] == "draining"
    assert reviewed["impact"]["counts"]["sources"] == 1
    assert reviewed["measured_at"] == reviewed["impact"]["measured_at"]
    assert datetime.fromisoformat(reviewed["expires_at"]) - datetime.fromisoformat(
        reviewed["measured_at"]
    ) == timedelta(minutes=5)
    assert "private-source-directory" not in str(reviewed)
    assert "offline-private-root" not in str(reviewed)
    response = await apply(httpx_client, where, super_admin, reviewed)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "draining" and response.json()["revision"] == 2
    assert where.config_revision == 1 and where.enabled
    for operation, revision, target in [
        ("stop", 2, "stopped"),
        ("resume", 3, "active"),
        ("drain", 4, "draining"),
        ("resume", 5, "active"),
    ]:
        reviewed = await preview(httpx_client, where, super_admin, operation, revision)
        assert reviewed["target_state"] == target
        response = await apply(httpx_client, where, super_admin, reviewed)
        assert response.status_code == 200, response.text
        assert (
            response.json()["state"] == target
            and response.json()["revision"] == revision + 1
        )
    audit = await db_session.scalar(
        select(AuditLog)
        .where(
            AuditLog.target_id == str(where.id),
            AuditLog.action == "storage_location.maintenance_resume",
        )
        .order_by(AuditLog.id.desc())
    )
    assert audit.detail_json["previous_revision"] == 5
    assert audit.detail_json["revision"] == 6
    assert audit.detail_json["counts"]["sources"] == 1
    assert audit.detail_json["physical_cleanup_permission"] is False
    assert "impact_token" not in str(audit.detail_json)
    assert "private-source-directory" not in str(audit.detail_json)


async def test_drain_permits_current_receipt_but_stop_requires_actual_settlement(
    httpx_client, db_session, super_admin, where, enabled
):
    permit = await access.acquire(db_session, where.id, "long_read")
    reviewed = await preview(httpx_client, where, super_admin)
    assert reviewed["open_operations"] == 1
    response = await apply(httpx_client, where, super_admin, reviewed)
    assert response.status_code == 200
    rejected = await httpx_client.post(
        path(where) + "-preview",
        headers=headers(super_admin),
        json={"operation": "stop", "expected_revision": 2},
    )
    assert (
        rejected.status_code == 409
        and rejected.json()["detail"]["code"] == "storage_maintenance_unsettled"
    )
    await permit.__aexit__(None, None, None)
    reviewed = await preview(httpx_client, where, super_admin, "stop", 2)
    assert reviewed["open_operations"] == 0
    response = await apply(httpx_client, where, super_admin, reviewed)
    assert response.status_code == 200 and response.json()["state"] == "stopped"


async def test_drain_token_does_not_bind_changing_receipt_count(
    httpx_client, db_session, super_admin, where, enabled
):
    permit = await access.acquire(db_session, where.id, "finishing_read")
    reviewed = await preview(httpx_client, where, super_admin)
    assert reviewed["open_operations"] == 1
    await permit.__aexit__(None, None, None)
    response = await apply(httpx_client, where, super_admin, reviewed)
    assert response.status_code == 200 and response.json()["open_operations"] == 0


@pytest.mark.parametrize(
    "failure",
    [
        "tampered",
        "expired",
        "location",
        "actor",
        "revision",
        "dependencies",
        "same_count",
        "config",
    ],
)
async def test_changed_or_invalid_preview_cannot_mutate_gate(
    httpx_client, db_session, super_admin, where, enabled, failure
):
    retained = await source(db_session, where)
    reviewed = await preview(httpx_client, where, super_admin)
    actor = super_admin
    if failure == "tampered":
        reviewed["impact_token"] += "invalid"
    elif failure == "expired":
        claims = jwt.get_unverified_claims(reviewed["impact_token"])
        claims["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        reviewed["impact_token"] = jwt.encode(
            claims, settings.secret_key, algorithm="HS256"
        )
    elif failure == "location":
        other = await location(db_session)
        reviewed["impact_token"] = (await preview(httpx_client, other, super_admin))[
            "impact_token"
        ]
    elif failure == "actor":
        claims = jwt.get_unverified_claims(reviewed["impact_token"])
        claims["binding"] = "a different current administrator binding"
        reviewed["impact_token"] = jwt.encode(
            claims, settings.secret_key, algorithm="HS256"
        )
    elif failure == "revision":
        reviewed["revision"] = 2
    elif failure == "dependencies":
        await source(db_session, where)
    elif failure == "same_count":
        await db_session.delete(retained)
        await db_session.flush()
        await source(db_session, where)
    elif failure == "config":
        where.name += " changed"
        where.config_revision += 1
        await db_session.flush()
    response = await apply(httpx_client, where, actor, reviewed)
    assert response.status_code == 409, response.text
    code = response.json()["detail"]["code"]
    assert code == {
        "tampered": "storage_maintenance_preview_invalid",
        "expired": "storage_maintenance_preview_expired",
        "revision": "storage_maintenance_changed",
    }.get(failure, "storage_maintenance_preview_changed")
    result = await svc.snapshot(db_session, where.id)
    assert result["state"] == "active" and result["revision"] == 1


async def test_replay_and_invalid_transition_are_rejected(
    httpx_client, super_admin, where, enabled
):
    rejected = await httpx_client.post(
        path(where) + "-preview",
        headers=headers(super_admin),
        json={"operation": "stop", "expected_revision": 1},
    )
    assert (
        rejected.status_code == 409
        and rejected.json()["detail"]["code"]
        == "storage_maintenance_transition_invalid"
    )
    reviewed = await preview(httpx_client, where, super_admin)
    assert (await apply(httpx_client, where, super_admin, reviewed)).status_code == 200
    response = await apply(httpx_client, where, super_admin, reviewed)
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "storage_maintenance_changed"
    )


async def test_status_is_bounded_and_preserves_unresolved_old_generation_receipts(
    httpx_client, db_session, super_admin, where, enabled
):
    await access.ensure(db_session, where.id)
    async with access.engine(db_session).begin() as connection:
        await connection.execute(
            StorageOperation.__table__.insert(),
            [
                {
                    "id": uuid.uuid4(),
                    "location_id": where.id,
                    "revision": 1,
                    "purpose": "safe_read",
                    "node_id": "worker:test",
                }
                for _ in range(53)
            ],
        )
    response = await httpx_client.get(path(where), headers=headers(super_admin))
    assert response.status_code == 200
    result = response.json()
    assert result["open_operations"] == 53 and len(result["operations"]) == 50
    assert result["has_more"]
    assert set(result["operations"][0]) == {
        "id",
        "purpose",
        "node_id",
        "revision",
        "started_at",
    }
    reviewed = await preview(httpx_client, where, super_admin)
    assert (await apply(httpx_client, where, super_admin, reviewed)).json()[
        "open_operations"
    ] == 53


@pytest.mark.parametrize("role", ["project_admin", "annotator", "reviewer"])
async def test_only_current_super_admin_can_observe_and_control(
    httpx_client, where, enabled, project_admin, annotator, reviewer, role
):
    actor = {
        "project_admin": project_admin,
        "annotator": annotator,
        "reviewer": reviewer,
    }[role]
    for method, suffix in [("get", ""), ("post", "-preview")]:
        response = await getattr(httpx_client, method)(
            path(where) + suffix,
            headers=headers(actor),
            **(
                {"json": {"operation": "drain", "expected_revision": 1}}
                if method == "post"
                else {}
            ),
        )
        assert response.status_code == 403


async def test_scope_rechecked_and_read_key_cannot_control(
    httpx_client, db_session, super_admin, where, enabled
):
    _, read_auth = await key_headers(db_session, super_admin[0], ["datasets:read"])
    response = await httpx_client.get(path(where), headers=read_auth)
    assert response.status_code == 200
    response = await httpx_client.post(
        path(where) + "-preview",
        headers=read_auth,
        json={"operation": "drain", "expected_revision": 1},
    )
    assert response.status_code == 403


async def test_platform_location_has_no_maintenance_contract(
    httpx_client, db_session, super_admin, enabled
):
    where = await location(db_session, kind="s3")
    for method, suffix in [("get", ""), ("post", "-preview")]:
        response = await getattr(httpx_client, method)(
            path(where) + suffix,
            headers=headers(super_admin),
            **(
                {"json": {"operation": "drain", "expected_revision": 1}}
                if method == "post"
                else {}
            ),
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "platform_maintenance_unsupported"
