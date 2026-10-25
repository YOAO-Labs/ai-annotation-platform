"""Frozen CPU previews, durable fences, verified evidence and bounded cleanup."""

import copy
import hashlib
import io
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select
from training_contract import definition
from training_contract.catalog import supported_identities
from training_contract.preview import POLICY, POLICY_DIGEST, closed_mosaic

from app.db.models.training_preview import TrainingPreview, TrainingPreviewResourceLease
from app.db.models.training_run import TrainingResourceLease
from app.schemas.training_preview import PreviewClaim, PreviewFinish, PreviewUploads
from app.schemas.training_run import ExecutorFence
from app.services import training_artifacts, training_previews as service
from tests.factory import create_membership, create_project
from tests.test_training_dataset_analysis import inputs
from tests.test_training_runs import (
    GPU,
    IMAGE,
    ready_dataset,
    training_config as training_config,
)


def claim_body(**changes):
    body = PreviewClaim(
        purpose="augmentation-preview",
        executor_id="preview-executor",
        boot_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        resource_id="test/augmentation-preview/cpu-0",
        runtime={
            "executor_protocol": 2,
            "supported_configs": supported_identities(),
            "preview_protocol": 1,
            "preview_policy_digest": POLICY_DIGEST,
            "image_digest": IMAGE,
            "gpu_uuid": GPU,
            "frameworks": {"ultralytics": "8.4.177"},
        },
    )
    return body.model_copy(update=changes)


def fence(claim, assignment):
    return dict(
        executor_id=claim.executor_id,
        boot_id=claim.boot_id,
        lease_token=assignment["lease_token"],
    )


async def preview(db, client, owner, *, boxes=1, create=True):
    user, token = owner
    project, dataset = await ready_dataset(db, user)
    dataset.snapshot, dataset.manifest = inputs(
        [{"content": "a", "labels": ["car"] * boxes}, {"content": "b", "split": "val"}],
        classes=["car"],
    )
    await db.flush()
    assert await service.claim(db, claim_body()) is None
    schema = definition("ultralytics", "yolo11n", version="8.4.177")
    body = dict(
        dataset_id=str(dataset.id),
        framework="ultralytics",
        base_model="yolo11n",
        configuration=dict(
            schema_id=schema["schema_id"],
            schema_digest=schema["schema_digest"],
            values={"epochs": 3, "imgsz": 128},
        ),
        seed=42,
        epoch=1,
        count=1,
        request_id=str(uuid.uuid4()),
    )
    path = f"/api/v1/projects/{project.id}/training-previews"
    headers = {"Authorization": f"Bearer {token}"}
    if not create:
        return project, dataset, path, headers, body
    response = await client.post(path, headers=headers, json=body)
    assert response.status_code == 202, response.text
    row = await db.get(TrainingPreview, uuid.UUID(response.json()["id"]))
    return row, path, headers, body


class Objects:
    def __init__(self):
        from app.config import settings

        scheme = "https" if settings.minio_use_ssl else "http"
        self.meta = SimpleNamespace(
            endpoint_url=f"{scheme}://{settings.minio_endpoint}"
        )
        self.objects = {}
        self.reads = 0

    def generate_presigned_url(self, operation, **kwargs):
        return "http://owned-storage/" + operation + "/" + kwargs["Params"]["Key"]

    def get_object(self, *, Key, **kwargs):
        self.reads += 1
        raw, metadata = self.objects[Key]
        return dict(Body=io.BytesIO(raw), Metadata=metadata, ContentLength=len(raw))


