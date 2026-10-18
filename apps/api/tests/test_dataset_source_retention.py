from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.db.models.active_learning import (
    ActiveLearningCandidate,
    ActiveLearningPoolMember,
    ActiveLearningRound,
)
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.services.training_dataset_bridge import content_digest
from tests.factory import create_project, create_task, create_user
from tests.sample_evaluation_fixtures import Objects, seed as seed_model


pytestmark = pytest.mark.asyncio

_GUARD_ITEM = text(
    "SELECT aap_learning_v1.guard_dataset_item_deletion("
    "CAST(:dataset_id AS uuid), CAST(:item_id AS uuid))"
)
_GUARD_UNLINK = text(
    "SELECT aap_learning_v1.guard_dataset_unlink("
    "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid))"
)


async def _guard_item(db, dataset_id: uuid.UUID, item_id: uuid.UUID) -> dict:
    return await db.scalar(_GUARD_ITEM, {"dataset_id": dataset_id, "item_id": item_id})


async def _guard_unlink(db, dataset_id: uuid.UUID, project_id: uuid.UUID) -> dict:
    return await db.scalar(
        _GUARD_UNLINK, {"dataset_id": dataset_id, "project_id": project_id}
    )


async def _seed_source(db):
    owner = await create_user(
        db,
        "project_admin",
        f"source-retention-{uuid.uuid4().hex}@example.test",
        "Source retention owner",
    )
    target_project = await create_project(db, owner_id=owner.id)
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"DS-{uuid.uuid4().hex[:10]}",
        name="Source retention dataset",
        created_by=owner.id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="source.png",
        file_path=f"source-retention/{uuid.uuid4().hex}.png",
        file_type="image",
    )
    db.add(item)
    await db.flush()
    task = await create_task(db, project_id=target_project.id)
    task.dataset_item_id = item.id
    db.add(ProjectDataset(dataset_id=dataset.id, project_id=target_project.id))
    await db.flush()
    other = await seed_model(db, owner, Objects())
    round_id = uuid.uuid4()
    spec = {"schema": 1, "pool": [], "samples": []}
    round = ActiveLearningRound(
        id=round_id,
        project_id=other.project.id,
        creator_id=owner.id,
        model_version_id=other.model.id,
        deployment_id=other.deployment.id,
        request_id=uuid.uuid4(),
        request_digest=content_digest({"round_id": str(round_id)}),
        name="Cross-project source retention round",
        status="failed",
        spec=spec,
        spec_digest=content_digest(spec),
        authorization={},
    )
    db.add(round)
    await db.flush()
    return owner, dataset, item, target_project, task, round


def _blocked() -> dict:
    return {
        "schema": "aap-source-retention/v1",
        "ok": False,
        "error": {"code": "active_learning_reference"},
    }


def _clear() -> dict:
    return {
        "schema": "aap-source-retention/v1",
        "ok": True,
        "data": {},
    }


@pytest.mark.parametrize("reference_kind", ["member", "candidate"])
async def test_item_guard_preserves_cross_project_active_learning_evidence(
    db_session, reference_kind: str
):
    _owner, dataset, item, _project, task, round = await _seed_source(db_session)
    if reference_kind == "member":
        db_session.add(
            ActiveLearningPoolMember(
                round_id=round.id, task_id=task.id, dataset_item_id=item.id
            )
        )
    else:
        db_session.add(
            ActiveLearningCandidate(
                round_id=round.id,
                ordinal=0,
                task_id=task.id,
                dataset_item_id=item.id,
                request_id=uuid.uuid4(),
                status="failed",
            )
        )
    await db_session.flush()

    assert await _guard_item(db_session, dataset.id, item.id) == _blocked()


@pytest.mark.parametrize("reference_kind", ["member", "candidate"])
async def test_unlink_guard_preserves_cross_project_task_references(
    db_session, reference_kind: str
):
    _owner, dataset, item, project, task, round = await _seed_source(db_session)
    if reference_kind == "member":
        db_session.add(
            ActiveLearningPoolMember(
                round_id=round.id, task_id=task.id, dataset_item_id=item.id
            )
        )
    else:
        db_session.add(
            ActiveLearningCandidate(
                round_id=round.id,
                ordinal=0,
                task_id=task.id,
                dataset_item_id=item.id,
                request_id=uuid.uuid4(),
                status="failed",
            )
        )
    await db_session.flush()

    assert await _guard_unlink(db_session, dataset.id, project.id) == _blocked()


async def test_unlink_guard_ignores_same_dataset_tasks_outside_target_project(
    db_session,
):
    _owner, dataset, item, target_project, _target_task, round = await _seed_source(
        db_session
    )
    other_project = await create_project(db_session, owner_id=round.creator_id)
    other_task = await create_task(db_session, project_id=other_project.id)
    other_task.dataset_item_id = item.id
    db_session.add(
        ActiveLearningPoolMember(
            round_id=round.id, task_id=other_task.id, dataset_item_id=item.id
        )
    )
    await db_session.flush()

    assert await _guard_unlink(db_session, dataset.id, target_project.id) == _clear()


async def test_item_guard_scopes_reference_to_the_dataset_owner(
    db_session,
):
    _owner, dataset, item, _project, task, round = await _seed_source(db_session)
    db_session.add(
        ActiveLearningPoolMember(
            round_id=round.id, task_id=task.id, dataset_item_id=item.id
        )
    )
    other_dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"DS-{uuid.uuid4().hex[:10]}",
        name="Unrelated dataset",
        created_by=round.creator_id,
    )
    db_session.add(other_dataset)
    await db_session.flush()

    assert await _guard_item(db_session, dataset.id, item.id) == _blocked()
    assert await _guard_item(db_session, other_dataset.id, item.id) == _clear()
