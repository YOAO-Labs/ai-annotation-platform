"""Archive/restore preserves ownership while rejecting unreviewed state changes."""

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException
from jose import jwt
import pytest
from sqlalchemy import delete, func, literal, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.dataset_source import (
    DatasetRegistrationRun,
    DatasetScanRun,
    DatasetSourceSyncSchedule,
)
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.user import User
from app.services import api_key_service, dataset_lifecycle as lifecycle
from tests.factory import create_project, create_task, create_user
from tests.test_storage_maintenance_impact import (
    dataset,
    forbid_source_io,
    frozen_version,
    key_headers,
    location,
    source,
    statements,
)
from tests.test_storage_locations_api import headers


def path(row):
    return f"/api/v1/datasets/{row.id}"


async def preview(client, row, actor, *, operation="archive", revision=None, auth=None):
    response = await client.post(
        path(row) + "/lifecycle-preview",
        headers=auth or headers(actor),
        json={
            "operation": operation,
            "expected_revision": revision or row.lifecycle_revision,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def apply(client, row, actor, review, *, auth=None, **overrides):
    return await client.post(
        path(row) + "/lifecycle",
        headers=auth or headers(actor),
        json={
            "operation": review["operation"],
            "expected_revision": review["lifecycle_revision"],
            "impact_token": review["impact_token"],
            **overrides,
        },
    )


async def world(db, actor):
    row = await dataset(db, actor)
    item = DatasetItem(
        dataset_id=row.id, file_name="retained.png", file_path="retained/source.png"
    )
    db.add(item)
    await db.flush()
    project = await create_project(db, owner_id=actor.id)
    link = ProjectDataset(dataset_id=row.id, project_id=project.id)
    db.add(link)
    task = await create_task(db, project_id=project.id)
    task.dataset_item_id = item.id
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=actor.id,
        class_name="car",
        geometry={},
    )
    db.add(annotation)
    await db.flush()
    return row, item, project, link, task, annotation


async def test_preview_is_offline_read_only_and_archive_preserves_business_identities(
    db_session,
    super_admin,
    httpx_client,
    monkeypatch,
):
    db = db_session
    row, item, project, link, task, annotation = await world(db, super_admin[0])
    where = await location(db)
    retained = await source(db, where, dataset_id=row.id)
    frozen = await frozen_version(
        db, project.id, {"samples": [{"dataset_id": str(row.id)}]}, "ready"
    )
    await db.commit()
    forbid_source_io(monkeypatch)
    identities = [
        obj.id for obj in (row, item, project, link, task, annotation, retained, frozen)
    ]
    with statements(db) as sql:
        review = await preview(httpx_client, row, super_admin)
    assert not any(
        statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in sql
    )
    assert sum("jsonb_array_elements" in statement for statement in sql) == 1
    assert datetime.fromisoformat(review["expires_at"]) - datetime.fromisoformat(
        review["measured_at"]
    ) == timedelta(minutes=5)
    assert review["counts"] == {
        "items": 1,
        "sources": 1,
        "projects": 1,
        "tasks": 1,
        "annotations": 1,
        "active_scans": 0,
        "active_registrations": 0,
        "enabled_schedules": 0,
        "pending_jobs": 0,
        "ready_lineage_versions": 1,
        "unfinished_lineage_versions": 0,
    }
    assert review["state"] == "active"
    assert review["frozen_coverage"] == "recorded_dataset_membership_only"
    assert "retained/source.png" not in str(review)
    response = await apply(httpx_client, row, super_admin, review)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["state"] == "archived" and result["lifecycle_revision"] == 2
    assert result["archived_by"] == str(super_admin[0].id) and result["archived_at"]
    for model, object_id in zip(
        (
            Dataset,
            DatasetItem,
            type(project),
            ProjectDataset,
            type(task),
            Annotation,
            type(retained),
            type(frozen),
        ),
        identities,
        strict=True,
    ):
        assert await db.get(model, object_id, populate_existing=True) is not None
    assert retained.enabled and where.enabled and frozen.status == "ready"
    assert frozen.snapshot == {"samples": [{"dataset_id": str(row.id)}]}
    audit = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "dataset.archive", AuditLog.target_id == str(row.id)
        )
    )
    assert audit.detail_json["counts"] == review["counts"]
    assert (
        audit.detail_json["previous_revision"] == 1
        and audit.detail_json["lifecycle_revision"] == 2
    )
    assert datetime.fromisoformat(
        audit.detail_json["reviewed_at"]
    ) == datetime.fromisoformat(review["measured_at"])
    assert "impact_token" not in str(audit.detail_json) and "source.png" not in str(
        audit.detail_json
    )


