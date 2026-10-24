"""Primary relations keep the existing reference authority and frozen context."""

import uuid

from fastapi import HTTPException
import pytest

from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import dataset_references as references, original_sources
from app.services.content_snapshot import ContentError
from app.services.dataset_source_access import assert_dataset_manage
from tests.test_dataset_content import content_world as content_world
from tests.test_dataset_references import reference_world as reference_world


async def linked_task(db, original, item_id, annotator, *, direct=False, path=None):
    # Create a separate Task: immutable historical reference FKs are never
    # rewritten to fabricate a primary-only relation.
    task = Task(
        id=uuid.uuid4(),
        project_id=original.project_id,
        display_id="ref-" + uuid.uuid4().hex[:20],
        file_name=original.file_name,
        file_path=path or original.file_path,
        file_type=original.file_type,
        dataset_item_id=item_id if direct else None,
        assignee_id=annotator.id,
        status="in_progress",
    )
    db.add(task)
    await db.flush()
    db.add(
        TaskDatasetItemLink(
            id=uuid.uuid4(),
            task_id=task.id,
            dataset_item_id=item_id,
            role="primary_lidar",
            sensor_name="primary",
        )
    )
    await db.flush()
    return task


@pytest.mark.parametrize("direct", [False, True])
async def test_primary_reference_uses_task_visibility_and_unchanged_context(
    db_session, reference_world, annotator, direct
):
    registered, _, original, world = reference_world
    item_id = original.dataset_item_id
    task = await linked_task(db_session, original, item_id, annotator[0], direct=direct)
    baseline = await references.reference_context(
        db_session, item_id, annotator[0], task_id=original.id
    )
    context = await references.reference_context(
        db_session, item_id, annotator[0], task_id=task.id
    )
    assert context == baseline
    assert "relation" not in context and "task_id" not in context
    source = await original_sources.resolve_task(db_session, task.id)
    assert source.bucket is None and source.item_id == item_id
    assert source.relation.direct_item_id == (item_id if direct else None)
    assert source.relation.primary_item_id == item_id
    snapshot = await references.reference_snapshot(
        db_session,
        item_id,
        annotator[0],
        task_id=task.id,
        expected_sha256=registered["sha256"],
    )
    try:
        snapshot.file.seek(0)
        assert snapshot.file.read() == world[1]
    finally:
        snapshot.close()
    with pytest.raises(HTTPException) as error:
        await assert_dataset_manage(db_session, world[3].id, annotator[0])
    assert error.value.status_code == 403


async def test_primary_only_still_calls_original_task_authorizer(
    db_session, reference_world, annotator
):
    _, _, original, _ = reference_world
    item_id = original.dataset_item_id
    task = await linked_task(db_session, original, item_id, annotator[0])
    called = []

    async def denied(db, current, actor):
        called.append((db, current.id, actor.id, current.dataset_item_id))
        raise HTTPException(403, "current task permission revoked")

    with pytest.raises(HTTPException) as error:
        await references.reference_context(
            db_session, item_id, annotator[0], task_id=task.id, task_authorizer=denied
        )
    assert error.value.status_code == 403
    assert called == [(db_session, task.id, annotator[0].id, None)]


async def test_conflicting_primary_fails_before_any_reference_io(
    db_session, reference_world, annotator, monkeypatch
):
    _, _, original, world = reference_world
    item_id = original.dataset_item_id
    other = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=world[3].id,
        file_name="other.png",
        file_path="legacy/other.png",
        file_type="image",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        TaskDatasetItemLink(
            id=uuid.uuid4(),
            task_id=original.id,
            dataset_item_id=other.id,
            role="primary_lidar",
        )
    )
    await db_session.flush()

    def forbidden(*args, **kwargs):
        pytest.fail("ambiguous relations must fail before physical source access")

    monkeypatch.setattr(references.reference_storage, "content_file", forbidden)
    with pytest.raises(ContentError) as error:
        await references.reference_snapshot(
            db_session, item_id, annotator[0], task_id=original.id
        )
    assert error.value.code == "original_relation_conflict"


async def test_primary_relation_does_not_authorize_another_requested_item(
    db_session, reference_world, annotator
):
    _, _, original, _ = reference_world
    task = await linked_task(
        db_session, original, original.dataset_item_id, annotator[0]
    )
    with pytest.raises(HTTPException) as error:
        await references.reference_context(
            db_session, uuid.uuid4(), annotator[0], task_id=task.id
        )
    assert error.value.status_code == 404


async def test_primary_relation_retains_exact_task_path_check(
    db_session, reference_world, annotator
):
    _, _, original, _ = reference_world
    item_id = original.dataset_item_id
    task = await linked_task(
        db_session, original, item_id, annotator[0], path="different/source.png"
    )
    with pytest.raises(ContentError) as error:
        await references.reference_context(
            db_session, item_id, annotator[0], task_id=task.id
        )
    assert error.value.code == "content_binding_changed"
