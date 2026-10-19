"""Current lifecycle/media and complete retained membership fence acceptance."""

import copy
import hashlib
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.task_lock import AnnotationDraft, TaskLock
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.active_learning import DecisionCreate, RoundCreate
from app.services import active_learning as service
from app.services import active_learning_pool as pool
from app.services import active_learning_references as references
from app.services.assets_active_learning_fence_contract import CandidateItemFenceBusy
from app.services.labeling_active_learning_source_contract import (
    CandidateEditFence,
    CandidatePageRead,
    CandidateSourceError,
    CandidateSourcesRead,
)
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from app.services.training_datasets import bind_training_authority
from tests.sample_evaluation_fixtures import evaluation as create_evaluation
from tests.active_learning_fixtures import (
    candidate_ports,
    complete,
    create,
    decide_with_ports,
    prepare_with_ports,
    seed,
)
from tests.test_training_dataset_analysis import inputs
from tests.factory import create_batch


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
        assert task.total_annotations == 0
        db_session.add(
            Annotation(
                task_id=task.id,
                project_id=task.project_id,
                user_id=owner.id,
                class_name="car",
                tool_unit_id="bbox",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            )
        )
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
    if change == "annotation_history":
        await db_session.refresh(task, attribute_names=["total_annotations"])
        assert task.total_annotations == 0
    with pytest.raises(HTTPException) as error:
        await prepare_with_ports(db_session, world.project, world.selection, owner.id)
    assert error.value.detail["code"] == "pool_stale"


async def test_labeling_candidate_reads_keep_page_and_batch_query_counts(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    candidate_sources, _assets_fence = candidate_ports(db_session)
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        page = await candidate_sources.list_candidates(
            CandidatePageRead(world.project.id, limit=2, offset=0)
        )
        assert len(statements) == 2
        expected_page = sorted(
            world.tasks, key=lambda task: (task.created_at, task.id)
        )[:2]
        assert [row["task_id"] for row in page["items"]] == [
            str(task.id) for task in expected_page
        ]
        assert page["total"] == len(world.tasks)

        statements.clear()
        requested = tuple(task.id for task in reversed(world.tasks))
        snapshots = await candidate_sources.read_candidate_sources(
            CandidateSourcesRead(world.project.id, requested)
        )
        assert len(statements) == 1
        assert [row["task_id"] for row in snapshots] == sorted(
            str(identity) for identity in requested
        )

        statements.clear()
        assert (
            await candidate_sources.read_candidate_sources(
                CandidateSourcesRead(world.project.id, ())
            )
            == []
        )
        assert len(statements) == 1

        statements.clear()
        with pytest.raises(CandidateSourceError) as error:
            await candidate_sources.read_candidate_sources(
                CandidateSourcesRead(world.project.id, (uuid.uuid4(),))
            )
        assert error.value.code == "pool_stale"
        assert len(statements) == 1

        statements.clear()
        with pytest.raises(CandidateSourceError) as error:
            await candidate_sources.read_candidate_sources(
                CandidateSourcesRead(uuid.uuid4(), (world.tasks[0].id,))
            )
        assert error.value.code == "pool_stale"
        assert len(statements) == 1
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)


