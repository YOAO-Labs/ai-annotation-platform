"""Three AL raw-input paths retain frozen ownership and current authority."""

from copy import deepcopy
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.task import Task
from app.schemas.active_learning import DecisionCreate, RoundCreate
from app.services import active_learning as service
from app.services import active_learning_originals as originals
from app.services.content_snapshot import ContentError
from app.services.training_dataset_bridge import content_digest
from app.services.training_datasets import bind_training_authority
from tests.active_learning_fixtures import (
    candidate_ports,
    complete,
    decide_with_ports,
    prepare_with_ports,
    seed,
)
from tests.factory import create_api_key
from tests.sample_evaluation_original_fixtures import registered
from tests.test_original_binary import request


@pytest.fixture
async def current(db_session, super_admin, monkeypatch):
    owner, token = super_admin
    world = await seed(db_session, owner, monkeypatch, count=4)
    key, key_token = await create_api_key(
        db_session, owner.id, "AL original", sorted(originals.SCOPES)
    )
    candidate_sources, assets_fence = candidate_ports(db_session)
    owner_ports = {
        "candidate_sources": candidate_sources,
        "assets_fence": assets_fence,
        "training_authority": bind_training_authority(db_session),
    }

    def proof():
        return request(owner, token=key_token, key_id=key.id, scopes=key.scopes)

    async def prepare():
        return await prepare_with_ports(
            db_session,
            world.project,
            world.selection,
            owner.id,
            str(key.id),
            request=proof(),
        )

    async def create():
        prepared = await prepare()
        body = RoundCreate(
            **world.selection.model_dump(),
            request_id=uuid.uuid4(),
            name="Original inputs",
            preview_digest=prepared["preview_digest"],
        )
        round = await service.create_round(
            db_session, world.project, owner, body, prepared, str(key.id), **owner_ports
        )
        await db_session.flush()
        from app.db.models.active_learning import ActiveLearningAttempt

        return round, await db_session.get(
            ActiveLearningAttempt, round.current_attempt_id
        )

    yield SimpleNamespace(
        db=db_session,
        owner=owner,
        token=token,
        world=world,
        key=key,
        key_token=key_token,
        proof=proof,
        prepare=prepare,
        create=create,
        owner_ports=owner_ports,
    )
    assert all(body.closed for body in world.originals.bodies)


async def test_preview_preserves_scopes_saved_bucket_and_public_projection(
    current, httpx_client, monkeypatch
):
    world = current.world
    monkeypatch.setattr(service.storage, "bucket", "wrong-current-annotations")
    monkeypatch.setattr(service.storage, "datasets_bucket", "wrong-current-datasets")
    for replica in world.replicas.values():
        world.originals.put(
            "wrong-current-datasets", replica.object_key, b"wrong same-key bytes"
        )
    result = await httpx_client.post(
        f"/api/v1/projects/{world.project.id}/active-learning-rounds:preview",
        json=world.selection.model_dump(mode="json"),
        headers={"Authorization": "Bearer " + current.key_token},
    )
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["candidate_count"] == 4 and "_authority" not in data
    assert all("snapshot" not in sample for sample in data["samples"])
    assert {bucket for op, bucket, _ in world.originals.calls if op != "close"} == {
        r.bucket for r in world.replicas.values()
    }


@pytest.mark.parametrize("path", ["preview", "worker", "accept"])
@pytest.mark.parametrize("boundary", ["decode", "close"])
@pytest.mark.parametrize("change", ["key", "actor", "gate", "owner"])
async def test_each_raw_path_rechecks_late_authority_and_original_facts(
    current, monkeypatch, path, boundary, change
):
    world = current.world
    if path != "preview":
        round, attempt = await current.create()
        sample = round.spec["samples"][0]
        if path == "accept":
            report = await complete(current.db, round, attempt)
            chosen = next(row for row in report["ranking"] if row["recommended"])
            body = DecisionCreate(
                request_id=uuid.uuid4(), action="accept", ordinals=[chosen["ordinal"]]
            )
    bounded, close = originals.bounded_thread, originals.close_stage
    changed, active_snapshot = False, None

    async def mutate(snapshot):
        nonlocal changed
        if changed:
            return
        changed = True
        if change == "key":
            current.key.scopes = ["datasets:read"]
        elif change == "actor":
            current.owner.is_active = False
        elif change == "gate":
            gate = await current.db.scalar(
                select(StorageMaintenanceGate).where(
                    StorageMaintenanceGate.location_id
                    == uuid.UUID(snapshot["original"]["location_id"])
                )
            )
            gate.state, gate.revision = "draining", gate.revision + 1
        else:
            task = await current.db.get(Task, uuid.UUID(snapshot["task_id"]))
            replica = world.replicas[task.id]
            raw = world.originals.objects[replica.bucket, replica.object_key]
            await registered(
                current.db, task, current.owner, raw, bucket=replica.bucket
            )
        await current.db.flush()

    async def observed(call, *args, **kwargs):
        nonlocal active_snapshot
        value = await bounded(call, *args, **kwargs)
        if call is originals.normalize:
            active_snapshot = args[1]
            if boundary == "decode":
                await mutate(active_snapshot)
        return value

    async def closed(stage):
        await close(stage)
        if boundary == "close":
            await mutate(active_snapshot)

    monkeypatch.setattr(originals, "bounded_thread", observed)
    monkeypatch.setattr(originals, "close_stage", closed)
    with pytest.raises((HTTPException, ContentError)) as caught:
        if path == "preview":
            await current.prepare()
        elif path == "worker":
            await service.prepare_image(current.db, round, sample)
        else:
            await decide_with_ports(
                current.db,
                world.project,
                current.owner,
                round.id,
                body,
                str(current.key.id),
                request=current.proof(),
            )
    assert changed
    error = caught.value
    assert (
        error.status_code if isinstance(error, HTTPException) else error.status
    ) == (403 if change in {"key", "actor"} else 409)