async def test_restore_changes_only_lifecycle_and_does_not_revive_revoked_authority(
    db_session,
    project_admin,
    super_admin,
    httpx_client,
):
    db = db_session
    owner = project_admin[0]
    row = await dataset(db, owner)
    where = await location(db)
    where.config = {**where.config, "use_user_ids": [str(owner.id)]}
    retained = await source(db, where, dataset_id=row.id)
    schedule = DatasetSourceSyncSchedule(
        source_id=retained.id, actor_id=owner.id, enabled=True
    )
    db.add(schedule)
    key, _ = await api_key_service.create_key(
        db, owner, "Archive history", ["datasets:write"]
    )
    await db.commit()
    archived = await apply(
        httpx_client,
        row,
        project_admin,
        await preview(httpx_client, row, project_admin),
    )
    assert archived.status_code == 200
    retained.enabled = False
    where.config = {**where.config, "use_user_ids": []}
    schedule.enabled, schedule.state = False, "blocked"
    key.revoked_at = datetime.now(timezone.utc)
    owner.is_active = False
    await db.commit()
    review = await preview(
        httpx_client, row, super_admin, operation="restore", revision=2
    )
    restored = await apply(httpx_client, row, super_admin, review)
    assert restored.status_code == 200, restored.text
    assert restored.json() == {
        "dataset_id": str(row.id),
        "lifecycle_revision": 3,
        "state": "active",
        "archived_at": None,
        "archived_by": None,
    }
    for obj in (owner, retained, where, schedule, key):
        await db.refresh(obj)
    assert not owner.is_active and not retained.enabled
    assert where.config["use_user_ids"] == []
    assert not schedule.enabled and schedule.state == "blocked" and key.revoked_at
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "dataset.restore", AuditLog.target_id == str(row.id)
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "tampered",
        "expired",
        "operation",
        "dataset",
        "actor",
        "revision",
        "metadata",
    ],
)
async def test_invalid_confirmations_never_transition_state(
    db_session, super_admin, project_admin, httpx_client, failure
):
    db = db_session
    row = await dataset(db, super_admin[0])
    other = await dataset(db, super_admin[0])
    await db.commit()
    review = await preview(httpx_client, row, super_admin)
    actor, target, overrides = super_admin, row, {}
    if failure == "missing":
        response = await httpx_client.post(
            path(row) + "/lifecycle",
            headers=headers(actor),
            json={"operation": "archive", "expected_revision": 1},
        )
    else:
        if failure == "tampered":
            review["impact_token"] = "wrong." + review["impact_token"]
        elif failure == "expired":
            claims = jwt.get_unverified_claims(review["impact_token"])
            claims["exp"] = int(datetime.now(timezone.utc).timestamp()) - 1
            review["impact_token"] = jwt.encode(
                claims, settings.secret_key, algorithm="HS256"
            )
        elif failure == "operation":
            overrides["operation"] = "restore"
        elif failure == "dataset":
            target = other
        elif failure == "actor":
            project_admin[0].role = "super_admin"
            await db.commit()
            actor = project_admin
        elif failure == "revision":
            row.lifecycle_revision += 1
            await db.commit()
        else:
            row.description = "A different reviewed dataset configuration"
            await db.commit()
        response = await apply(httpx_client, target, actor, review, **overrides)
    assert response.status_code == (422 if failure == "missing" else 409), response.text
    await db.refresh(row)
    assert row.archived_at is None
    assert not await db.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "dataset.archive", AuditLog.target_id == str(row.id))
    )


