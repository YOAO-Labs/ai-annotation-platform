"""Private plan API, durable ownership and selected failed-object retries."""

import hashlib
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete

from app.core import security
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.schemas.storage_migration_copy import (
    StorageMigrationCopyAttemptRequest,
    StorageMigrationCopyRequest,
)
from app.services import (
    storage_migration_copies as service,
    storage_migration_inventories as inventories,
)
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.storage_location import location_is_referenced, register_connection
from app.services.storage_maintenance_impact import dependencies
from app.workers import storage_migration_copy as worker
from tests.test_storage_migration_inventories import report

PATH = "/api/v1/storage/migration-copies"


async def seed(db, actor):
    await ensure_legacy_bindings(db)
    inventory, job, _ = await inventories.start(db, actor)
    await inventories.claim(db, job.id, inventory.task_id)
    inventory.operation_id = uuid.uuid4()
    await inventories.publish(db, job.id, inventory.task_id, report=report(inventory))
    connection = StorageConnection(
        name="Copy target",
        kind="s3",
        scope="global",
        project_id=None,
        config={
            "endpoint": "http://127.0.0.1:9000",
            "bucket": "test-copy-target",
            "base_prefix": "copy",
        },
        secret_enc=b"encrypted",
    )
    db.add(connection)
    await db.flush()
    location = await register_connection(db, connection)
    body = StorageMigrationCopyRequest(
        inventory_job_id=job.id,
        target_location_id=location.id,
        handling=dict.fromkeys(inventories.PURPOSES, "migrate"),
    )
    return inventory, connection, location, body


@pytest.fixture
async def seeded(db_session, super_admin, monkeypatch):
    async def dispatch(db, job):
        await db.commit()

    monkeypatch.setattr(worker, "dispatch", dispatch)
    return await seed(db_session, super_admin[0])


@pytest.mark.parametrize("role", ["super_admin", "project_admin", "annotator"])
async def test_all_global_paths_and_shared_counts_require_current_admin(
    db_session, httpx_client, seeded, super_admin, project_admin, annotator, role
):
    _, token = {
        "super_admin": super_admin,
        "project_admin": project_admin,
        "annotator": annotator,
    }[role]
    headers = {"Authorization": f"Bearer {token}"}
    body = seeded[3]
    response = await httpx_client.post(
        PATH, json=body.model_dump(mode="json"), headers=headers
    )
    assert response.status_code == (202 if role == "super_admin" else 403)
    row, job, _ = await service.start(db_session, super_admin[0], body)
    for path in [
        PATH,
        f"{PATH}/{row.id}",
        f"{PATH}/{row.id}/failed-objects",
        f"/api/v1/storage/migration-copy-targets?inventory_job_id={body.inventory_job_id}",
        f"/api/v1/async-jobs/{job.id}",
    ]:
        assert (await httpx_client.get(path, headers=headers)).status_code == (
            200 if role == "super_admin" else 403
        )
    listing = await httpx_client.get(
        f"/api/v1/async-jobs?kind={service.KIND}", headers=headers
    )
    assert listing.json()["total"] == int(role == "super_admin")
    response = await httpx_client.post(
        f"{PATH}/{row.id}/generations",
        json={"inventory_job_id": str(body.inventory_job_id)},
        headers=headers,
    )
    assert response.status_code == (409 if role == "super_admin" else 403)


@pytest.mark.parametrize("scopes", [[], ["datasets:read"], ["*"]])
async def test_key_scope_protects_every_private_entry(
    db_session, httpx_client, seeded, super_admin, scopes
):
    body = seeded[3]
    row, job, _ = await service.start(db_session, super_admin[0], body)
    secret = "ak_" + uuid.uuid4().hex
    db_session.add(
        ApiKey(
            user_id=super_admin[0].id,
            name="Copy key",
            key_prefix=secret[:12],
            key_hash=security.hash_password(secret),
            scopes=scopes,
        )
    )
    await db_session.flush()
    headers = {"Authorization": f"Bearer {secret}"}
    for method, path, data in [
        ("GET", PATH, None),
        ("GET", f"{PATH}/{row.id}", None),
        ("GET", f"{PATH}/{row.id}/checkpoint", None),
        ("GET", f"{PATH}/{row.id}/failed-objects", None),
        (
            "GET",
            f"/api/v1/storage/migration-copy-targets?inventory_job_id={body.inventory_job_id}",
            None,
        ),
        ("GET", f"/api/v1/async-jobs/{job.id}", None),
        ("POST", PATH, body.model_dump(mode="json")),
        (
            "POST",
            f"{PATH}/{row.id}/generations",
            {"inventory_job_id": str(body.inventory_job_id)},
        ),
        ("POST", f"{PATH}/{row.id}/attempts", {"operation": "run"}),
        ("POST", f"{PATH}/{row.id}/dispatch", {}),
        ("POST", f"/api/v1/async-jobs/{job.id}/cancel", {}),
    ]:
        response = await httpx_client.request(method, path, json=data, headers=headers)
        assert (
            response.status_code != 403
            if scopes == ["*"]
            else response.status_code == 403
        )


