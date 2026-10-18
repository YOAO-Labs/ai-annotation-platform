from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.active_learning import (
    ActiveLearningCandidate,
    ActiveLearningPoolMember,
    ActiveLearningRound,
)
from app.db.models.project import Project
from app.db.models.project_task_dataset import ProjectTaskDataset
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_recipe import TrainingRecipe, TrainingRecipeRevision
from app.db.models.training_study import TrainingStudy
from app.db.models.trained_model import ModelRelease, ProjectModelSelection
from app.db.models.user import User
from app.services.project_task_dataset import lock_project as lock_task_dataset_project
from tests.factory import create_project, create_task, create_user
from tests.sample_evaluation_fixtures import Objects, seed as seed_model


pytestmark = pytest.mark.asyncio

_LOCK_PROJECT_DELETION = text(
    "SELECT aap_learning_v1.lock_project_deletion(:project_id)"
)
_GUARD_PROJECT_DELETION = text(
    "SELECT aap_learning_v1.guard_project_deletion(:project_id)"
)
_DELETE_PROJECT_OWNED_RECORDS = text(
    "SELECT aap_learning_v1.delete_project_owned_records(:project_id)"
)


def _sqlstate(error: DBAPIError) -> str | None:
    pending = [error.orig]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        state = getattr(current, "sqlstate", None)
        if state:
            return state
        for nested in (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ):
            if nested is not None:
                pending.append(nested)
    return None


