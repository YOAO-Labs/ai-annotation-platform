"""Current lifecycle/media and complete retained membership fence acceptance."""

import copy
import hashlib
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.active_learning import ActiveLearningDecision
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import AnnotationDraft, TaskLock
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.active_learning import DecisionCreate
from app.services import active_learning as service, active_learning_pool as pool
from app.services.content_snapshot import ContentError
from app.services.training_dataset_bridge import content_digest
from tests.active_learning_fixtures import complete, create, seed
from tests.test_original_binary import request
from tests.test_training_dataset_analysis import inputs


@pytest.mark.parametrize(
    "change", ["assigned", "reviewed", "draft", "lock", "annotation_history"]
)
async def test_pool_rejects_current_or_historical_human_work(
    db_session, super_admin, monkeypatch, change
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    task = world.tasks[0]
    if change == "assigned":
        task.assignee_id = owner.id
    elif change == "reviewed":
        task.first_reviewed_at = service.now()
    elif change == "annotation_history":
        task.total_annotations = 1
    elif change == "draft":
        db_session.add(AnnotationDraft(task_id=task.id, user_id=owner.id, result={}))
    else:
        db_session.add(
            TaskLock(
                task_id=task.id,
                user_id=owner.id,
                expire_at=service.now() + timedelta(minutes=3),
            )
        )
    await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await pool.prepare(
            db_session, world.project, world.selection, owner.id, request=request(owner)
        )
    assert error.value.detail["code"] == "pool_stale"


@pytest.mark.parametrize("change", ["assignment", "media"])
async def test_accept_revalidates_every_duplicate_member_not_only_representative(
    db_session, super_admin, monkeypatch, change
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch, count=5, duplicate=True)
    round, attempt, _ = await create(db_session, owner, world)
    report = await complete(db_session, round, attempt)
    grouped = next(
        sample for sample in round.spec["samples"] if len(sample["group_task_ids"]) == 2
    )
    assert next(
        row for row in report["ranking"] if row["ordinal"] == grouped["ordinal"]
    )["recommended"]
    other = next(
        task
        for task in world.tasks
        if str(task.id) in grouped["group_task_ids"]
        and str(task.id) != grouped["task_id"]
    )
    if change == "assignment":
        other.assignee_id = owner.id
        await db_session.flush()
    else:
        replica = world.replicas[other.id]
        replacement = world.replicas[world.tasks[-1].id]
        world.originals.put(
            replica.bucket,
            replica.object_key,
            world.originals.objects[replacement.bucket, replacement.object_key],
        )
    batches_before = await db_session.scalar(
        select(func.count()).select_from(TaskBatch).where(
            TaskBatch.project_id == world.project.id
        )
    )
    expected_error = HTTPException if change == "assignment" else ContentError
    with pytest.raises(expected_error):
        await service.decide(
            db_session,
            world.project,
            owner,
            round.id,
            DecisionCreate(
                request_id=uuid.uuid4(), action="accept", ordinals=[grouped["ordinal"]]
            ),
            request=request(owner),
        )
    assert round.accepted_tasks == 0
    assert not await db_session.scalar(
        select(ActiveLearningDecision).where(
            ActiveLearningDecision.round_id == round.id
        )
    )
    assert all(task.batch_id is None for task in world.tasks)
    assert await db_session.scalar(
        select(func.count()).select_from(TaskBatch).where(
            TaskBatch.project_id == world.project.id
        )
    ) == batches_before
    assert all(body.closed for body in world.originals.bodies)


async def test_conditional_reads_compute_actual_sha_and_detect_inflight_object_change(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    preview = await pool.prepare(
        db_session, world.project, world.selection, owner.id, request=request(owner)
    )
    for sample in preview["pool"]:
        replica = world.replicas[uuid.UUID(sample["task_id"])]
        raw = world.originals.objects[replica.bucket, replica.object_key]
        assert sample["sha256"] == hashlib.sha256(raw).hexdigest()
    assert world.originals.bodies and all(
        body.closed for body in world.originals.bodies
    )
    body_count = len(world.originals.bodies)
    original = world.originals.get_object

    def replaced(**kwargs):
        world.originals.put(kwargs["Bucket"], kwargs["Key"], b"changed after HEAD")
        return original(**kwargs)

    monkeypatch.setattr(world.originals, "get_object", replaced)
    with pytest.raises(ContentError):
        await pool.prepare(
            db_session, world.project, world.selection, owner.id, request=request(owner)
        )
    # The conditional GET must reject the replaced object before opening a
    # response body, not decode or publish a preview of unverified bytes.
    assert len(world.originals.bodies) == body_count
    assert all(body.closed for body in world.originals.bodies)


@pytest.mark.parametrize("manifest", [None, {}])
async def test_retained_version_without_complete_manifest_fails_closed(
    db_session, super_admin, monkeypatch, manifest
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    world.dataset.manifest = manifest
    await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await pool.prepare(
            db_session, world.project, world.selection, owner.id, request=request(owner)
        )
    assert error.value.detail["code"] == "history_unready"


async def test_later_content_alias_membership_blocks_accept_and_retained_window_blocks_deletion(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch, count=5, duplicate=True)
    round, attempt, _ = await create(db_session, owner, world)
    report = await complete(db_session, round, attempt)
    for task in world.tasks:
        with pytest.raises(HTTPException) as error:
            await pool.guard_source(db_session, task_ids=[task.id])
        assert error.value.detail["code"] == "active_learning_reference"
    with pytest.raises(HTTPException):
        await pool.guard_source(db_session, version_id=world.dataset.id)
    chosen = next(row for row in report["ranking"] if row["recommended"])
    frozen = round.spec["samples"][chosen["ordinal"]]
    snapshot, manifest = inputs(
        [
            {
                "content": "later independent alias",
                "sha256": frozen["sha256"],
                "split": "val",
            }
        ],
        classes=["car", "person"],
    )
    db_session.add(
        TrainingDatasetVersion(
            project_id=world.project.id,
            creator_id=owner.id,
            request_id=uuid.uuid4(),
            request_digest="e" * 64,
            name="Later held-out alias",
            snapshot=snapshot,
            snapshot_digest=content_digest(snapshot),
            manifest=manifest,
            status="ready",
        )
    )
    await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await service.decide(
            db_session,
            world.project,
            owner,
            round.id,
            DecisionCreate(
                request_id=uuid.uuid4(), action="accept", ordinals=[chosen["ordinal"]]
            ),
            request=request(owner),
        )
    assert error.value.detail["code"] == "source_stale"


async def test_frozen_and_retained_evidence_digest_corruption_never_publishes_report(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    round, attempt, _ = await create(db_session, owner, world)
    await complete(db_session, round, attempt)
    row = (await service.candidate_rows(db_session, round))[0]
    evidence = copy.deepcopy(row.evidence)
    evidence["features"]["priority"] = 0
    row.evidence = evidence
    await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await service.read_report(db_session, round, attempt)
    assert error.value.detail["code"] == "evidence_corrupt"