async def test_prepare_is_explicit_reused_and_public_projection_is_closed(
    db_session, httpx_client, seeded, super_admin
):
    body = seeded[3]
    headers = {"Authorization": f"Bearer {super_admin[1]}"}
    first = await httpx_client.post(
        PATH, json=body.model_dump(mode="json"), headers=headers
    )
    assert first.status_code == 202, first.text
    item = first.json()["item"]
    assert {
        k: item[k]
        for k in (
            "parent_copy_id",
            "generation",
            "successor_copy_id",
            "parent_evidence_sha256",
            "read_only",
            "generation_summary",
        )
    } == {
        "parent_copy_id": None,
        "generation": 1,
        "successor_copy_id": None,
        "parent_evidence_sha256": None,
        "read_only": False,
        "generation_summary": None,
    }
    assert (
        item["operation"] == "prepare"
        and item["prepared"] is False
        and item["counts"] is None
    )
    second = await httpx_client.post(
        PATH, json=body.model_dump(mode="json"), headers=headers
    )
    assert (
        second.json()["reused"] and second.json()["item"]["copy_id"] == item["copy_id"]
    )
    raw = first.text
    for value in [
        "endpoint",
        "bucket",
        "base_prefix",
        "secret_enc",
        "target_key",
        "authority",
    ]:
        assert value not in raw
    assert (
        await httpx_client.get(f"{PATH}/{item['copy_id']}/checkpoint", headers=headers)
    ).status_code == 409
    for extra in ({"target_location_id": str(body.target_location_id)}, {"limits": {}}):
        assert (
            await httpx_client.post(
                f"{PATH}/{item['copy_id']}/generations",
                json={"inventory_job_id": str(body.inventory_job_id), **extra},
                headers=headers,
            )
        ).status_code == 422
    assert (
        await httpx_client.post(
            f"/api/v1/async-jobs/{item['job_id']}/cancel", json={}, headers=headers
        )
    ).json()["status"] == "cancelled"
    assert (
        await httpx_client.post(
            f"{PATH}/{item['copy_id']}/dispatch", json={}, headers=headers
        )
    ).status_code == 409


async def test_frozen_owner_survives_job_purge_and_protects_location(
    db_session, seeded, super_admin
):
    row, job, _ = await service.start(db_session, super_admin[0], seeded[3])
    await service.cancel(db_session, job.id)
    await db_session.execute(
        delete(AsyncJob).where(AsyncJob.id.in_([job.id, seeded[0].async_job_id]))
    )
    await db_session.flush()
    await db_session.refresh(row)
    a = await service.last(db_session, row)
    await db_session.refresh(a)
    assert row.source_inventory_job_id is None and a.async_job_id is None
    assert await location_is_referenced(db_session, row.target_location_id)
    impact = (
        (await db_session.execute(dependencies(row.target_location_id)))
        .mappings()
        .one()
    )
    assert impact["migration_copy_plans"] == 1
    value = await service.view(db_session, row.id, super_admin[0])
    assert value.job_id is None and value.status == "cancelled"
    values = await service.inputs(db_session, row.id)
    assert (
        hashlib.sha256(values["source_report"]).hexdigest() == row.source_report_sha256
    )


async def test_claim_once_pending_cancel_and_active_conflict(
    db_session, seeded, super_admin
):
    row, job, _ = await service.start(db_session, super_admin[0], seeded[3])
    a = await service.claim(db_session, job.id, job.celery_task_id)
    assert a and await service.claim(db_session, job.id, job.celery_task_id) is None
    assert await service.claim(db_session, job.id, "wrong") is None
    with pytest.raises(HTTPException, match="409"):
        await service.attempt(
            db_session,
            row.id,
            super_admin[0],
            StorageMigrationCopyAttemptRequest(operation="run"),
        )
    assert await service.cancel(db_session, job.id) == "cancel_requested"
    assert (await service.last(db_session, row)).status == "running"


@pytest.mark.parametrize("change", ["location", "connection", "source", "actor"])
async def test_fresh_context_stops_revision_or_authority_changes(
    db_session, seeded, super_admin, change
):
    row, job, _ = await service.start(db_session, super_admin[0], seeded[3])
    await service.claim(db_session, job.id, job.celery_task_id)
    if change == "location":
        seeded[2].config_revision += 1
    elif change == "connection":
        seeded[1].revision += 1
    elif change == "source":
        (
            await db_session.get(
                StorageLocation, service.inventories.LEGACY_LOCATION_ID
            )
        ).config_revision += 1
    else:
        super_admin[0].role = "annotator"
    await db_session.flush()
    with pytest.raises(HTTPException):
        await service.checkpoint(db_session, job.id, job.celery_task_id)


@pytest.mark.parametrize(
    "body",
    [
        {"handling": {}},
        {"limits": {"max_object_bytes": True}},
        {"limits": {"max_objects": 100001}},
        {"limits": {"max_seconds": 3601}},
        {"limits": {"read_timeout_seconds": 61}},
        {"limits": {"max_source_bytes": 0}},
        {"endpoint_url": "http://raw"},
    ],
)
async def test_input_limits_and_fixed_source_reject_before_dispatch(
    httpx_client, seeded, super_admin, body
):
    data = seeded[3].model_dump(mode="json") | body
    response = await httpx_client.post(
        PATH, json=data, headers={"Authorization": f"Bearer {super_admin[1]}"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"operation": "verify", "retry_failed": True},
        {"operation": "run", "retry_object_ids": ["a" * 64]},
        {"operation": "run", "retry_failed": True, "retry_object_ids": []},
        {"operation": "run", "retry_failed": True, "retry_object_ids": ["a" * 64] * 2},
        {"operation": "run", "retry_failed": True, "retry_object_ids": ["raw-key"]},
    ],
)
def test_selected_retry_contract_is_bounded_and_explicit(body):
    with pytest.raises(ValueError):
        StorageMigrationCopyAttemptRequest(**body)


def test_process_death_requires_exact_host_boot_namespace_and_pid_start():
    proof = service.process_identity()
    assert proof and not service.proven_dead(proof)
    assert service.proven_dead({**proof, "start": "replaced"})
    assert not service.proven_dead({**proof, "boot": "other"})
    assert not service.proven_dead({**proof, "pid_namespace": -1})
    assert not service.proven_dead(None)
