"""Location edits require a current, actor-bound review of logical dependencies."""

from datetime import datetime, timedelta, timezone
import uuid

from jose import jwt
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import ProjectDataset
from app.db.models.dataset_asset import DatasetAsset
from app.db.models.dataset_source import DatasetScanRun, DatasetSourcePreview
from app.db.models.original_cache import OriginalCacheReadLease
from app.db.models.storage_connection import StorageConnection
from tests.factory import create_project, create_task
from tests.test_dataset_content import content_world as content_world
from tests.test_dataset_references import reference_world as reference_world
from tests.test_storage_locations_api import headers
from tests.test_storage_maintenance_targets import (
    change_confirmation_target,
    saved_avatar,
)
from tests.test_storage_maintenance_impact import (
    cache_entry,
    forbid_source_io,
    frozen_version,
    input_lease,
    key_headers,
    location,
    source,
    statements,
)


def path(where):
    return f"/api/v1/storage-locations/{where.id}"


async def preview(client, where, actor, payload=None, *, auth=None):
    response = await client.post(
        path(where) + "/change-preview",
        headers=auth or headers(actor),
        json=payload or {"expected_revision": where.config_revision, "enabled": False},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def apply(client, where, actor, review, payload=None, *, auth=None):
    return await client.patch(
        path(where),
        headers=auth or headers(actor),
        json={
            **(
                payload
                or {"expected_revision": review["config_revision"], "enabled": False}
            ),
            "impact_token": review["impact_token"],
        },
    )


async def test_preview_is_read_only_offline_and_apply_records_reviewed_changes(
    db_session,
    super_admin,
    httpx_client,
    monkeypatch,
):
    db = db_session
    where = await location(db)
    await source(db, where)
    await db.commit()
    forbid_source_io(monkeypatch)
    with statements(db) as sql:
        review = await preview(httpx_client, where, super_admin)
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "WITH")) for statement in sql
    )
    assert sum("jsonb_array_elements" in statement for statement in sql) == 1
    assert review["changes"] == [{"field": "enabled", "before": True, "after": False}]
    assert review["impact"]["counts"]["sources"] == 1
    assert review["impact"]["execution_permission"] is False
    assert datetime.fromisoformat(review["expires_at"]) - datetime.fromisoformat(
        review["measured_at"]
    ) == timedelta(minutes=5)
    assert "private-source-directory" not in str(review)
    assert "offline-private-root" not in str(review)
    assert where.config_revision == 1 and where.enabled
    result = await apply(httpx_client, where, super_admin, review)
    assert result.status_code == 200, result.text
    assert result.json()["config_revision"] == 2 and not result.json()["enabled"]
    audit = await db.scalar(
        select(AuditLog)
        .where(
            AuditLog.action == "storage_location.update",
            AuditLog.target_id == str(where.id),
        )
        .order_by(AuditLog.id.desc())
    )
    assert audit.detail_json["changes"] == review["changes"]
    assert audit.detail_json["counts"] == review["impact"]["counts"]
    assert audit.detail_json["previous_revision"] == 1
    assert datetime.fromisoformat(
        audit.detail_json["reviewed_at"]
    ) == datetime.fromisoformat(review["measured_at"])
    assert "impact_token" not in str(audit.detail_json)
    assert "private-source-directory" not in str(audit.detail_json)


@pytest.mark.parametrize(
    "change", ["replacement", "state", "binding", "unknown", "polling"]
)
async def test_saved_target_confirmation_uses_selected_semantics(
    db_session, super_admin, httpx_client, monkeypatch, change
):
    where = await location(db_session)
    row = await saved_avatar(db_session, where)
    await db_session.commit()
    forbid_source_io(monkeypatch)
    reviewed = await preview(httpx_client, where, super_admin)
    assert reviewed["impact"]["counts"]["target_avatars"] == 1
    await change_confirmation_target(db_session, where, row, change)
    response = await apply(httpx_client, where, super_admin, reviewed)
    if change in {"unknown", "polling"}:
        assert response.status_code == 200, response.text
        assert response.json()["config_revision"] == 2
    else:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "storage_change_preview_changed"
        assert where.enabled and where.config_revision == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "Reviewed rename"),
        ("enabled", False),
        ("scope", "global"),
        ("cache_policy", "none"),
        ("original_cache_quota_bytes", 4096),
        ("original_cache_ttl_seconds", 90),
    ],
)
async def test_all_editable_fields_use_the_same_review_contract(
    db_session,
    super_admin,
    httpx_client,
    field,
    value,
):
    where = await location(db_session)
    await db_session.commit()
    payload = {"expected_revision": 1, field: value}
    review = await preview(httpx_client, where, super_admin, payload)
    assert review["changes"][0]["field"] == field
    response = await apply(httpx_client, where, super_admin, review, payload)
    assert response.status_code == 200, response.text
    assert response.json()[field] == value