@pytest.mark.parametrize(
    "relation",
    [
        "item",
        "source",
        "task",
        "annotation",
        "project",
        "schedule",
        "scan",
        "registration",
        "job",
        "frozen",
    ],
)
async def test_new_dependency_requires_a_fresh_review(
    db_session, super_admin, httpx_client, relation
):
    db = db_session
    row, item, project, _, task, _ = await world(db, super_admin[0])
    where = await location(db)
    retained = await source(db, where, dataset_id=row.id)
    await db.commit()
    review = await preview(httpx_client, row, super_admin)
    if relation == "item":
        db.add(
            DatasetItem(
                dataset_id=row.id, file_name="new.png", file_path="managed/new.png"
            )
        )
    elif relation == "source":
        await source(db, where, dataset_id=row.id)
    elif relation == "task":
        extra = await create_task(db, project_id=project.id)
        extra.dataset_item_id = item.id
    elif relation == "annotation":
        db.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=super_admin[0].id,
                class_name="car",
                geometry={},
            )
        )
    elif relation == "project":
        extra_project = await create_project(db, owner_id=super_admin[0].id)
        db.add(ProjectDataset(dataset_id=row.id, project_id=extra_project.id))
    elif relation == "schedule":
        db.add(
            DatasetSourceSyncSchedule(
                source_id=retained.id, actor_id=super_admin[0].id, enabled=True
            )
        )
    elif relation in {"scan", "registration"}:
        scan = DatasetScanRun(
            source_id=retained.id,
            snapshot={},
            status="pending" if relation == "scan" else "completed",
        )
        db.add(scan)
        await db.flush()
        if relation == "registration":
            db.add(
                DatasetRegistrationRun(
                    source_id=retained.id,
                    scan_run_id=scan.id,
                    snapshot={},
                    status="pending",
                )
            )
    elif relation == "job":
        db.add(
            AsyncJob(
                kind="dataset_import",
                status="pending",
                payload={"dataset_id": str(row.id)},
            )
        )
    else:
        await frozen_version(
            db, project.id, {"samples": [{"dataset_id": str(row.id)}]}, "pending"
        )
    await db.commit()
    response = await apply(httpx_client, row, super_admin, review)
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "dataset_lifecycle_preview_changed"
    ), response.text
    await db.refresh(row)
    assert row.archived_at is None and row.lifecycle_revision == 1


@pytest.mark.parametrize("relation", ["task", "annotation"])
async def test_same_count_replaced_identity_is_not_the_reviewed_dependency(
    db_session, super_admin, httpx_client, relation
):
    db = db_session
    row, item, project, _, task, annotation = await world(db, super_admin[0])
    await db.commit()
    review = await preview(httpx_client, row, super_admin)
    if relation == "task":
        await db.delete(annotation)
        await db.delete(task)
        await db.flush()
        replacement = await create_task(db, project_id=project.id)
        replacement.dataset_item_id = item.id
        db.add(
            Annotation(
                task_id=replacement.id,
                project_id=project.id,
                user_id=super_admin[0].id,
                class_name="car",
                geometry={},
            )
        )
    else:
        await db.delete(annotation)
        await db.flush()
        db.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=super_admin[0].id,
                class_name="car",
                geometry={},
            )
        )
    await db.commit()
    current = await preview(httpx_client, row, super_admin)
    assert current["counts"] == review["counts"]
    response = await apply(httpx_client, row, super_admin, review)
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "dataset_lifecycle_preview_changed"
    )


async def test_multi_file_tasks_and_recorded_frozen_membership_are_deduplicated(
    db_session, super_admin, httpx_client
):
    db = db_session
    row, item, project, _, task, _ = await world(db, super_admin[0])
    db.add(
        TaskDatasetItemLink(
            task_id=task.id, dataset_item_id=item.id, role="camera_front"
        )
    )
    independent = await create_task(db, project_id=project.id)
    db.add(
        TaskDatasetItemLink(
            task_id=independent.id, dataset_item_id=item.id, role="camera_front"
        )
    )
    await frozen_version(
        db,
        project.id,
        {"samples": [{"dataset_id": str(row.id)}, {"dataset_id": str(row.id)}]},
        "ready",
    )
    await frozen_version(
        db, project.id, {"samples": [{"dataset_id": str(row.id)}]}, "running"
    )
    for snapshot in (
        {},
        {"samples": None},
        {"samples": {}},
        {"samples": [{"dataset_id": str(uuid.uuid4())}]},
    ):
        await frozen_version(db, project.id, snapshot, "ready")
    await db.commit()
    review = await preview(httpx_client, row, super_admin)
    assert review["counts"]["tasks"] == 2 and review["counts"]["projects"] == 1
    assert (
        review["counts"]["ready_lineage_versions"]
        == review["counts"]["unfinished_lineage_versions"]
        == 1
    )


async def test_only_current_creator_or_super_admin_can_review(
    db_session, super_admin, project_admin, annotator, httpx_client
):
    db = db_session
    row = await dataset(db, super_admin[0])
    await db.commit()
    for actor, status in ((project_admin, 404), (annotator, 403)):
        result = await httpx_client.post(
            path(row) + "/lifecycle-preview",
            headers=headers(actor),
            json={"operation": "archive", "expected_revision": 1},
        )
        assert result.status_code == status, result.text
    super_admin[0].role = "employee"
    await db.commit()
    result = await httpx_client.post(
        path(row) + "/lifecycle-preview",
        headers=headers(super_admin),
        json={"operation": "archive", "expected_revision": 1},
    )
    assert result.status_code == 403


