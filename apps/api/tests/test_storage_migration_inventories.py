"""Fixed-source job authority, claim-once lifecycle and private bounded reports."""

import hashlib
import json
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core import security
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.notification import Notification
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_migration_inventory import (
    StorageMigrationInventory as Inventory,
)
from app.schemas.storage_migration_inventory import StorageMigrationInventoryRequest
from app.services import storage_migration_inventories as service
from app.services.async_job_notify import notify_job_terminal
from app.services.storage_migration_inventory import InventoryLimits, write_inventory
from app.workers import storage_migration_inventory as worker

PATH = "/api/v1/storage/migration-inventories"


@pytest.fixture(autouse=True)
async def configured_source(db_session):
    from app.services.dataset_assets import ensure_legacy_bindings

    await ensure_legacy_bindings(db_session)


class EmptySource:
    def get_bucket_versioning(self, **kwargs):
        return {}

    def get_bucket_lifecycle_configuration(self, **kwargs):
        return {"Rules": []}

    def list_objects_v2(self, **kwargs):
        return {"IsTruncated": False, "Contents": []}

    def list_multipart_uploads(self, **kwargs):
        return {"IsTruncated": False, "Uploads": []}


def report(row, *, partial=False):
    source = EmptySource()
    if partial:

        def failed(**kwargs):
            raise ValueError("private endpoint and credential")

        source.list_objects_v2 = failed
    sink = worker.ReportSink()
    write_inventory(
        None,
        source,
        row.source["purpose_buckets"],
        source={"endpoint": row.source["endpoint"]},
        limits=InventoryLimits(
            **{key: value for key, value in row.limits.items() if key != "max_seconds"}
        ),
        handling=row.handling,
        sink=sink,
    )
    return sink.value


async def running(db, actor):
    row, job, reused = await service.start(db, actor)
    assert not reused
    assert await service.claim(db, job.id, row.task_id) is row
    row.operation_id = uuid.uuid4()
    await db.flush()
    return row, job


@pytest.mark.parametrize("identity", ["super_admin", "project_admin", "annotator"])
async def test_global_inventory_api_and_shared_job_scope(
    db_session,
    httpx_client,
    super_admin,
    project_admin,
    annotator,
    monkeypatch,
    identity,
):
    monkeypatch.setattr(worker, "dispatch", AsyncMock())
    actor, token = {
        "super_admin": super_admin,
        "project_admin": project_admin,
        "annotator": annotator,
    }[identity]
    response = await httpx_client.post(
        PATH, json={}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == (202 if identity == "super_admin" else 403)
    job = await service.jobs.create_job(db_session, kind=service.KIND, user_id=actor.id)
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        await httpx_client.get(f"/api/v1/async-jobs/{job.id}", headers=headers)
    ).status_code == (200 if identity == "super_admin" else 403)
    listing = await httpx_client.get(
        f"/api/v1/async-jobs?kind={service.KIND}", headers=headers
    )
    assert listing.status_code == 200
    assert listing.json()["total"] == (2 if identity == "super_admin" else 0)
    assert (await httpx_client.get(PATH, headers=headers)).status_code == (
        200 if identity == "super_admin" else 403
    )


@pytest.mark.parametrize("scopes", [[], ["datasets:read", "datasets:write"], ["*"]])
async def test_narrow_api_keys_cannot_read_create_dispatch_report_or_cancel(
    db_session, httpx_client, super_admin, monkeypatch, scopes
):
    monkeypatch.setattr(worker, "dispatch", AsyncMock())
    actor, _ = super_admin
    row, job, _ = await service.start(db_session, actor)
    await db_session.refresh(job)
    secret = "ak_" + uuid.uuid4().hex
    db_session.add(
        ApiKey(
            user_id=actor.id,
            name="Inventory test",
            key_prefix=secret[:12],
            key_hash=security.hash_password(secret),
            scopes=scopes,
        )
    )
    await db_session.flush()
    headers = {"Authorization": f"Bearer {secret}"}
    allowed = scopes == ["*"]
    for method, path, body in [
        ("GET", PATH, None),
        ("GET", f"/api/v1/async-jobs/{job.id}", None),
        ("GET", f"{PATH}/{job.id}/report", None),
        ("POST", f"{PATH}/{job.id}/dispatch", {}),
        ("POST", PATH, {}),
        ("POST", f"/api/v1/async-jobs/{job.id}/cancel", {}),
    ]:
        response = await httpx_client.request(method, path, json=body, headers=headers)
        assert response.status_code != 403 if allowed else response.status_code == 403
    listing = await httpx_client.get(
        f"/api/v1/async-jobs?kind={service.KIND}", headers=headers
    )
    assert listing.json()["total"] == int(allowed)