async def test_grants_are_normalized_before_binding_the_candidate(
    db_session,
    super_admin,
    project_admin,
    httpx_client,
):
    where = await location(db_session)
    await db_session.commit()
    manager, admin = str(project_admin[0].id), str(super_admin[0].id)
    review = await preview(
        httpx_client,
        where,
        super_admin,
        {
            "expected_revision": 1,
            "use_user_ids": [manager, admin, manager],
        },
    )
    response = await apply(
        httpx_client,
        where,
        super_admin,
        review,
        {
            "expected_revision": 1,
            "use_user_ids": [admin, manager],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["use_user_ids"] == sorted([admin, manager])


@pytest.mark.parametrize(
    "failure",
    ["missing", "tampered", "expired", "candidate", "location", "actor", "revision"],
)
async def test_invalid_confirmations_do_not_mutate_location(
    db_session,
    super_admin,
    project_admin,
    httpx_client,
    failure,
):
    where = await location(db_session)
    other = await location(db_session)
    await db_session.commit()
    review = await preview(httpx_client, where, super_admin)
    payload = {"expected_revision": 1, "enabled": False}
    target, actor = where, super_admin
    if failure == "missing":
        response = await httpx_client.patch(
            path(where), headers=headers(actor), json=payload
        )
    else:
        if failure == "tampered":
            review["impact_token"] = "invalid." + review["impact_token"]
        elif failure == "expired":
            claims = jwt.get_unverified_claims(review["impact_token"])
            claims["exp"] = int(datetime.now(timezone.utc).timestamp()) - 1
            review["impact_token"] = jwt.encode(
                claims, settings.secret_key, algorithm="HS256"
            )
        elif failure == "candidate":
            payload = {"expected_revision": 1, "enabled": True}
        elif failure == "location":
            target = other
        elif failure == "actor":
            project_admin[0].role = "super_admin"
            await db_session.commit()
            actor = project_admin
        elif failure == "revision":
            where.config_revision = 2
            await db_session.commit()
        response = await apply(httpx_client, target, actor, review, payload)
    assert response.status_code == (422 if failure == "missing" else 409), response.text
    if failure == "expired":
        assert response.json()["detail"]["code"] == "storage_change_preview_expired"
    await db_session.refresh(where)
    assert where.enabled and where.config_revision == (
        2 if failure == "revision" else 1
    )
    assert not await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == "storage_location.update",
            AuditLog.target_id == str(where.id),
        )
    )


@pytest.mark.parametrize("change", ["add", "replace", "policy", "paused"])
async def test_changed_source_membership_or_state_requires_a_new_preview(
    db_session,
    super_admin,
    httpx_client,
    change,
):
    where = await location(db_session)
    retained = await source(db_session, where)
    await db_session.commit()
    review = await preview(httpx_client, where, super_admin)
    if change == "add":
        await source(db_session, where)
    elif change == "replace":
        await db_session.delete(retained)
        await db_session.flush()
        await source(db_session, where)
    elif change == "policy":
        retained.policy_revision += 1
    else:
        retained.enabled = False
    await db_session.commit()
    response = await apply(httpx_client, where, super_admin, review)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "storage_change_preview_changed"
    await db_session.refresh(where)
    assert where.enabled and where.config_revision == 1