def evidence(row, objects, *, change=None):
    buffer = io.BytesIO()
    Image.new("RGB", (16, 12), "orange").save(buffer, format="PNG")
    png = buffer.getvalue()
    frozen = row.spec
    source = frozen["sources"][0]["task_id"]
    transforms = {
        key: frozen["native_values"][key]
        for key in ("mosaic", "mixup", "cutmix", "copy_paste")
    }
    if closed_mosaic(frozen["config"], frozen["epoch"]):
        transforms = {key: 0.0 for key in transforms}
    report = dict(
        revision=1,
        preview_id=str(row.id),
        input_digest=row.input_digest,
        config_digest=frozen["config_digest"],
        policy=POLICY,
        policy_digest=POLICY_DIGEST,
        seed=frozen["seed"],
        epoch=frozen["epoch"],
        mosaic_closed=closed_mosaic(frozen["config"], frozen["epoch"]),
        native_parameters=frozen["native_values"],
        transform_parameters=transforms,
        source_pool=frozen["sources"],
        pool_summary=frozen["pool_summary"],
        runtime=dict(
            framework_version="8.4.177",
            image_digest=IMAGE,
            adapter_revision=2,
            pipeline_sha256="f" * 64,
            cuda_initialized=False,
        ),
        outputs=[
            dict(
                index=0,
                image="preview-0.png",
                sha256=hashlib.sha256(png).hexdigest(),
                width=16,
                height=12,
                boxes=[dict(class_index=0, xywh=[0.5, 0.5, 0.5, 0.5])],
                source_draws=[source],
                sources_read=[source],
                operations=[],
                filtered=0,
                clipped=0,
            )
        ],
    )
    if change:
        change(report)
    blobs = {"preview-0.png": png, "report.json": json.dumps(report).encode()}
    inventory = []
    for name, raw in blobs.items():
        sha = hashlib.sha256(raw).hexdigest()
        inventory.append(dict(name=name, sha256=sha, size_bytes=len(raw)))
        objects.objects[service.prefix(row) + name] = (
            raw,
            {
                "preview-id": str(row.id),
                "input-digest": row.input_digest,
                "sha256": sha,
            },
        )
    return inventory


async def claimed(db, row):
    claim = claim_body()
    assignment = await service.claim(db, claim)
    assert assignment["preview_id"] == str(row.id)
    return claim, assignment


async def test_idle_recovery_after_upgrade_preserves_lease_until_fresh_claim(
    db_session,
    super_admin,
    httpx_client,
    training_config,
):
    row, *_ = await preview(db_session, httpx_client, super_admin)
    lease = await db_session.get(TrainingPreviewResourceLease, service.resource_id())
    old_runtime = {**claim_body().runtime, "image_digest": "sha256:" + "0" * 64}
    old_claim = claim_body(runtime=old_runtime, recover_only=True)
    lease.executor_id, lease.boot_id, lease.runtime, lease.last_seen_at = (
        old_claim.executor_id,
        old_claim.boot_id,
        old_runtime,
        service.now() - timedelta(hours=1),
    )
    previous = (lease.executor_id, lease.boot_id, lease.runtime, lease.last_seen_at)
    await db_session.flush()

    assert await service.claim(db_session, old_claim) is None
    assert (
        lease.executor_id,
        lease.boot_id,
        lease.runtime,
        lease.last_seen_at,
    ) == previous
    assert lease.preview_id is None and row.status == "queued"
    with pytest.raises(HTTPException) as rejected:
        await service.claim(
            db_session, old_claim.model_copy(update={"recover_only": False})
        )
    assert rejected.value.status_code == 409
    with pytest.raises(HTTPException) as wrong_resource:
        await service.claim(
            db_session, old_claim.model_copy(update={"resource_id": "other"})
        )
    assert wrong_resource.value.status_code == 409

    current = claim_body()
    assignment = await service.claim(db_session, current)
    assert assignment["preview_id"] == str(row.id)
    assert lease.preview_id == row.id and row.status == "running"
    assert lease.boot_id == current.boot_id and lease.runtime == current.runtime
    assert lease.last_seen_at > previous[3]


@pytest.mark.parametrize("same_owner", [True, False])
async def test_old_runtime_recovery_preserves_active_preview_ownership(
    db_session,
    super_admin,
    httpx_client,
    training_config,
    same_owner,
):
    row, *_ = await preview(db_session, httpx_client, super_admin)
    original, assignment = await claimed(db_session, row)
    lease = await db_session.get(TrainingPreviewResourceLease, service.resource_id())
    old_runtime = {**original.runtime, "image_digest": "sha256:" + "0" * 64}
    row.runtime = lease.runtime = old_runtime
    old_claim = original.model_copy(
        update={"runtime": old_runtime, "recover_only": True}
    )
    if not same_owner:
        old_claim = old_claim.model_copy(update={"boot_id": uuid.uuid4()})
    previous = (lease.executor_id, lease.boot_id, lease.runtime, lease.last_seen_at)
    await db_session.flush()

    recovered = await service.claim(db_session, old_claim)
    assert recovered == (assignment if same_owner else None)
    assert lease.preview_id == row.id and row.status == "running"
    assert (
        lease.executor_id,
        lease.boot_id,
        lease.runtime,
        lease.last_seen_at,
    ) == previous
    assert row.boot_id == original.boot_id and row.claim_id == original.claim_id
    assert (
        row.lease_digest
        == hashlib.sha256(assignment["lease_token"].encode()).hexdigest()
    )