async def test_start_reuses_exact_input_and_conflicts_for_other_inputs_or_actor(
    db_session, super_admin, monkeypatch
):
    actor, _ = super_admin
    row, job, reused = await service.start(db_session, actor)
    assert not reused
    again, same, reused = await service.start(
        db_session, actor, StorageMigrationInventoryRequest()
    )
    assert reused and again is row and same is job
    with pytest.raises(HTTPException) as caught:
        await service.start(
            db_session,
            actor,
            StorageMigrationInventoryRequest(handling={"datasets": "retain"}),
        )
    assert caught.value.detail == {"code": "inventory_active_conflict"}
    # Public handling keys are purpose names, not private bucket locators.
    payload = service.item(row).model_dump_json(exclude={"handling"})
    for private in [
        row.source["endpoint"],
        *row.source["purpose_buckets"].values(),
        "authority",
        '"source":',
    ]:
        assert private not in payload
    assert service.item(row).handling["datasets"] == "unreviewed"


@pytest.mark.parametrize(
    "body",
    [
        {"endpoint": "http://arbitrary"},
        {"handling": {"other": "migrate"}},
        {"handling": {"datasets": "unreviewed"}},
        {"limits": {"max_seconds": 3601}},
        {"limits": {"page_size": True}},
        {"limits": {"max_total_bytes": 16 * 1024**3 + 1}},
    ],
)
async def test_no_arbitrary_sources_credentials_or_unbounded_limits(
    httpx_client, super_admin, body
):
    response = await httpx_client.post(
        PATH, json=body, headers={"Authorization": f"Bearer {super_admin[1]}"}
    )
    assert response.status_code == 422


async def test_pending_dispatch_is_explicit_same_id_and_running_delivery_claims_once(
    db_session, super_admin, monkeypatch
):
    row, job, _ = await service.start(db_session, super_admin[0])
    published = []
    monkeypatch.setattr(worker, "_publish", lambda *args: published.append(args))
    await worker.dispatch(db_session, job)
    await worker.dispatch(db_session, job)
    assert published == [(job.id, row.task_id)] * 2
    assert await service.claim(db_session, job.id, "another-task") is None
    assert await service.claim(db_session, job.id, row.task_id)
    assert await service.claim(db_session, job.id, row.task_id) is None
    await worker.dispatch(db_session, job)
    assert len(published) == 2


async def test_binding_moved_to_b_is_reported_coverage_not_source_gate(
    db_session, super_admin
):
    binding = await db_session.get(StorageBinding, "datasets")
    # A non-source binding observation does not grant access to that location.
    binding.bucket = "other-source-bucket"
    binding.generation += 1
    await db_session.flush()
    row, _, _ = await service.start(db_session, super_admin[0])
    assert row.source["purpose_buckets"]["datasets"] != binding.bucket


@pytest.mark.parametrize("partial", [False, True])
async def test_sealed_report_publishes_safe_summary_and_private_download_without_source_io(
    db_session, httpx_client, super_admin, monkeypatch, partial
):
    row, job = await running(db_session, super_admin[0])
    raw = report(row, partial=partial)
    assert await service.publish(db_session, job.id, row.task_id, report=raw)
    assert row.status == ("failed" if partial else "completed")
    assert row.complete is not partial
    assert row.error_code == ("inventory_incomplete" if partial else None)
    assert "key" not in json.dumps(job.result) and row.source[
        "endpoint"
    ] not in json.dumps(job.result)
    monkeypatch.setattr(
        worker, "scan", lambda *args: pytest.fail("GET performed source IO")
    )
    monkeypatch.setattr(
        service,
        "source_scope",
        AsyncMock(side_effect=AssertionError("GET source context IO")),
    )
    response = await httpx_client.get(
        f"{PATH}/{job.id}/report", headers={"Authorization": f"Bearer {super_admin[1]}"}
    )
    assert response.status_code == 200 and response.content == raw
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-sha256"] == hashlib.sha256(raw).hexdigest()
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="storage-migration-inventory.jsonl"'
    )
    history = await httpx_client.get(
        PATH, headers={"Authorization": f"Bearer {super_admin[1]}"}
    )
    assert history.status_code == 200 and history.json()["items"][0]["report_available"]
    await notify_job_terminal(db_session, job_id=job.id)
    await notify_job_terminal(db_session, job_id=job.id)
    notifications = list(
        await db_session.scalars(
            select(Notification).where(Notification.target_id == job.id)
        )
    )
    assert len(notifications) == 1
    assert notifications[0].payload["counts"] == row.counts
    assert row.source["endpoint"] not in json.dumps(notifications[0].payload)


@pytest.mark.parametrize("change", ["role", "source", "cancel", "unsettled"])
async def test_final_fences_never_publish_report_after_revocation_or_unsettled_close(
    db_session, super_admin, change
):
    row, job = await running(db_session, super_admin[0])
    raw = report(row)
    if change == "role":
        super_admin[0].role = "project_admin"
    elif change == "source":
        location = await db_session.get(StorageLocation, service.LEGACY_LOCATION_ID)
        location.config_revision += 1
    elif change == "cancel":
        assert await service.cancel(db_session, job.id) == "cancel_requested"
    else:
        db_session.add(
            StorageOperation(
                id=row.operation_id,
                location_id=service.LEGACY_LOCATION_ID,
                revision=row.maintenance_revision,
                purpose=service.KIND,
                node_id="test",
            )
        )
    await db_session.flush()
    assert await service.publish(db_session, job.id, row.task_id, report=raw)
    assert row.status == ("cancelled" if change == "cancel" else "failed")
    assert not service.item(row).report_available
    if change == "unsettled":
        assert row.error_code == "storage_operation_unsettled"
        assert await db_session.get(StorageOperation, row.operation_id)