@pytest.mark.parametrize(
    "relation",
    [
        "project",
        "task",
        "annotation",
        "input",
        "cache",
        "lease",
        "frozen",
        "preview",
        "scan",
    ],
)
async def test_new_recorded_dependencies_invalidate_confirmation(
    db_session,
    reference_world,
    super_admin,
    httpx_client,
    relation,
):
    db = db_session
    registered, project, task, world = reference_world
    where, retained = world[4:6]
    await db.commit()
    review = await preview(httpx_client, where, super_admin)
    now = await db.scalar(select(func.statement_timestamp()))
    if relation == "project":
        other = await create_project(db, owner_id=super_admin[0].id)
        db.add(ProjectDataset(project_id=other.id, dataset_id=world[3].id))
    elif relation == "task":
        other = await create_task(db, project_id=project.id)
        other.dataset_item_id = task.dataset_item_id
    elif relation == "annotation":
        db.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=super_admin[0].id,
                class_name="car",
                geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            )
        )
    elif relation == "input":
        db.add(input_lease(task.id, now))
    elif relation in ("cache", "lease"):
        entry = cache_entry(where.id, uuid.uuid4(), now)
        db.add(entry)
        await db.flush()
        if relation == "lease":
            # Re-review the entry so only its read lease is new.
            await db.commit()
            review = await preview(httpx_client, where, super_admin)
            db.add(
                OriginalCacheReadLease(
                    id=uuid.uuid4(),
                    entry_id=entry.id,
                    expires_at=now + timedelta(days=1),
                )
            )
    elif relation == "frozen":
        await frozen_version(
            db,
            project.id,
            {"samples": [{"source_lineage": {"location_id": str(where.id)}}]},
            "ready",
        )
    elif relation == "scan":
        db.add(DatasetScanRun(source_id=retained.id, snapshot={}, status="pending"))
    else:
        asset = await db.scalar(
            select(DatasetAsset).where(
                DatasetAsset.dataset_item_id == task.dataset_item_id
            )
        )
        db.add(
            DatasetSourcePreview(
                source_id=retained.id,
                path_digest=world[7].path_digest,
                asset_id=asset.id,
                source_sha256=registered["sha256"],
                spec="review",
                policy_revision=1,
                state="failed",
            )
        )
    await db.commit()
    response = await apply(httpx_client, where, super_admin, review)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "storage_change_preview_changed"


async def test_api_key_review_is_bound_to_exact_credential_and_current_scope(
    db_session,
    super_admin,
    httpx_client,
):
    where = await location(db_session)
    await db_session.commit()
    key, auth = await key_headers(db_session, super_admin[0], ["datasets:write"])
    _, other = await key_headers(db_session, super_admin[0], ["datasets:write"])
    review = await preview(httpx_client, where, super_admin, auth=auth)
    response = await apply(httpx_client, where, super_admin, review, auth=other)
    assert response.status_code == 409
    response = await apply(httpx_client, where, super_admin, review)
    assert response.status_code == 409
    key.scopes = ["datasets:read"]
    await db_session.commit()
    response = await apply(httpx_client, where, super_admin, review, auth=auth)
    assert response.status_code == 403


async def test_preview_requires_current_admin_write_scope_and_keeps_platform_guards(
    db_session,
    super_admin,
    project_admin,
    annotator,
    httpx_client,
):
    db = db_session
    where = await location(db)
    platform = await location(db, kind="s3")
    connection = StorageConnection(
        name="Review connector",
        kind="s3",
        scope="owner",
        created_by=super_admin[0].id,
        config={"bucket": "private"},
    )
    db.add(connection)
    await db.flush()
    connector = await location(db, kind="s3", connection_id=connection.id)
    await db.commit()
    for actor in (project_admin, annotator):
        response = await httpx_client.post(
            path(where) + "/change-preview",
            headers=headers(actor),
            json={"expected_revision": 1, "enabled": False},
        )
        assert response.status_code == 403
    _, auth = await key_headers(db, super_admin[0], ["datasets:read"])
    response = await httpx_client.post(
        path(where) + "/change-preview",
        headers=auth,
        json={"expected_revision": 1, "enabled": False},
    )
    assert response.status_code == 403
    for target, payload in [
        (platform, {"enabled": False}),
        (connector, {"scope": "global"}),
    ]:
        response = await httpx_client.post(
            path(target) + "/change-preview",
            headers=headers(super_admin),
            json={"expected_revision": 1, **payload},
        )
        assert response.status_code == 409
    review = await preview(httpx_client, connector, super_admin)
    connection.revision += 1
    await db.commit()
    response = await apply(httpx_client, connector, super_admin, review)
    assert response.status_code == 409