async def test_candidate_source_snapshot_detaches_mutable_json_arrays(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    task = world.tasks[0]
    task.tags = ["current"]
    batch = await create_batch(db_session, project_id=world.project.id)
    batch.display_id = "B-DEFAULT"
    task.batch_id = batch.id
    await db_session.flush()
    candidate_sources, _assets_fence = candidate_ports(db_session)

    [snapshot] = await candidate_sources.read_candidate_sources(
        CandidateSourcesRead(world.project.id, (task.id,))
    )
    assert snapshot["task"]["tags"] == ["current"]
    assert snapshot["batch"]["assigned_user_ids"] == []

    task.tags.append("later")
    batch.assigned_user_ids.append(str(owner.id))
    assert snapshot["task"]["tags"] == ["current"]
    assert snapshot["batch"]["assigned_user_ids"] == []


async def test_candidate_edit_fence_maps_advisory_contention_to_pool_busy(
    db_session, test_engine, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    candidate_sources, _assets_fence = candidate_ports(db_session)
    holder = async_sessionmaker(test_engine, expire_on_commit=False)()
    try:
        task_id = world.tasks[0].id
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"aap:task-edit-lock:{task_id}"},
        )
        with pytest.raises(CandidateSourceError) as error:
            await candidate_sources.lock_candidate_edits(
                CandidateEditFence((task_id,), owner.id)
            )
        assert error.value.code == "pool_busy"
    finally:
        await holder.rollback()
        await holder.close()


async def test_candidate_edit_fence_rejects_own_active_task_lock(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    task = world.tasks[0]
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=owner.id,
            expire_at=service.now() + timedelta(minutes=3),
        )
    )
    await db_session.flush()
    candidate_sources, _assets_fence = candidate_ports(db_session)

    with pytest.raises(CandidateSourceError) as error:
        await candidate_sources.lock_candidate_edits(
            CandidateEditFence((task.id,), owner.id)
        )
    assert error.value.code == "pool_locked"


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
        world.objects.put_object(
            Bucket=service.storage.bucket,
            Key=other.file_path,
            Body=world.objects.objects[world.tasks[-1].file_path]["raw"],
        )
    with pytest.raises(HTTPException) as error:
        await decide_with_ports(
            db_session,
            world.project,
            owner,
            round.id,
            DecisionCreate(
                request_id=uuid.uuid4(), action="accept", ordinals=[grouped["ordinal"]]
            ),
        )
    assert error.value.detail["code"] in {"pool_stale", "source_stale"}
    assert round.accepted_tasks == 0