async def test_unknown_original_has_no_raw_io(current):
    current.world.tasks[0].dataset_item_id = None
    await current.db.flush()
    current.world.originals.calls.clear()
    with pytest.raises(ContentError):
        await current.prepare()
    assert not current.world.originals.calls


async def test_creator_key_rotation_invalidates_new_background_round(current):
    from app.core.security import hash_password

    round, _ = await current.create()
    current.key.key_hash = hash_password("a different original Key")
    await current.db.flush()
    current.world.originals.calls.clear()
    with pytest.raises(HTTPException) as caught:
        await service.prepare_image(current.db, round, round.spec["samples"][0])
    assert caught.value.status_code == 403 and not current.world.originals.calls


async def test_legacy_specs_keep_retained_evidence_but_never_repair_or_accept(current):
    round, attempt = await current.create()
    for sample in round.spec["samples"]:
        await service.prepare_image(current.db, round, sample)
    spec = deepcopy(round.spec)
    spec.pop("original_contract")
    for sample in spec["pool"]:
        sample["snapshot"].pop("original")
    spec["pool_digest"] = content_digest(spec["pool"])
    samples, omitted = service.engine.grouped_pool(
        spec["pool"], spec["exclusions"]["identities"]
    )
    spec["samples"] = service.engine.partition(
        samples, spec["seed"], spec["batch_size"]
    )
    spec["omitted"] = omitted
    spec["preview_digest"] = content_digest(
        {
            k: v
            for k, v in spec.items()
            if k not in {"round_id", "issued_at", "expires_at", "preview_digest"}
        }
    )
    round.spec, round.spec_digest = spec, content_digest(spec)
    round.authorization = {"api_key_id": str(current.key.id)}
    await current.db.flush()
    service.validate_spec(round)
    report = await complete(current.db, round, attempt)
    sample = spec["samples"][0]
    current.world.originals.calls.clear()
    assert await service.prepare_image(current.db, round, sample)
    assert await service.read_report(current.db, round, attempt) == report
    body = DecisionCreate(
        request_id=uuid.uuid4(),
        action="accept",
        ordinals=[
            next(row["ordinal"] for row in report["ranking"] if row["recommended"])
        ],
    )
    with pytest.raises(ContentError, match="original_input_unrecorded"):
        await decide_with_ports(
            current.db,
            current.world.project,
            current.owner,
            round.id,
            body,
            str(current.key.id),
            request=current.proof(),
        )
    artifact = service.media_descriptor(round, sample)
    current.world.objects.objects.pop(artifact["key"])
    with pytest.raises(ContentError, match="original_input_unrecorded"):
        await service.prepare_image(current.db, round, sample)
    assert not current.world.originals.calls


@pytest.mark.parametrize("path", ["worker", "accept"])
async def test_frozen_owner_replacement_with_identical_bytes_is_rejected_before_raw_io(
    current, path
):
    round, attempt = await current.create()
    sample = round.spec["samples"][0]
    if path == "accept":
        report = await complete(current.db, round, attempt)
        chosen = next(row for row in report["ranking"] if row["recommended"])
        sample = round.spec["samples"][chosen["ordinal"]]
    task = await current.db.get(Task, uuid.UUID(sample["task_id"]))
    replica = current.world.replicas[task.id]
    raw = current.world.originals.objects[replica.bucket, replica.object_key]
    await registered(current.db, task, current.owner, raw, bucket=replica.bucket)
    current.world.originals.calls.clear()
    with pytest.raises((ContentError, HTTPException)):
        if path == "worker":
            await service.prepare_image(current.db, round, sample)
        else:
            await decide_with_ports(
                current.db,
                current.world.project,
                current.owner,
                round.id,
                DecisionCreate(
                    request_id=uuid.uuid4(),
                    action="accept",
                    ordinals=[sample["ordinal"]],
                ),
                str(current.key.id),
                request=current.proof(),
            )
    assert not current.world.originals.calls


async def test_gate_change_between_preview_and_create_cannot_publish_a_round(current):
    prepared = await current.prepare()
    snapshot = prepared["pool"][0]["snapshot"]
    gate = await current.db.scalar(
        select(StorageMaintenanceGate).where(
            StorageMaintenanceGate.location_id
            == uuid.UUID(snapshot["original"]["location_id"])
        )
    )
    gate.revision += (
        2  # A completed maintenance cycle still retires this in-flight read.
    )
    await current.db.flush()
    body = RoundCreate(
        **current.world.selection.model_dump(),
        request_id=uuid.uuid4(),
        name="Stale gate",
        preview_digest=prepared["preview_digest"],
    )
    with pytest.raises(ContentError, match="content_binding_changed"):
        await service.create_round(
            current.db,
            current.world.project,
            current.owner,
            body,
            prepared,
            str(current.key.id),
            **current.owner_ports,
        )


async def test_api_jwt_expiry_after_physical_close_is_not_a_successful_preview(
    current, monkeypatch
):
    from datetime import timedelta
    from app.services import comment_attachment_authority as credentials

    close = originals.close_stage

    async def expired(stage):
        await close(stage)
        future = credentials.now() + timedelta(days=1)
        monkeypatch.setattr(credentials, "now", lambda: future)

    monkeypatch.setattr(originals, "close_stage", expired)
    with pytest.raises(HTTPException) as caught:
        await prepare_with_ports(
            current.db,
            current.world.project,
            current.world.selection,
            current.owner.id,
            request=request(current.owner),
        )
    assert caught.value.status_code == 403