async def test_successful_confirmation_cannot_be_replayed_or_duplicate_audit(
    db_session,
    super_admin,
    httpx_client,
):
    db = db_session
    where = await location(db)
    await db.commit()
    review = await preview(httpx_client, where, super_admin)
    first = await apply(httpx_client, where, super_admin, review)
    assert first.status_code == 200, first.text
    replay = await apply(httpx_client, where, super_admin, review)
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["code"] == "storage_location_revision_changed"
    await db.refresh(where)
    assert where.config_revision == 2 and not where.enabled
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "storage_location.update",
                AuditLog.target_id == str(where.id),
            )
        )
        == 1
    )


@pytest.mark.parametrize("relation", ["task", "annotation"])
async def test_same_count_business_identity_replacement_requires_a_new_review(
    db_session,
    reference_world,
    super_admin,
    httpx_client,
    relation,
):
    db = db_session
    _, project, task, world = reference_world
    where = world[4]
    old_id = task.id
    if relation == "annotation":
        original = Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=super_admin[0].id,
            class_name="car",
            geometry={},
        )
        db.add(original)
        await db.flush()
        old_id = original.id
    await db.commit()
    review = await preview(httpx_client, where, super_admin)
    if relation == "task":
        item_id = task.dataset_item_id
        await db.delete(task)
        await db.flush()
        replacement = await create_task(db, project_id=project.id)
        replacement.dataset_item_id = item_id
    else:
        await db.delete(original)
        await db.flush()
        replacement = Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=super_admin[0].id,
            class_name="car",
            geometry={},
        )
        db.add(replacement)
    await db.commit()
    assert replacement.id != old_id
    latest = await preview(httpx_client, where, super_admin)
    assert latest["impact"]["counts"] == review["impact"]["counts"]
    response = await apply(httpx_client, where, super_admin, review)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "storage_change_preview_changed"
    await db.refresh(where)
    assert where.config_revision == review["config_revision"] and where.enabled


async def test_confirmation_expiring_during_the_census_cannot_apply(
    db_session,
    super_admin,
    httpx_client,
    monkeypatch,
):
    from app.services import storage_location_change as changes
    from app.services import storage_maintenance_impact as maintenance

    db = db_session
    where = await location(db)
    await db.commit()
    review = await preview(httpx_client, where, super_admin)

    class ReviewClock(datetime):
        instant = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return (
                cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)
            )

    monkeypatch.setattr(changes, "datetime", ReviewClock)
    original_dependencies, execute = maintenance.dependencies, db.execute
    checkpoints = []

    def marked(*args, **kwargs):
        return original_dependencies(*args, **kwargs).execution_options(
            expiry_checkpoint=True
        )

    async def expire_after_snapshot(statement, *args, **kwargs):
        result = await execute(statement, *args, **kwargs)
        if statement.get_execution_options().get("expiry_checkpoint"):
            checkpoints.append(True)
            ReviewClock.instant = datetime.fromisoformat(
                review["expires_at"]
            ) + timedelta(seconds=1)
        return result

    monkeypatch.setattr(maintenance, "dependencies", marked)
    monkeypatch.setattr(db, "execute", expire_after_snapshot)
    response = await apply(httpx_client, where, super_admin, review)
    assert checkpoints == [True]
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "storage_change_preview_expired"
    await db.refresh(where)
    assert where.config_revision == 1 and where.enabled
    assert not await db.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == "storage_location.update",
            AuditLog.target_id == str(where.id),
        )
    )


async def test_heartbeat_and_cache_access_updates_do_not_invalidate_review(
    db_session,
    super_admin,
    httpx_client,
):
    db = db_session
    where = await location(db)
    retained = await source(db, where)
    now = await db.scalar(select(func.statement_timestamp()))
    run = DatasetScanRun(
        source_id=retained.id,
        snapshot={},
        status="running",
        lease_expires_at=now + timedelta(minutes=1),
    )
    entry = cache_entry(where.id, uuid.uuid4(), now)
    db.add_all([run, entry])
    await db.commit()
    review = await preview(httpx_client, where, super_admin)
    run.lease_token = uuid.uuid4()
    run.lease_expires_at = now + timedelta(minutes=2)
    run.counts = {"observed": 100}
    entry.last_accessed_at = now + timedelta(seconds=1)
    await db.commit()
    response = await apply(httpx_client, where, super_admin, review)
    assert response.status_code == 200, response.text
    assert response.json()["config_revision"] == 2