async def test_conditional_reads_compute_actual_sha_and_detect_inflight_object_change(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    preview = await prepare_with_ports(
        db_session, world.project, world.selection, owner.id
    )
    for sample in preview["pool"]:
        raw = world.objects.objects[sample["snapshot"]["source_key"]]["raw"]
        assert sample["sha256"] == hashlib.sha256(raw).hexdigest()
    original = world.objects.get_object

    def replaced(**kwargs):
        key = kwargs["Key"]
        world.objects.put_object(
            Bucket=service.storage.bucket, Key=key, Body=b"changed after HEAD"
        )
        return original(**kwargs)

    monkeypatch.setattr(world.objects, "get_object", replaced)
    with pytest.raises(HTTPException) as error:
        await prepare_with_ports(db_session, world.project, world.selection, owner.id)
    assert error.value.detail["code"] == "source_unavailable"


async def test_prepare_rechecks_api_key_after_candidate_media_io(
    db_session, super_admin, monkeypatch
):
    from tests.factory import create_api_key

    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    key, _token = await create_api_key(
        db_session, owner.id, "active-learning-revocation", ["*"]
    )
    original = pool.asyncio.to_thread

    async def revoke_after_source_read(function, *args, **kwargs):
        result = await original(function, *args, **kwargs)
        if function is pool.read_input:
            key.revoked_at = service.now()
            await db_session.flush()
        return result

    monkeypatch.setattr(pool.asyncio, "to_thread", revoke_after_source_read)
    with pytest.raises(TrainingDatasetError) as error:
        await prepare_with_ports(
            db_session,
            world.project,
            world.selection,
            owner.id,
            str(key.id),
        )
    assert error.value.code == "api_key_revoked"


async def test_source_item_contention_maps_to_source_busy_during_create(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    candidate_sources, _assets_fence = candidate_ports(db_session)
    prepared = await pool.prepare(
        db_session,
        world.project,
        world.selection,
        owner.id,
        candidate_sources=candidate_sources,
        training_authority=bind_training_authority(db_session),
    )
    body = RoundCreate(
        **world.selection.model_dump(),
        request_id=uuid.uuid4(),
        name="Human selection",
        preview_digest=prepared["preview_digest"],
    )

    class BusyFence:
        async def lock_candidate_items(self, _request):
            raise CandidateItemFenceBusy

    with pytest.raises(HTTPException) as error:
        await service.create_round(
            db_session,
            world.project,
            owner,
            body,
            prepared,
            candidate_sources=candidate_sources,
            assets_fence=BusyFence(),
            training_authority=bind_training_authority(db_session),
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "source_busy"


@pytest.mark.parametrize("manifest", [None, {}])
async def test_retained_version_without_complete_manifest_fails_closed(
    db_session, super_admin, monkeypatch, manifest
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    world.dataset.manifest = manifest
    await db_session.flush()
    with pytest.raises(HTTPException) as error:
        await prepare_with_ports(db_session, world.project, world.selection, owner.id)
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
            await references.guard_source(db_session, task_ids=[task.id])
        assert error.value.detail["code"] == "active_learning_reference"
    with pytest.raises(HTTPException):
        await references.guard_source(db_session, version_id=world.dataset.id)
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
        await decide_with_ports(
            db_session,
            world.project,
            owner,
            round.id,
            DecisionCreate(
                request_id=uuid.uuid4(), action="accept", ordinals=[chosen["ordinal"]]
            ),
        )
    assert error.value.detail["code"] == "source_stale"


async def test_held_out_evaluation_reference_blocks_evaluation_deletion(
    db_session, super_admin, httpx_client, monkeypatch
):
    import io
    import json
    import zipfile

    from PIL import Image

    from app.services.training_dataset_bridge import detection_labels

    owner, token = super_admin
    world = await seed(db_session, owner, monkeypatch)
    image = io.BytesIO()
    Image.new("RGB", (16, 12), (241, 239, 237)).save(image, format="PNG")
    raw = image.getvalue()
    snapshot, manifest = inputs(
        [
            {
                "content": "independent-held-out",
                "split": "val",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
        ],
        classes=["car", "person"],
    )
    package = io.BytesIO()
    member = manifest["samples"][0]
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(member["image_path"], raw)
        archive.writestr(
            member["label_path"],
            detection_labels(
                snapshot["samples"][0]["annotations"], manifest["classes"]
            ),
        )
        archive.writestr("manifest.json", json.dumps(manifest))
    package_raw = package.getvalue()
    held_out = TrainingDatasetVersion(
        project_id=world.project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest="f" * 64,
        name="Independent held-out",
        snapshot=snapshot,
        snapshot_digest=content_digest(snapshot),
        manifest=manifest,
        status="ready",
        package_key=f"held-out/{uuid.uuid4()}.zip",
        package_sha256=hashlib.sha256(package_raw).hexdigest(),
    )
    db_session.add(held_out)
    await db_session.flush()
    world.objects.put_object(
        Bucket=service.storage.training_bucket,
        Key=held_out.package_key,
        Body=package_raw,
        ContentType="application/zip",
    )
    sample_id = manifest["samples"][0]["task_id"]
    world.objects.put_object(
        Bucket=service.storage.training_bucket,
        Key=f"projects/{world.project.id}/datasets/{held_out.id}/media/{sample_id}",
        Body=raw,
        Metadata={
            "snapshot-digest": held_out.snapshot_digest,
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        ContentType="image/png",
    )
    world.body = world.body.model_copy(
        update={
            "dataset_id": held_out.id,
            "sample_task_ids": [uuid.UUID(sample_id)],
            "role": "held_out_test",
            "held_out_confirmed": True,
        }
    )
    run, _attempt = await create_evaluation(db_session, owner, world)
    await create(db_session, owner, world)

    response = await httpx_client.delete(
        f"/api/v1/projects/{world.project.id}/sample-evaluations/{run.id}",
        headers={"Authorization": "Bearer " + token},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "active_learning_reference"


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
