"""Real source/authority rows guard an exact bucket/key provider."""

from datetime import timedelta
import hashlib
import threading
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.task import Task
from app.services import comment_attachment_authority as credentials
from app.services import sample_evaluation_originals as originals
from app.services import sample_evaluation_sources as sources
from app.services import sample_evaluations as service, task_originals
from app.services.api_key_service import create_key
from tests.factory import create_project
from tests.sample_evaluation_fixtures import evaluation, seed
from tests.sample_evaluation_original_fixtures import Objects, registered
from tests.test_original_binary import request
from tests.test_original_binary import world as world
from tests.test_task_originals import original_client, original_db  # noqa: F401
from tests.test_sample_evaluations import objects as objects


@pytest.fixture
async def current(db_session, super_admin, objects, monkeypatch):
    owner, token = super_admin
    world = await seed(db_session, owner, objects)
    run, attempt = await evaluation(db_session, owner, world)
    task = Task(
        id=uuid.UUID(run.spec["samples"][0]["task_id"]),
        project_id=world.project.id,
        display_id="T-CURRENT-" + uuid.uuid4().hex[:8],
        file_path="fixed/current.png",
        file_name="current.png",
        file_type="image",
        status="completed",
        reviewed_at=service.now(),
    )
    db_session.add(task)
    await db_session.flush()
    annotation = Annotation(
        task_id=task.id,
        project_id=world.project.id,
        user_id=owner.id,
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        source="manual",
    )
    db_session.add(annotation)
    item, asset, replica = await registered(
        db_session, task, owner, world.raw_images[0]
    )
    provider = Objects().install(monkeypatch)
    provider.put(replica.bucket, replica.object_key, world.raw_images[0])
    monkeypatch.setattr(originals, "_slots", threading.BoundedSemaphore(2))
    value = SimpleNamespace(
        db=db_session,
        owner=owner,
        token=token,
        world=world,
        run=run,
        attempt=attempt,
        task=task,
        annotation=annotation,
        item=item,
        asset=asset,
        replica=replica,
        provider=provider,
        endpoint=f"/api/v1/projects/{run.project_id}/sample-evaluations/{run.id}/attempts/{attempt.id}/samples/0/source",
    )
    yield value
    assert all(body.closed for body in provider.bodies)
    assert originals._slots._value == 2


async def response(client, current, *, token=None):
    return await client.get(
        current.endpoint,
        headers={"Authorization": "Bearer " + (token or current.token)},
    )


async def test_scope_contract_requires_no_tasks_read(current, httpx_client):
    _key, token = await create_key(
        current.db, current.owner, "source-only", ["datasets:read", "annotations:read"]
    )
    result = await response(httpx_client, current, token=token)
    assert result.status_code == 200, result.text
    assert result.json()["media"] == "same"


@pytest.mark.parametrize(
    "change", ["unknown", "non-image", "oversized", "missing", "tampered"]
)
async def test_unverified_or_invalid_inputs_never_publish_current_digest(
    current, httpx_client, change
):
    if change == "unknown":
        current.task.dataset_item_id = None
    elif change == "non-image":
        current.task.file_type = "video"
    elif change == "oversized":
        await registered(
            current.db,
            current.task,
            current.owner,
            b"x" * (service.engine.MAX_RAW_BYTES + 1),
            key="oversized/current.png",
        )
    elif change == "missing":
        current.provider.objects.clear()
    else:
        current.provider.put(
            current.replica.bucket,
            current.replica.object_key,
            current.world.raw_images[1],
        )
    await current.db.flush()
    result = await response(httpx_client, current)
    assert result.status_code == 200, result.text
    assert result.json()["media"] == "unavailable"
    assert result.json()["current_sha256"] is None
    if change in {"unknown", "non-image", "oversized"}:
        assert not current.provider.calls


async def test_same_key_in_current_bucket_never_substitutes_the_saved_object(
    current, httpx_client, monkeypatch
):
    monkeypatch.setattr(service.storage, "datasets_bucket", "current-trap")
    monkeypatch.setattr(service.storage, "bucket", "other-trap")
    current.provider.put(
        "current-trap", current.replica.object_key, current.world.raw_images[1]
    )
    result = await response(httpx_client, current)
    assert result.status_code == 200, result.text
    assert result.json()["media"] == "same"
    assert {bucket for op, bucket, _ in current.provider.calls if op != "close"} == {
        current.replica.bucket
    }