async def test_create_is_frozen_idempotent_scoped_and_blocks_dataset_delete(
    db_session,
    super_admin,
    annotator,
    httpx_client,
    training_config,
):
    training_leases_before = {
        lease.resource_id: lease.attempt_id
        for lease in (await db_session.scalars(select(TrainingResourceLease))).all()
    }
    row, path, headers, body = await preview(db_session, httpx_client, super_admin)
    frozen = copy.deepcopy(row.spec)
    repeated = await httpx_client.post(path, headers=headers, json=body)
    assert repeated.status_code == 202 and repeated.json()["id"] == str(row.id)
    changed = await httpx_client.post(path, headers=headers, json={**body, "seed": 43})
    assert changed.status_code == 409 and row.spec == frozen
    deleted = await httpx_client.delete(
        f"/api/v1/projects/{row.project_id}/training-datasets/{row.dataset_id}",
        headers=headers,
    )
    assert deleted.status_code == 409
    member, token = annotator
    await create_membership(
        db_session, project_id=row.project_id, user_id=member.id, role="annotator"
    )
    forbidden = await httpx_client.get(
        path + f"/{row.id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert forbidden.status_code == 403
    other = await create_project(db_session, owner_id=super_admin[0].id)
    assert (
        await httpx_client.get(
            f"/api/v1/projects/{other.id}/training-previews/{row.id}", headers=headers
        )
    ).status_code == 404
    assert {
        lease.resource_id: lease.attempt_id
        for lease in (await db_session.scalars(select(TrainingResourceLease))).all()
    } == training_leases_before


async def test_claim_replay_fences_lost_boot_and_keeps_cpu_until_exit(
    db_session,
    super_admin,
    httpx_client,
    training_config,
):
    row, *_ = await preview(db_session, httpx_client, super_admin)
    claim, assignment = await claimed(db_session, row)
    assert await service.claim(db_session, claim) == assignment
    assert await service.claim(db_session, claim_body()) is None
    row.heartbeat_at = service.now() - timedelta(hours=1)
    await db_session.flush()
    assert (await service.output(row, db=db_session)).status == "lost"
    lease = await db_session.get(TrainingPreviewResourceLease, service.resource_id())
    assert lease.preview_id == row.id
    with pytest.raises(HTTPException) as stale:
        await service.heartbeat(
            db_session,
            row.id,
            ExecutorFence(**{**fence(claim, assignment), "boot_id": uuid.uuid4()}),
        )
    assert stale.value.status_code == 409
    await service.cancel(db_session, row)
    assert (
        await service.heartbeat(
            db_session, row.id, ExecutorFence(**fence(claim, assignment))
        )
    )["stop"]
    body = PreviewFinish(
        **fence(claim, assignment), status="succeeded", process_exited=True
    )
    assert (await service.finish(db_session, row.id, body)).status == "cancelled"
    assert lease.preview_id is None
    assert (await service.finish(db_session, row.id, body)).status == "cancelled"
    with pytest.raises(HTTPException):
        await service.finish(
            db_session, row.id, body.model_copy(update={"status": "failed"})
        )


async def test_dense_256_box_report_publishes_and_cache_still_verifies_storage(
    db_session,
    super_admin,
    httpx_client,
    training_config,
    monkeypatch,
):
    objects = Objects()
    monkeypatch.setattr(service.storage, "client", objects)
    row, path, headers, _ = await preview(
        db_session, httpx_client, super_admin, boxes=256
    )
    claim, assignment = await claimed(db_session, row)
    assert len(row.spec["sources"][0]["labels"]) > 8192
    inventory = evidence(row, objects)
    await service.uploads(
        db_session,
        row.id,
        PreviewUploads(**fence(claim, assignment), artifacts=inventory),
    )
    finished = await service.finish(
        db_session,
        row.id,
        PreviewFinish(
            **fence(claim, assignment), status="succeeded", process_exited=True
        ),
    )
    assert finished.status == "succeeded"
    response = await httpx_client.get(path + f"/{row.id}", headers=headers)
    assert response.status_code == 200, response.text
    assert (
        len(response.json()["report"]["source_pool"][0]["labels"].splitlines()) == 256
    )
    image = await httpx_client.get(path + f"/{row.id}/images/0", headers=headers)
    assert (
        image.status_code == 200
        and image.headers["cache-control"] == "private, no-store"
    )
    report_spec = finished.artifacts["report.json"]
    with pytest.raises(ValueError, match="string"):
        training_artifacts.read_json_artifact(
            report_spec, row.id, metadata_key="preview-id"
        )
    raw, metadata = objects.objects[report_spec["key"]]
    objects.objects[report_spec["key"]] = (raw, {**metadata, "input-digest": "0" * 64})
    assert (
        await httpx_client.get(path + f"/{row.id}", headers=headers)
    ).status_code == 422


@pytest.mark.parametrize("corruption", ["seed", "source", "class", "counts", "image"])
async def test_publish_rejects_mismatched_report_or_image_and_retains_lease(
    db_session,
    super_admin,
    httpx_client,
    training_config,
    monkeypatch,
    corruption,
):
    objects = Objects()
    monkeypatch.setattr(service.storage, "client", objects)
    row, *_ = await preview(db_session, httpx_client, super_admin)
    claim, assignment = await claimed(db_session, row)

    def change(report):
        if corruption == "seed":
            report["seed"] += 1
        elif corruption == "source":
            report["outputs"][0]["source_draws"] = [str(uuid.uuid4())]
        elif corruption == "class":
            report["outputs"][0]["boxes"][0]["class_index"] = 9
        elif corruption == "counts":
            report["outputs"][0]["filtered"] = 1

    inventory = evidence(row, objects, change=change)
    await service.uploads(
        db_session,
        row.id,
        PreviewUploads(**fence(claim, assignment), artifacts=inventory),
    )
    if corruption == "image":
        key = service.prefix(row) + "preview-0.png"
        raw, metadata = objects.objects[key]
        objects.objects[key] = (raw + b"changed", metadata)
    with pytest.raises(HTTPException) as failure:
        await service.finish(
            db_session,
            row.id,
            PreviewFinish(
                **fence(claim, assignment), status="succeeded", process_exited=True
            ),
        )
    assert failure.value.status_code == 422
    assert (
        await db_session.get(TrainingPreviewResourceLease, service.resource_id())
    ).preview_id == row.id
    assert not row.artifacts


async def test_deadline_and_revocation_stop_without_releasing_unknown_process(
    db_session,
    super_admin,
    httpx_client,
    training_config,
    monkeypatch,
):
    row, *_ = await preview(db_session, httpx_client, super_admin)
    claim, assignment = await claimed(db_session, row)
    row.started_at = service.now() - timedelta(seconds=61)
    result = await service.heartbeat(
        db_session, row.id, ExecutorFence(**fence(claim, assignment))
    )
    assert result["stop"] and row.status == "cancelling"
    lease = await db_session.get(TrainingPreviewResourceLease, service.resource_id())
    assert lease.preview_id == row.id

    async def revoked(*args):
        raise HTTPException(status_code=403)

    monkeypatch.setattr(service.training, "authority", revoked)
    done = await service.finish(
        db_session,
        row.id,
        PreviewFinish(**fence(claim, assignment), status="failed", process_exited=True),
    )
    assert done.status == "cancelled" and lease.preview_id is None


async def test_expiry_hides_results_and_cleanup_waits_for_put_window(
    db_session,
    super_admin,
    httpx_client,
    training_config,
    monkeypatch,
):
    row, path, headers, _ = await preview(db_session, httpx_client, super_admin)
    await service.cancel(db_session, row)
    row.expires_at = service.now() - timedelta(seconds=1)
    row.put_expires_at = service.now() + timedelta(seconds=60)
    removed = []
    monkeypatch.setattr(
        service.storage,
        "delete_prefix",
        lambda prefix, **kwargs: removed.append(prefix),
    )
    assert (await httpx_client.get(path + f"/{row.id}", headers=headers)).json()[
        "status"
    ] == "expired"
    assert (
        await httpx_client.get(path + f"/{row.id}/images/0", headers=headers)
    ).status_code == 410
    assert (await service.cleanup(db_session))["removed"] == 0 and not removed
    row.put_expires_at = service.now() - timedelta(seconds=1)
    assert (await service.cleanup(db_session))["removed"] == 1
    await db_session.flush()
    assert removed == [service.prefix(row)]
    assert await db_session.get(TrainingPreview, row.id) is None


def test_pool_bounds_and_missing_labels_are_explicit():
    snapshot, manifest = inputs(
        [
            {"content": "oversize", "width": 16000001},
            {"content": "missing"},
            {"content": "negative", "labels": []},
            {"content": "val", "split": "val"},
        ],
        classes=["car"],
    )
    snapshot["samples"][1].pop("annotations")
    from tests.test_training_preview_storage_flow import reference

    version = SimpleNamespace(
        snapshot=snapshot, manifest=manifest, project_id=uuid.uuid4(), id=uuid.uuid4()
    )
    version.storage_ref = reference(
        version.project_id, version.id, purpose="training-dataset"
    )
    selected, summary = service.select_sources(version, 42)
    assert len(selected) == 1 and selected[0]["labels"] == ""
    assert summary["selected"] == 1 and sum(summary["skipped"].values()) == 2


async def test_api_key_scopes_and_revocation_are_rechecked_during_execution(
    db_session,
    super_admin,
    httpx_client,
    training_config,
):
    from app.services.api_key_service import create_key

    _, _, path, _, body = await preview(
        db_session, httpx_client, super_admin, create=False
    )
    key, token = await create_key(
        db_session, super_admin[0], "preview", ["datasets:read", "annotations:read"]
    )
    headers = {"Authorization": "Bearer " + token}
    assert (
        await httpx_client.post(path, headers=headers, json=body)
    ).status_code == 403
    key.scopes = ["datasets:read", "datasets:write", "annotations:read"]
    await db_session.flush()
    result = await httpx_client.post(path, headers=headers, json=body)
    assert result.status_code == 202, result.text
    row = await db_session.get(TrainingPreview, uuid.UUID(result.json()["id"]))
    claim, assignment = await claimed(db_session, row)
    key.revoked_at = service.now()
    await db_session.flush()
    assert (
        await service.heartbeat(
            db_session, row.id, ExecutorFence(**fence(claim, assignment))
        )
    )["stop"]
    assert (
        await db_session.get(TrainingPreviewResourceLease, service.resource_id())
    ).preview_id == row.id
    assert (
        await service.finish(
            db_session,
            row.id,
            PreviewFinish(
                **fence(claim, assignment), status="failed", process_exited=True
            ),
        )
    ).status == "cancelled"


async def test_slow_expired_storage_cleanup_does_not_block_active_cpu_heartbeat(
    test_engine,
    training_config,
    monkeypatch,
):
    import asyncio
    import threading

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models.async_job import AsyncJob
    from app.db.models.project import Project
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.user import User
    from app.schemas.training_preview import TrainingPreviewCreate
    from tests.factory import create_user

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as seed:
        owner = await create_user(
            seed,
            "super_admin",
            f"preview-race-{uuid.uuid4()}@test.local",
            "Preview race",
        )
        project, dataset = await ready_dataset(seed, owner)
        dataset.snapshot, dataset.manifest = inputs(
            [{"content": "a"}, {"content": "b", "split": "val"}], classes=["car"]
        )
        await seed.flush()
        await service.claim(seed, claim_body())
        schema = definition("ultralytics", "yolo11n", version="8.4.177")
        body = TrainingPreviewCreate(
            dataset_id=dataset.id,
            framework="ultralytics",
            base_model="yolo11n",
            configuration=dict(
                schema_id=schema["schema_id"],
                schema_digest=schema["schema_digest"],
                values={"epochs": 3},
            ),
            request_id=uuid.uuid4(),
        )
        active = await service.create(seed, project, owner, body, None)
        claim, assignment = await claimed(seed, active)
        expired = await service.create(
            seed,
            project,
            owner,
            body.model_copy(update={"request_id": uuid.uuid4()}),
            None,
        )
        await service.cancel(seed, expired)
        expired.expires_at = service.now() - timedelta(seconds=1)
        await seed.commit()
    entered, release = threading.Event(), threading.Event()

    def blocked_storage(*args, **kwargs):
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(service.storage, "delete_prefix", blocked_storage)

    async def collecting():
        async with maker() as db:
            result = await service.cleanup(db)
            await db.commit()
            return result

    async def beating():
        async with maker() as db:
            result = await service.heartbeat(
                db, active.id, ExecutorFence(**fence(claim, assignment))
            )
            await db.commit()
            return result

    task = asyncio.create_task(collecting())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert not (await asyncio.wait_for(beating(), 2))["stop"]
        assert not task.done()
        release.set()
        assert (await asyncio.wait_for(task, 5))["removed"] == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        async with maker() as db:
            await db.execute(
                delete(TrainingPreviewResourceLease).where(
                    TrainingPreviewResourceLease.resource_id == service.resource_id()
                )
            )
            await db.execute(
                delete(TrainingPreview).where(TrainingPreview.project_id == project.id)
            )
            await db.execute(
                delete(AsyncJob).where(
                    AsyncJob.id.in_([active.async_job_id, expired.async_job_id])
                )
            )
            await db.execute(
                delete(TrainingDatasetVersion).where(
                    TrainingDatasetVersion.id == dataset.id
                )
            )
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(User).where(User.id == owner.id))
            await db.commit()
