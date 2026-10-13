"""A2 · annotation-phase contributor evidence across real producers (DB-backed).

Each test drives one producer through the public service/HTTP boundary and
asserts the acting user was accumulated on the task.  The pure sticky-unknown
semantics live in ``test_annotation_evidence.py``; first-review performance
attribution must stay unchanged (asserted in the submit/skip cases).

These tests require the isolated PostgreSQL test database described in
``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.prediction import Prediction
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.annotation_evidence import (
    freeze_review_contributor_evidence,
    record_annotation_actor,
    refresh_task_evidence,
)
from app.services.annotation_slice import AnnotationSliceService
from app.services.batch import BatchService
from tests.factory import create_membership, create_project, create_task, create_user

pytestmark = pytest.mark.asyncio


def _bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def _token_for(user, role: str = "super_admin") -> str:
    from app.core.security import create_access_token

    return create_access_token(subject=str(user.id), role=role)


def _bbox_payload(class_name: str = "car") -> dict:
    return {
        "annotation_type": "bbox",
        "class_name": class_name,
        "geometry": {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
    }


async def _seed_task(db, owner, *, accumulator="empty", status="in_progress"):
    project = await create_project(db, owner_id=owner.id)
    task = await create_task(db, project_id=project.id, status=status)
    task.annotation_contributor_ids = [] if accumulator == "empty" else None
    await db.flush()
    return project, task


async def _second_editor(db, *, email: str):
    """A second privileged actor so accumulation is observable across requests."""
    return await create_user(db, "super_admin", email, "Second Editor")


# ── direct annotation API ────────────────────────────────────────────


async def test_create_records_actor_on_known_accumulator(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    _, task = await _seed_task(db_session, user)

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_bbox_payload(),
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text

    await db_session.refresh(task)
    assert task.annotation_contributor_ids == [str(user.id)]


async def test_create_on_legacy_null_stays_unknown(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    _, task = await _seed_task(db_session, user, accumulator="unknown")

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_bbox_payload(),
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text

    await db_session.refresh(task)
    assert task.annotation_contributor_ids is None


async def test_update_records_the_editing_actor(httpx_client, db_session, super_admin):
    owner, owner_token = super_admin
    editor = await _second_editor(db_session, email="patch-editor@test.local")
    editor_token = _token_for(editor)
    _project, task = await _seed_task(db_session, owner)

    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_bbox_payload(),
        headers=_bearer(owner_token),
    )
    assert created.status_code == 201, created.text
    annotation_id = created.json()["id"]

    patched = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation_id}",
        json={"attributes": {"reviewed": True}},
        headers=_bearer(editor_token),
    )
    assert patched.status_code == 200, patched.text

    await db_session.refresh(task)
    assert task.annotation_contributor_ids == sorted([str(owner.id), str(editor.id)])


async def test_delete_records_the_deleting_actor_and_retains_authors(
    httpx_client, db_session, super_admin
):
    owner, owner_token = super_admin
    editor = await _second_editor(db_session, email="delete-editor@test.local")
    editor_token = _token_for(editor)
    _project, task = await _seed_task(db_session, owner)

    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_bbox_payload(),
        headers=_bearer(owner_token),
    )
    assert created.status_code == 201, created.text
    annotation_id = created.json()["id"]

    deleted = await httpx_client.delete(
        f"/api/v1/tasks/{task.id}/annotations/{annotation_id}",
        headers=_bearer(editor_token),
    )
    assert deleted.status_code == 204, deleted.text

    await db_session.refresh(task)
    # Conservative: the deleted annotation's author stays an accumulator member.
    assert task.annotation_contributor_ids == sorted([str(owner.id), str(editor.id)])


# ── submit / skip freeze and lifecycle invalidation ──────────────────


async def test_submit_freezes_round_evidence_without_changing_first_review(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    _, task = await _seed_task(db_session, user)
    task.assignee_id = user.id
    await db_session.flush()

    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_bbox_payload(),
        headers=_bearer(token),
    )
    assert created.status_code == 201, created.text

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/submit", headers=_bearer(token)
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(task)
    assert task.status == "review"
    assert task.review_round_id is not None
    assert task.review_submitter_id == user.id
    assert task.review_contributor_ids == [str(user.id)]
    # First-review performance facts are unchanged.
    assert task.first_review_contributor_ids == [str(user.id)]


async def test_skip_freezes_round_evidence(httpx_client, db_session, super_admin):
    user, token = super_admin
    _, task = await _seed_task(db_session, user, status="pending")
    task.assignee_id = user.id
    await db_session.flush()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/skip",
        json={"reason": "no_target"},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(task)
    assert task.review_round_id is not None
    assert task.review_submitter_id == user.id
    assert task.review_contributor_ids == [str(user.id)]
    assert task.first_review_contributor_ids == [str(user.id)]


async def test_review_adjustment_does_not_add_reviewer_as_contributor(
    httpx_client, db_session, super_admin
):
    owner, owner_token = super_admin
    reviewer = await create_user(
        db_session, "employee", "review-edit@test.local", "Reviewer"
    )
    reviewer_token = _token_for(reviewer, role="employee")
    project, task = await _seed_task(db_session, owner)
    task.assignee_id = owner.id
    await create_membership(
        db_session, project_id=project.id, user_id=reviewer.id, role="reviewer"
    )
    await db_session.flush()

    created = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_bbox_payload(),
        headers=_bearer(owner_token),
    )
    assert created.status_code == 201, created.text
    annotation_id = created.json()["id"]

    submitted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/submit", headers=_bearer(owner_token)
    )
    assert submitted.status_code == 200, submitted.text

    task.reviewer_id = reviewer.id
    await db_session.flush()
    adjusted = await httpx_client.patch(
        f"/api/v1/tasks/{task.id}/annotations/{annotation_id}",
        json={"attributes": {"qc_adjusted": True}},
        headers=_bearer(reviewer_token),
    )
    assert adjusted.status_code == 200, adjusted.text

    await db_session.refresh(task)
    assert task.annotation_contributor_ids == [str(owner.id)]
    assert str(reviewer.id) not in (task.annotation_contributor_ids or [])


async def test_withdraw_invalidates_round_but_retains_accumulator(
    httpx_client, db_session, super_admin, annotator
):
    owner, _ = super_admin
    user, token = annotator
    project, task = await _seed_task(db_session, owner)
    # The submitter/withdrawer is the literal project annotator, not a manager,
    # so withdrawing their own submission is not a self-review decision.
    await create_membership(
        db_session, project_id=project.id, user_id=user.id, role="annotator"
    )
    task.assignee_id = user.id
    await db_session.flush()
    await httpx_client.post(
        f"/api/v1/tasks/{task.id}/annotations",
        json=_bbox_payload(),
        headers=_bearer(token),
    )
    submitted = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/submit", headers=_bearer(token)
    )
    assert submitted.status_code == 200, submitted.text

    withdrawn = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/withdraw", headers=_bearer(token)
    )
    assert withdrawn.status_code == 200, withdrawn.text

    await db_session.refresh(task)
    assert task.status == "in_progress"
    assert task.review_contributor_ids is None
    assert task.review_submitter_id is None
    assert task.annotation_contributor_ids == [str(user.id)]


# ── AI acceptance and import side routes ─────────────────────────────


async def test_accept_prediction_records_actor(httpx_client, db_session, super_admin):
    owner, token = super_admin
    project, task = await _seed_task(db_session, owner, status="pending")
    prediction = Prediction(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        ml_backend_id=None,
        model_version="test",
        score=0.9,
        result=[
            {
                "type": "rectanglelabels",
                "value": {
                    "x": 0,
                    "y": 0,
                    "width": 10,
                    "height": 10,
                    "rectanglelabels": ["car"],
                },
                "score": 0.9,
            }
        ],
    )
    db_session.add(prediction)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/predictions/{prediction.id}/accept?shape_index=0",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(task)
    assert task.annotation_contributor_ids == [str(owner.id)]


async def test_aap_import_records_operator(db_session, super_admin):
    from tests.test_annotations_import import (
        _aap_envelope,
        _ann_entry,
        _seed_project_with_tasks,
    )

    operator, _ = super_admin
    project, tasks = await _seed_project_with_tasks(db_session, operator.id, n_tasks=1)
    tasks[0].annotation_contributor_ids = []
    await db_session.flush()
    envelope = _aap_envelope(
        [
            {
                "task_match": {"display_id": tasks[0].display_id},
                "annotations": [
                    _ann_entry(
                        geometry={
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        }
                    )
                ],
            }
        ]
    )

    from app.services.annotations_import import import_aap_json_annotations

    result = await import_aap_json_annotations(
        db_session,
        project.id,
        envelope,
        operator_user_id=operator.id,
    )
    assert result.imported == 1, result.errors

    await db_session.refresh(tasks[0])
    assert tasks[0].annotation_contributor_ids == [str(operator.id)]


# ── undo / restore side route ────────────────────────────────────────


async def test_slice_commit_and_restore_record_the_actor(db_session, super_admin):
    from tests.test_annotation_slices import _payload, _restore, _seed

    owner, _ = super_admin
    editor = await create_user(
        db_session, "super_admin", "slice-editor@test.local", "Slice Editor"
    )
    task, source, _parent = await _seed(db_session, owner)
    task.annotation_contributor_ids = []
    await db_session.flush()

    service = AnnotationSliceService(db_session)
    committed = await service.commit(task.id, _payload(source), owner)
    await db_session.flush()

    await db_session.refresh(task)
    assert task.annotation_contributor_ids == [str(owner.id)]

    restore_payload = _restore(committed)
    await service.restore(
        task.id, committed.slice_operation_id, restore_payload, editor
    )
    await db_session.flush()

    await db_session.refresh(task)
    assert task.annotation_contributor_ids == sorted([str(owner.id), str(editor.id)])


# ── helper semantics with a real row lock ────────────────────────────


async def test_new_orm_task_defaults_to_known_empty(db_session, super_admin):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=project.id)
    await db_session.refresh(task)

    assert task.annotation_contributor_ids == []


async def test_old_binary_raw_task_insert_stays_unknown(db_session, super_admin):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    value = await db_session.scalar(
        text(
            "INSERT INTO tasks (id, project_id, display_id, file_name, file_path, "
            "file_type, tags, status, is_labeled, overlap, total_annotations, "
            "total_predictions) VALUES (:id, :project, :display, 'legacy.png', "
            "'legacy.png', 'image', '[]'::jsonb, 'pending', false, 1, 0, 0) "
            "RETURNING annotation_contributor_ids"
        ),
        {
            "id": uuid.uuid4(),
            "project": project.id,
            "display": f"LEGACY-{uuid.uuid4().hex[:12]}",
        },
    )
    assert value is None


@pytest.mark.parametrize("nowait", [False, True])
async def test_same_transaction_two_actors_union(db_session, super_admin, nowait):
    owner, _ = super_admin
    first = await create_user(
        db_session, "super_admin", "union-a@test.local", "Union A"
    )
    second = await create_user(
        db_session, "super_admin", "union-b@test.local", "Union B"
    )
    _project, task = await _seed_task(db_session, owner)

    assert (
        await record_annotation_actor(db_session, task, first.id, nowait=nowait) is True
    )
    # A second record in the same transaction must not discard the first: the
    # helper flushes pending accumulator changes before the locking refresh.
    assert (
        await record_annotation_actor(db_session, task, second.id, nowait=nowait)
        is True
    )
    await db_session.flush()
    await db_session.refresh(task)

    assert task.annotation_contributor_ids == sorted([str(first.id), str(second.id)])


async def test_freeze_is_immutable_for_the_round(db_session, super_admin):
    owner, _ = super_admin
    writer = await create_user(
        db_session, "super_admin", "freeze-writer@test.local", "Writer"
    )
    late = await create_user(
        db_session, "super_admin", "freeze-late@test.local", "Late"
    )
    _project, task = await _seed_task(db_session, owner)
    await record_annotation_actor(db_session, task, writer.id)

    frozen = freeze_review_contributor_evidence(
        task, submitter_id=owner.id, contributor_ids=[]
    )
    assert frozen == sorted([str(owner.id), str(writer.id)])

    # A later write in the same round accumulates but must not mutate the frozen
    # round evidence.
    await record_annotation_actor(db_session, task, late.id)
    assert task.review_contributor_ids == frozen
    assert str(late.id) in task.annotation_contributor_ids


async def test_concurrent_recorders_serialize_without_lost_update(test_engine):
    """Two connections recording the same task must both survive the row lock."""
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as seed:
        suffix = uuid.uuid4().hex[:8]
        user_a = await create_user(
            seed, "super_admin", f"conc-a-{suffix}@test.local", "Conc A"
        )
        user_b = await create_user(
            seed, "super_admin", f"conc-b-{suffix}@test.local", "Conc B"
        )
        project = await create_project(seed, owner_id=user_a.id)
        task = await create_task(seed, project_id=project.id, status="in_progress")
        task.annotation_contributor_ids = []
        await seed.commit()
        task_id, project_id = task.id, project.id
        user_a_id, user_b_id = user_a.id, user_b.id

    session_a = maker()
    session_b = maker()
    try:
        task_a = await session_a.get(Task, task_id)
        assert await record_annotation_actor(session_a, task_a, user_a_id) is True

        async def _record_b() -> bool:
            task_b = await session_b.get(Task, task_id)
            return await record_annotation_actor(session_b, task_b, user_b_id)

        pending = asyncio.create_task(_record_b())
        # The second connection must block on the first connection's task row lock.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), timeout=0.5)
        assert not pending.done()

        await session_a.commit()
        assert await asyncio.wait_for(pending, timeout=10) is True
        await session_b.commit()

        async with maker() as check:
            fresh = await check.get(Task, task_id)
            assert fresh.annotation_contributor_ids == sorted(
                [str(user_a_id), str(user_b_id)]
            )
    finally:
        await session_a.close()
        await session_b.close()
        async with maker() as cleanup:
            await cleanup.execute(delete(Task).where(Task.id == task_id))
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(
                delete(User).where(User.id.in_([user_a_id, user_b_id]))
            )
            await cleanup.commit()


async def _seed_evidence_fixture(maker, prefix: str) -> dict:
    async with maker() as seed:
        suffix = uuid.uuid4().hex[:8]
        owner = await create_user(
            seed, "super_admin", f"{prefix}-owner-{suffix}@test.local", "Owner"
        )
        writer = await create_user(
            seed, "super_admin", f"{prefix}-writer-{suffix}@test.local", "Writer"
        )
        other = await create_user(
            seed, "super_admin", f"{prefix}-other-{suffix}@test.local", "Other"
        )
        project = await create_project(seed, owner_id=owner.id)
        task = await create_task(seed, project_id=project.id, status="in_progress")
        task.annotation_contributor_ids = []
        await seed.commit()
        return {
            "task": task.id,
            "project": project.id,
            "owner": owner.id,
            "writer": writer.id,
            "other": other.id,
        }


async def _drop_evidence_fixture(maker, fixture: dict) -> None:
    async with maker() as cleanup:
        await cleanup.execute(delete(Task).where(Task.id == fixture["task"]))
        await cleanup.execute(delete(Project).where(Project.id == fixture["project"]))
        await cleanup.execute(
            delete(User).where(
                User.id.in_([fixture["owner"], fixture["writer"], fixture["other"]])
            )
        )
        await cleanup.commit()


@pytest.mark.parametrize("dirty", [False, True])
async def test_nowait_busy_lock_rolls_back_to_409(test_engine, dirty):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    fixture = await _seed_evidence_fixture(maker, "busy")
    session_a = maker()
    session_b = maker()
    try:
        task_a = await session_a.get(Task, fixture["task"])
        assert (
            await record_annotation_actor(session_a, task_a, fixture["writer"]) is True
        )

        task_b = await session_b.get(Task, fixture["task"])
        if dirty:
            # An autoflush UPDATE must not block before the NOWAIT lock attempt.
            task_b.total_annotations = 123
        with pytest.raises(HTTPException) as caught:
            await asyncio.wait_for(
                record_annotation_actor(
                    session_b, task_b, fixture["other"], nowait=True
                ),
                timeout=2,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail["reason"] == "task_evidence_busy"
        assert not session_b.in_transaction()

        await session_a.commit()
        async with maker() as check:
            fresh = await check.get(Task, fixture["task"])
            assert fresh.annotation_contributor_ids == [str(fixture["writer"])]
            assert fresh.total_annotations == 0
    finally:
        await session_a.close()
        await session_b.close()
        await _drop_evidence_fixture(maker, fixture)


async def test_stale_phase_and_prefreeze_writer_are_refreshed(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    fixture = await _seed_evidence_fixture(maker, "stale")
    stale = maker()
    writer_session = maker()
    review_session = maker()
    pending = None
    try:
        # A stale annotation-phase instance is loaded while the task is editable.
        stale_task = await stale.get(Task, fixture["task"])
        assert stale_task.status == "in_progress"
        review_task = await review_session.get(Task, fixture["task"])

        # A pre-freeze writer holds the task lock with uncommitted evidence.
        writer_task = await writer_session.get(Task, fixture["task"])
        assert (
            await record_annotation_actor(
                writer_session, writer_task, fixture["writer"]
            )
            is True
        )

        async def submit_after_writer():
            await refresh_task_evidence(review_session, review_task)
            review_task.status = "review"
            review_task.review_round_id = uuid.uuid4()
            frozen = freeze_review_contributor_evidence(
                review_task, submitter_id=fixture["writer"], contributor_ids=[]
            )
            await review_session.commit()
            return frozen

        pending = asyncio.create_task(submit_after_writer())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending), timeout=0.2)
        await writer_session.commit()
        frozen = await asyncio.wait_for(pending, timeout=10)
        assert frozen == [str(fixture["writer"])]

        # The stale instance still believes it is in the annotation phase; the
        # recorder must refresh the committed review phase and refuse to credit
        # the reviewer.
        assert (
            await record_annotation_actor(stale, stale_task, fixture["other"]) is False
        )
        assert stale_task.status == "review"
        await stale.commit()

        async with maker() as check:
            fresh = await check.get(Task, fixture["task"])
            assert str(fixture["writer"]) in fresh.review_contributor_ids
            assert str(fixture["other"]) not in (fresh.annotation_contributor_ids or [])
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stale.close()
        await writer_session.close()
        await review_session.close()
        await _drop_evidence_fixture(maker, fixture)


async def test_clean_task_predictions_records_actor_and_preserves_contributors(
    db_session, super_admin
):
    owner, _ = super_admin
    editor = await create_user(
        db_session, "super_admin", "cleanup-editor@test.local", "Cleanup Editor"
    )
    _project, task = await _seed_task(db_session, owner)
    task.annotation_contributor_ids = [str(owner.id)]
    ai_annotation = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner.id,
        source="prediction_based",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        is_active=True,
    )
    db_session.add(ai_annotation)
    await db_session.flush()

    await BatchService(db_session).clean_task_predictions([task.id], actor_id=editor.id)
    await db_session.flush()
    await db_session.refresh(task)
    await db_session.refresh(ai_annotation)

    assert ai_annotation.is_active is False
    # Existing contributor is preserved and the deletion actor is added.
    assert task.annotation_contributor_ids == sorted([str(owner.id), str(editor.id)])


async def test_metadata_only_prediction_cleanup_keeps_contributors(
    db_session, super_admin
):
    owner, _ = super_admin
    _project, task = await _seed_task(db_session, owner)
    task.annotation_contributor_ids = [str(owner.id)]
    manual = Annotation(
        task_id=task.id,
        project_id=task.project_id,
        user_id=owner.id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        is_active=True,
    )
    db_session.add(manual)
    await db_session.flush()

    await BatchService(db_session).clean_task_predictions([task.id])
    await db_session.flush()
    await db_session.refresh(task)
    await db_session.refresh(manual)

    # No accepted AI content was removed, so no deletion actor is recorded.
    assert manual.is_active is True
    assert task.annotation_contributor_ids == [str(owner.id)]