@pytest.mark.parametrize("phase", ["decode", "labels", "close"])
@pytest.mark.parametrize(
    "change", ["key", "jwt", "actor", "gate", "content", "annotation"]
)
async def test_after_io_and_label_work_rechecks_the_original_facts(
    current, httpx_client, monkeypatch, phase, change
):
    key, token = await create_key(current.db, current.owner, "mutable-source", ["*"])
    original = originals.bounded_thread
    close = originals.close_stage
    mutated = False

    async def mutate():
        nonlocal mutated
        if not mutated:
            mutated = True
            if change == "key":
                key.scopes = ["datasets:read"]
            elif change == "jwt":
                later = credentials.now() + timedelta(days=1)
                monkeypatch.setattr(credentials, "now", lambda: later)
            elif change == "actor":
                current.owner.is_active = False
            elif change == "gate":
                gate = await current.db.scalar(
                    select(StorageMaintenanceGate).where(
                        StorageMaintenanceGate.location_id
                        == current.replica.location_id
                    )
                )
                gate.state, gate.revision = "draining", gate.revision + 1
            elif change == "content":
                await registered(
                    current.db,
                    current.task,
                    current.owner,
                    current.world.raw_images[1],
                    key="replacement/current.png",
                )
            else:
                current.annotation.geometry = {**current.annotation.geometry, "w": 0.2}
            await current.db.flush()

    async def observed(call, *args, **kwargs):
        value = await original(call, *args, **kwargs)
        if phase != "close" and call is (
            originals.inspect_image if phase == "decode" else sources.classify_labels
        ):
            await mutate()
        return value

    async def closed(stage):
        await close(stage)
        if phase == "close":
            await mutate()

    monkeypatch.setattr(originals, "bounded_thread", observed)
    monkeypatch.setattr(originals, "close_stage", closed)
    # JWT uses the actual JWT, Key changes use the captured scoped Key proof.
    result = await response(
        httpx_client, current, token=current.token if change == "jwt" else token
    )
    assert mutated
    assert result.status_code == (
        403
        if change in {"key", "jwt", "actor"}
        else 409
        if change in {"gate", "content"}
        else 200
    ), result.text
    if result.status_code == 200:
        assert result.json()["source"] == "unavailable"
        assert result.json()["current_sha256"] is None


async def test_task_move_between_snapshot_and_capture_prevents_all_object_io(
    current, httpx_client, monkeypatch
):
    other = await create_project(current.db, owner_id=current.owner.id)
    freeze = originals.freeze_source

    async def moved(db, authority, snapshot):
        current.task.project_id = other.id
        await db.flush()
        return await freeze(db, authority, snapshot)

    monkeypatch.setattr(originals, "freeze_source", moved)
    result = await response(httpx_client, current)
    assert result.status_code == 200, result.text
    assert result.json()["source"] == "deleted"
    assert not current.provider.calls


async def test_standalone_task_uses_published_original_without_a_dataset_or_current_bucket(
    world, monkeypatch
):
    from io import BytesIO
    from PIL import Image
    from app.services import managed_original_creation as creation

    output = BytesIO()
    Image.new("RGB", (16, 12), "blue").save(output, "PNG")
    raw = output.getvalue()
    task = Task(
        id=uuid.uuid4(),
        project_id=world.project.id,
        display_id="T-DIRECT-" + uuid.uuid4().hex[:8],
        file_path="pending",
        file_name="direct.png",
        file_type="image",
        status="uploading",
    )
    world.db.add(task)
    await world.db.flush()
    allocation = await creation.allocate_task(
        world.db,
        project_id=world.project.id,
        task_id=task.id,
        logical_path=task.file_name,
        max_bytes=len(raw),
        requested_by=world.manager.id,
        authority={},
    )
    task.file_path = allocation["object_key"]
    await world.db.commit()
    world.client.store(allocation["staging_bucket"], allocation["staging_key"], raw)
    created = await creation.complete_direct(world.db, allocation)
    await task_originals.publish(world.db, task, allocation, created)
    task.status = "pending"
    await world.db.commit()
    provider = Objects().install(monkeypatch)
    provider.put(allocation["bucket"], allocation["object_key"], raw)
    monkeypatch.setattr(service.storage, "bucket", "current-trap")
    frozen = {
        "task_id": str(task.id),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "label_sha256": hashlib.sha256(b"").hexdigest(),
    }
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=world.project.id,
        spec={"schema": 1, "classes": ["car"], "samples": [frozen]},
    )
    result = await sources.resolve_source(
        world.db, run, uuid.uuid4(), 0, world.manager, request=request(world.manager)
    )
    assert task.dataset_item_id is None and task.original_id is not None
    assert result["media"] == result["labels"] == "same"
    assert {bucket for op, bucket, _ in provider.calls if op != "close"} == {
        allocation["bucket"]
    }
    assert all(body.closed for body in provider.bodies)