async def test_key_scope_revocation_and_exact_key_binding(
    db_session, super_admin, httpx_client
):
    db = db_session
    row = await dataset(db, super_admin[0])
    await db.commit()
    key, auth = await key_headers(db, super_admin[0], ["datasets:write"])
    _, other = await key_headers(db, super_admin[0], ["datasets:write"])
    review = await preview(httpx_client, row, super_admin, auth=auth)
    assert (
        await apply(httpx_client, row, super_admin, review, auth=other)
    ).status_code == 409
    assert (await apply(httpx_client, row, super_admin, review)).status_code == 409
    key.scopes = ["datasets:read"]
    await db.commit()
    assert (
        await apply(httpx_client, row, super_admin, review, auth=auth)
    ).status_code == 403
    result = await httpx_client.post(
        path(row) + "/lifecycle-preview",
        headers=auth,
        json={"operation": "archive", "expected_revision": 1},
    )
    assert result.status_code == 403


async def test_replayed_token_and_old_write_snapshot_stay_invalid_after_restore(
    db_session, super_admin, httpx_client
):
    db = db_session
    row = await dataset(db, super_admin[0])
    await db.commit()
    review = await preview(httpx_client, row, super_admin)
    assert (await apply(httpx_client, row, super_admin, review)).status_code == 200
    with pytest.raises(HTTPException) as error:
        await lifecycle.require_writable(db, row.id)
    assert error.value.detail["code"] == "dataset_archived"
    response = await apply(httpx_client, row, super_admin, review)
    assert response.status_code == 409
    restored = await apply(
        httpx_client,
        row,
        super_admin,
        await preview(httpx_client, row, super_admin, operation="restore", revision=2),
    )
    assert restored.status_code == 200
    with pytest.raises(HTTPException) as error:
        await lifecycle.require_writable(db, row.id, expected_revision=1)
    assert error.value.detail["code"] == "dataset_lifecycle_changed"
    assert (
        await lifecycle.require_writable(db, row.id, expected_revision=3)
    ).id == row.id
    assert (await apply(httpx_client, row, super_admin, review)).status_code == 409
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "dataset.archive", AuditLog.target_id == str(row.id)
            )
        )
        == 1
    )


async def test_expiry_is_rechecked_after_the_dependency_statement(
    db_session, super_admin, httpx_client, monkeypatch
):
    db = db_session
    row = await dataset(db, super_admin[0])
    await db.commit()
    review = await preview(httpx_client, row, super_admin)

    class Clock(datetime):
        instant = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return (
                cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)
            )

    monkeypatch.setattr(lifecycle, "datetime", Clock)
    dependencies, execute = lifecycle.dependencies, db.execute

    def marked(*args, **kwargs):
        return dependencies(*args, **kwargs).execution_options(expire_lifecycle=True)

    async def expire(statement, *args, **kwargs):
        result = await execute(statement, *args, **kwargs)
        if statement.get_execution_options().get("expire_lifecycle"):
            Clock.instant = datetime.fromisoformat(review["expires_at"]) + timedelta(
                seconds=1
            )
        return result

    monkeypatch.setattr(lifecycle, "dependencies", marked)
    monkeypatch.setattr(db, "execute", expire)
    result = await apply(httpx_client, row, super_admin, review)
    assert (
        result.status_code == 409
        and result.json()["detail"]["code"] == "dataset_lifecycle_preview_expired"
    )
    await db.refresh(row)
    assert row.archived_at is None and row.lifecycle_revision == 1


async def test_nowait_dataset_lock_refuses_without_poisoning_the_request_transaction(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        owner = await create_user(
            setup,
            "super_admin",
            f"lifecycle-lock-{uuid.uuid4().hex}@test.local",
            "Lifecycle lock owner",
        )
        row = await dataset(setup, owner)
        dataset_id, owner_id = row.id, owner.id
        await setup.commit()
    try:
        async with maker() as holder, maker() as contender:
            await holder.scalar(
                select(Dataset).where(Dataset.id == dataset_id).with_for_update()
            )
            with pytest.raises(HTTPException) as error:
                await lifecycle.require_writable(contender, dataset_id, lock=True)
            assert error.value.detail["code"] == "dataset_busy"
            assert await contender.scalar(select(literal(1))) == 1
            await holder.rollback()
            assert (
                await lifecycle.require_writable(contender, dataset_id, lock=True)
            ).id == dataset_id
            await contender.rollback()
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(Dataset).where(Dataset.id == dataset_id))
            await cleanup.execute(delete(User).where(User.id == owner_id))
            await cleanup.commit()