async def _create_project(test_engine) -> tuple[uuid.UUID, uuid.UUID]:
    suffix = uuid.uuid4().hex
    user_id, project_id = uuid.uuid4(), uuid.uuid4()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as session:
        session.add(
            User(
                id=user_id,
                email=f"retention-{suffix}@example.test",
                name="Retention owner",
                password_hash="test-only",
                role="project_admin",
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            Project(
                id=project_id,
                display_id=f"PR-{suffix[:16]}",
                name="Project retention contract fixture",
                type_label="Image",
                type_key="image",
                data_type="image",
                owner_id=user_id,
            )
        )
        await session.commit()
    return project_id, user_id


async def _cleanup(test_engine, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(
            delete(TrainingStudy).where(TrainingStudy.project_id == project_id)
        )
        await session.execute(
            delete(ProjectModelSelection).where(
                ProjectModelSelection.project_id == project_id
            )
        )
        await session.execute(
            delete(ModelRelease).where(ModelRelease.project_id == project_id)
        )
        await session.execute(
            delete(TrainingDatasetVersion).where(
                TrainingDatasetVersion.project_id == project_id
            )
        )
        await session.execute(
            delete(ProjectTaskDataset).where(
                ProjectTaskDataset.project_id == project_id
            )
        )
        await session.execute(
            delete(TrainingRecipeRevision).where(
                TrainingRecipeRevision.project_id == project_id
            )
        )
        await session.execute(
            delete(TrainingRecipe).where(TrainingRecipe.project_id == project_id)
        )
        await session.execute(delete(Project).where(Project.id == project_id))
        await session.execute(delete(User).where(User.id == user_id))


async def _guard(session, project_id: uuid.UUID | None) -> dict:
    return (
        await session.execute(_GUARD_PROJECT_DELETION, {"project_id": project_id})
    ).scalar_one()


async def test_frozen_schema_matches_retention_guard_and_sql_capabilities():
    schema_path = (
        Path(__file__).parents[1]
        / "alembic/sql/0196_project_deletion_retention.schema.json"
    )
    artifact_path = (
        Path(__file__).parents[1] / "alembic/sql/0196_project_deletion_retention.sql"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    artifact = artifact_path.read_text(encoding="utf-8")
    functions = {
        "aap_learning_v1.lock_project_deletion(uuid)",
        "aap_learning_v1.guard_project_deletion(uuid)",
        "aap_learning_v1.delete_project_owned_records(uuid)",
    }

    assert set(schema["x-postgresql-functions"]) == functions
    assert all(function.split("(", 1)[0] in artifact for function in functions)
    assert artifact.count("CREATE FUNCTION") == len(functions)
    assert artifact.count("REVOKE ALL ON FUNCTION") == len(functions)
    assert "SET search_path = pg_catalog, pg_temp" in artifact
    assert "aap_labeling_v1.project_task_references(p_project_id)" in artifact
    guard_body = artifact.split(
        "CREATE FUNCTION aap_learning_v1.guard_project_deletion", 1
    )[1].split("$function$;", 1)[0]
    assert "FROM public.tasks" not in guard_body


async def test_deletion_lock_matches_project_task_dataset_advisory_lock(test_engine):
    project_id, user_id = await _create_project(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    locker, contender = maker(), maker()
    try:
        await locker.begin()
        with pytest.raises(DBAPIError) as invalid_lock:
            await locker.execute(_LOCK_PROJECT_DELETION, {"project_id": None})
        assert _sqlstate(invalid_lock.value) == "22023"
        await locker.rollback()

        async with maker() as session:
            response = await _guard(session, None)
            assert response["ok"] is False
            assert response["error"]["code"] == "invalid_request"

        await locker.begin()
        await locker.execute(_LOCK_PROJECT_DELETION, {"project_id": project_id})

        await contender.begin()
        await contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(DBAPIError) as lock_error:
            await lock_task_dataset_project(contender, project_id)
        assert _sqlstate(lock_error.value) == "55P03"
        await contender.rollback()
        await locker.rollback()
    finally:
        await locker.close()
        await contender.close()
        await _cleanup(test_engine, project_id, user_id)


async def test_guard_reports_priority_and_model_release_retention(test_engine):
    project_id, user_id = await _create_project(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    version_id, study_id, release_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    request_id = uuid.uuid4()
    try:
        async with maker() as session:
            session.add(
                TrainingDatasetVersion(
                    id=version_id,
                    project_id=project_id,
                    request_id=request_id,
                    request_digest="a" * 64,
                    name="retention dataset",
                    snapshot={"schema": 1, "task": "image-detection"},
                    snapshot_digest="b" * 64,
                    manifest={},
                )
            )
            await session.flush()
            session.add(
                TrainingStudy(
                    id=study_id,
                    project_id=project_id,
                    dataset_id=version_id,
                    request_id=uuid.uuid4(),
                    request_digest="c" * 64,
                    plan_digest="d" * 64,
                    name="retention study",
                    plan={},
                )
            )
            await session.commit()

        async with maker() as session:
            response = await _guard(session, project_id)
            assert response["schema"] == "aap-project-delete-retention/v1"
            assert response["ok"] is False
            assert response["error"]["code"] == "retained_training_studies"

        async with maker.begin() as session:
            await session.execute(
                delete(TrainingStudy).where(TrainingStudy.project_id == project_id)
            )

        async with maker() as session:
            response = await _guard(session, project_id)
            assert response["error"]["code"] == "retained_training_dataset_versions"

        async with maker.begin() as session:
            await session.execute(
                delete(TrainingDatasetVersion).where(
                    TrainingDatasetVersion.project_id == project_id
                )
            )
            session.add(
                ModelRelease(
                    id=release_id,
                    project_id=project_id,
                    model_version_id=None,
                    deployment_id=None,
                    request_id=uuid.uuid4(),
                    request_digest="e" * 64,
                    generation=1,
                    snapshot={},
                )
            )
            await session.flush()

        async with maker() as session:
            response = await _guard(session, project_id)
            assert response["ok"] is False
            assert response["error"]["code"] == "retained_trained_models"

        async with maker.begin() as session:
            await session.execute(
                delete(ModelRelease).where(ModelRelease.project_id == project_id)
            )
            session.add(ProjectModelSelection(project_id=project_id, generation=0))

        async with maker() as session:
            response = await _guard(session, project_id)
            assert response["ok"] is False
            assert response["error"]["code"] == "retained_trained_models"
    finally:
        await _cleanup(test_engine, project_id, user_id)


@pytest.mark.parametrize("reference_kind", ["member", "candidate"])
async def test_guard_catches_cross_project_active_learning_task_references(
    db_session, reference_kind: str
):
    owner = await create_user(
        db_session,
        "project_admin",
        f"cross-al-retention-{uuid.uuid4().hex}@example.test",
        "Cross-project retention owner",
    )
    target_project = await create_project(db_session, owner_id=owner.id)
    target_task = await create_task(db_session, project_id=target_project.id)
    source = await seed_model(db_session, owner, Objects())
    round_id = uuid.uuid4()
    db_session.add(
        ActiveLearningRound(
            id=round_id,
            project_id=source.project.id,
            model_version_id=source.model.id,
            deployment_id=source.deployment.id,
            request_id=uuid.uuid4(),
            request_digest="3" * 64,
            name="Other project selection",
            status="failed",
            spec={},
            spec_digest="4" * 64,
            authorization={},
        )
    )
    if reference_kind == "member":
        db_session.add(
            ActiveLearningPoolMember(round_id=round_id, task_id=target_task.id)
        )
    else:
        db_session.add(
            ActiveLearningCandidate(
                round_id=round_id,
                ordinal=0,
                task_id=target_task.id,
                request_id=uuid.uuid4(),
                status="failed",
            )
        )
    await db_session.flush()

    response = await _guard(db_session, target_project.id)

    assert response["ok"] is False
    assert response["error"]["code"] == "active_learning_reference"


async def test_owned_cleanup_deletes_only_the_requested_learning_records(test_engine):
    project_id, user_id = await _create_project(test_engine)
    other_project_id, other_user_id = await _create_project(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            target_stream = ProjectTaskDataset(
                id=uuid.uuid4(), project_id=project_id, task="detect", next_ordinal=1
            )
            other_stream = ProjectTaskDataset(
                id=uuid.uuid4(),
                project_id=other_project_id,
                task="detect",
                next_ordinal=1,
            )
            target_recipe = TrainingRecipe(id=uuid.uuid4(), project_id=project_id)
            other_recipe = TrainingRecipe(id=uuid.uuid4(), project_id=other_project_id)
            session.add_all([target_stream, other_stream, target_recipe, other_recipe])
            await session.flush()
            session.add_all(
                [
                    TrainingRecipeRevision(
                        id=uuid.uuid4(),
                        recipe_id=target_recipe.id,
                        project_id=project_id,
                        ordinal=1,
                        name="target revision",
                        snapshot={},
                        snapshot_digest="f" * 64,
                        request_id=uuid.uuid4(),
                        request_digest="1" * 64,
                    ),
                    TrainingRecipeRevision(
                        id=uuid.uuid4(),
                        recipe_id=other_recipe.id,
                        project_id=other_project_id,
                        ordinal=1,
                        name="other revision",
                        snapshot={},
                        snapshot_digest="0" * 64,
                        request_id=uuid.uuid4(),
                        request_digest="2" * 64,
                    ),
                ]
            )

        async with maker.begin() as session:
            deleted = await session.scalar(
                _DELETE_PROJECT_OWNED_RECORDS, {"project_id": project_id}
            )
            assert deleted == 3

        async with maker() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ProjectTaskDataset)
                    .where(ProjectTaskDataset.project_id == project_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrainingRecipe)
                    .where(TrainingRecipe.project_id == project_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrainingRecipeRevision)
                    .where(TrainingRecipeRevision.project_id == project_id)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ProjectTaskDataset)
                    .where(ProjectTaskDataset.project_id == other_project_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrainingRecipe)
                    .where(TrainingRecipe.project_id == other_project_id)
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrainingRecipeRevision)
                    .where(TrainingRecipeRevision.project_id == other_project_id)
                )
                == 1
            )
    finally:
        await _cleanup(test_engine, project_id, user_id)
        await _cleanup(test_engine, other_project_id, other_user_id)