async def test_digest_corruption_and_unsealed_or_wrong_input_never_download(
    db_session, super_admin
):
    row, job = await running(db_session, super_admin[0])
    assert await service.publish(db_session, job.id, row.task_id, report=b"unsealed\n")
    assert row.error_code == "inventory_invalid_result"
    with pytest.raises(HTTPException) as caught:
        await service.download(db_session, job.id, super_admin[0])
    assert caught.value.detail["code"] == "inventory_report_unavailable"
    row, job = await running(db_session, super_admin[0])
    raw = report(row)
    await service.publish(db_session, job.id, row.task_id, report=raw)
    row.report = raw[:-1] + b" "
    await db_session.flush()
    with pytest.raises(HTTPException) as caught:
        await service.download(db_session, job.id, super_admin[0])
    assert caught.value.detail["code"] == "inventory_report_integrity"


async def test_pending_cancel_revoke_owner_and_report_cascade_retention(
    db_session, httpx_client, super_admin
):
    row, job, _ = await service.start(db_session, super_admin[0])
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{job.id}/cancel",
        headers={"Authorization": f"Bearer {super_admin[1]}"},
    )
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert await service.claim(db_session, job.id, row.task_id) is None
    await db_session.execute(delete(AsyncJob).where(AsyncJob.id == job.id))
    assert (
        await db_session.scalar(
            select(Inventory).where(Inventory.async_job_id == job.id)
        )
        is None
    )


async def test_original_owner_downgrade_loses_every_global_inventory_path(
    db_session, httpx_client, super_admin
):
    actor, token = super_admin
    row, job, _ = await service.start(db_session, actor)
    actor.role = "project_admin"
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    for method, path in [
        ("GET", PATH),
        ("GET", f"{PATH}/{job.id}/report"),
        ("POST", f"{PATH}/{job.id}/dispatch"),
        ("GET", f"/api/v1/async-jobs/{job.id}"),
        ("POST", f"/api/v1/async-jobs/{job.id}/cancel"),
    ]:
        assert (
            await httpx_client.request(method, path, headers=headers)
        ).status_code == 403
    listing = await httpx_client.get(
        f"/api/v1/async-jobs?kind={service.KIND}", headers=headers
    )
    assert listing.json()["total"] == 0


@pytest.mark.parametrize("stage", ["pending", "running"])
async def test_saved_key_revocation_is_rechecked_before_claim_and_publish(
    db_session, super_admin, stage
):
    actor, _ = super_admin
    key = ApiKey(
        user_id=actor.id,
        name="Inventory authority",
        key_prefix="ak_inventory",
        key_hash=security.hash_password(uuid.uuid4().hex),
        scopes=["*"],
    )
    db_session.add(key)
    await db_session.flush()
    row, job, _ = await service.start(db_session, actor, api_key_id=key.id)
    if stage == "running":
        assert await service.claim(db_session, job.id, row.task_id)
        row.operation_id = uuid.uuid4()
        raw = report(row)
    key.scopes = ["datasets:read"]
    await db_session.flush()
    if stage == "pending":
        assert await service.claim(db_session, job.id, row.task_id) is None
    else:
        assert await service.publish(db_session, job.id, row.task_id, report=raw)
    assert row.status == "failed" and not service.item(row).report_available


@pytest.mark.parametrize("cancelled", [False, True])
async def test_running_signal_keeps_physical_receipt_and_no_report(
    db_session, super_admin, cancelled
):
    row, job = await running(db_session, super_admin[0])
    db_session.add(
        StorageOperation(
            id=row.operation_id,
            location_id=service.LEGACY_LOCATION_ID,
            revision=row.maintenance_revision,
            purpose=service.KIND,
            node_id="test",
        )
    )
    await db_session.flush()
    assert await service.signal_interrupted(
        db_session, job.id, row.task_id, cancelled=cancelled
    )
    assert row.status == "failed" and row.error_code == "inventory_interrupted"
    assert await db_session.get(StorageOperation, row.operation_id)
    assert not await service.signal_interrupted(
        db_session, job.id, row.task_id, cancelled=cancelled
    )


async def test_private_report_database_cap_and_job_fk_are_enforced(
    db_session, super_admin
):
    row, job = await running(db_session, super_admin[0])
    raw = report(row)
    await service.publish(db_session, job.id, row.task_id, report=raw)
    async with db_session.begin_nested():
        row.report_size_bytes += 1
        with pytest.raises(IntegrityError):
            await db_session.flush()
