from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import bindparam, delete, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User


pytestmark = pytest.mark.asyncio

_LOCK_ACTOR = text("SELECT aap_identity_v1.lock_active_actor(:actor_id)").bindparams(
    bindparam("actor_id", type_=PGUUID(as_uuid=True))
)
_LOCK_MANAGER = text(
    "SELECT aap_workspace_v1.lock_project_manager(:actor_id, :project_id)"
).bindparams(
    bindparam("actor_id", type_=PGUUID(as_uuid=True)),
    bindparam("project_id", type_=PGUUID(as_uuid=True)),
)
_ASSOCIATE = text(
    "SELECT aap_assets_v1.associate_project(:actor_id, :dataset_id, :project_id)"
).bindparams(
    bindparam("actor_id", type_=PGUUID(as_uuid=True)),
    bindparam("dataset_id", type_=PGUUID(as_uuid=True)),
    bindparam("project_id", type_=PGUUID(as_uuid=True)),
)


async def _seed(
    test_engine,
    *,
    platform_role: str = "project_admin",
    actor_is_owner: bool = True,
    member_role: str | None = None,
    project_data_type: str = "image",
    dataset_data_type: str = "image",
    scene_mode: bool = False,
    has_scenes: bool = False,
    item_count: int = 0,
    linked: bool = False,
) -> dict[str, uuid.UUID | list[uuid.UUID]]:
    suffix = uuid.uuid4().hex[:10].upper()
    actor_id = uuid.uuid4()
    owner_id = actor_id if actor_is_owner else uuid.uuid4()
    project_id, dataset_id = uuid.uuid4(), uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as session:
        session.add(
            User(
                id=actor_id,
                email=f"creation-actor-{suffix}@example.test",
                name="Creation actor",
                password_hash="test-only",
                role=platform_role,
                is_active=True,
            )
        )
        if owner_id != actor_id:
            session.add(
                User(
                    id=owner_id,
                    email=f"creation-owner-{suffix}@example.test",
                    name="Project owner",
                    password_hash="test-only",
                    role="project_admin",
                    is_active=True,
                )
            )
        await session.flush()
        session.add_all(
            [
                Project(
                    id=project_id,
                    display_id=f"CA-P-{suffix}",
                    name=f"Creation authority project {suffix}",
                    type_label="Image",
                    type_key="image",
                    data_type=project_data_type,
                    scene_mode=scene_mode,
                    owner_id=owner_id,
                ),
                Dataset(
                    id=dataset_id,
                    display_id=f"CA-D-{suffix}",
                    name=f"creation-authority-{suffix}",
                    data_type=dataset_data_type,
                    created_by=actor_id,
                    file_count=item_count,
                ),
            ]
        )
        if member_role is not None:
            session.add(
                ProjectMember(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    user_id=actor_id,
                    role=member_role,
                    assigned_by=owner_id,
                )
            )
        await session.flush()
        item_ids = [uuid.uuid4() for _ in range(item_count)]
        session.add_all(
            DatasetItem(
                id=item_id,
                dataset_id=dataset_id,
                file_name=f"image-{index}.jpg",
                file_path=f"{suffix}/image-{index}.jpg",
                file_type="image",
            )
            for index, item_id in enumerate(item_ids)
        )
        if has_scenes:
            session.add(
                Scene(
                    id=uuid.uuid4(),
                    display_id=f"CA-S-{suffix}",
                    dataset_id=dataset_id,
                    name=f"Scene {suffix}",
                    created_by=actor_id,
                )
            )
        if linked:
            session.add(
                ProjectDataset(
                    id=uuid.uuid4(), project_id=project_id, dataset_id=dataset_id
                )
            )
        await session.commit()

    return {
        "actor_id": actor_id,
        "owner_id": owner_id,
        "project_id": project_id,
        "dataset_id": dataset_id,
        "item_ids": item_ids,
    }


async def _cleanup(test_engine, fixture: dict) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(
            delete(ProjectDataset).where(
                ProjectDataset.dataset_id == fixture["dataset_id"]
            )
        )
        await session.execute(
            delete(DatasetItem).where(DatasetItem.dataset_id == fixture["dataset_id"])
        )
        await session.execute(
            delete(Scene).where(Scene.dataset_id == fixture["dataset_id"])
        )
        await session.execute(
            delete(ProjectMember).where(
                ProjectMember.project_id == fixture["project_id"]
            )
        )
        await session.execute(
            delete(Dataset).where(Dataset.id == fixture["dataset_id"])
        )
        await session.execute(
            delete(Project).where(Project.id == fixture["project_id"])
        )
        await session.execute(
            delete(User).where(User.id.in_([fixture["actor_id"], fixture["owner_id"]]))
        )


async def _call(session: AsyncSession, statement, **params) -> dict:
    return (await session.execute(statement, params)).scalar_one()


def _sqlstate(error: DBAPIError) -> str | None:
    current = error.orig
    for _ in range(4):
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state:
            return state
        current = getattr(current, "__cause__", None)
        if current is None:
            break
    return None


async def test_frozen_schema_lists_exact_versioned_postgres_capabilities():
    schema_path = (
        Path(__file__).parents[1] / "alembic/sql/0192_creation_authority.schema.json"
    )
    artifact_path = (
        Path(__file__).parents[1] / "alembic/sql/0192_creation_authority.sql"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    artifact = artifact_path.read_text(encoding="utf-8")

    functions = {
        "aap_identity_v1.lock_active_actor(uuid)",
        "aap_workspace_v1.lock_project_manager(uuid, uuid)",
        "aap_assets_v1.associate_project(uuid, uuid, uuid)",
    }
    assert set(schema["x-postgresql-functions"]) == functions
    assert all(function.split("(", 1)[0] in artifact for function in functions)
    assert artifact.count("CREATE FUNCTION") == 3
    for name in (
        "activeActorSuccess",
        "activeActorFailure",
        "projectManagerSuccess",
        "projectManagerFailure",
        "datasetAssociationSuccess",
        "datasetAssociationFailure",
        "identityError",
        "authorityError",
        "associationSimpleError",
        "associationMismatchError",
    ):
        assert schema["$defs"][name]["additionalProperties"] is False
    for name in (
        "activeActorSuccess",
        "projectManagerSuccess",
        "datasetAssociationSuccess",
    ):
        assert (
            schema["$defs"][name]["properties"]["data"]["additionalProperties"] is False
        )
    assert (
        schema["$defs"]["associationMismatchError"]["properties"]["details"][
            "additionalProperties"
        ]
        is False
    )
    assert "SET search_path = pg_catalog, pg_temp" in artifact
    assert artifact.count("REVOKE ALL ON FUNCTION") == 3


async def test_identity_and_workspace_return_current_facts_without_changing_caller_role(
    test_engine,
):
    fixture = await _seed(test_engine, platform_role="project_admin")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            role_before = await session.scalar(text("SELECT current_user"))
            actor = await _call(session, _LOCK_ACTOR, actor_id=fixture["actor_id"])
            manager = await _call(
                session,
                _LOCK_MANAGER,
                actor_id=fixture["actor_id"],
                project_id=fixture["project_id"],
            )
            role_after = await session.scalar(text("SELECT current_user"))

            assert actor == {
                "schema": "aap-active-actor/v1",
                "ok": True,
                "data": {
                    "actor_id": str(fixture["actor_id"]),
                    "platform_role": "project_admin",
                },
            }
            assert manager["schema"] == "aap-project-manager/v1"
            assert manager["ok"] is True
            assert manager["data"] == {
                "actor_id": str(fixture["actor_id"]),
                "platform_role": "project_admin",
                "project_id": str(fixture["project_id"]),
                "owner_id": str(fixture["owner_id"]),
                "data_type": "image",
                "scene_mode": False,
            }
            assert role_after == role_before
            await session.rollback()
    finally:
        await _cleanup(test_engine, fixture)


async def test_unknown_platform_role_is_reported_after_project_visibility_check(
    test_engine,
):
    fixture = await _seed(test_engine, platform_role="legacy_role")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            actor = await _call(session, _LOCK_ACTOR, actor_id=fixture["actor_id"])
            unknown_project = await _call(
                session,
                _LOCK_MANAGER,
                actor_id=fixture["actor_id"],
                project_id=fixture["project_id"],
            )
            missing_project = await _call(
                session,
                _LOCK_MANAGER,
                actor_id=fixture["actor_id"],
                project_id=uuid.uuid4(),
            )
            assert actor["ok"] is True
            assert actor["data"]["platform_role"] == "legacy_role"
            assert unknown_project["error"]["code"] == "invalid_account_role"
            assert missing_project["error"]["code"] == "project_missing"
            await session.rollback()
    finally:
        await _cleanup(test_engine, fixture)


async def test_inactive_actor_error_uses_each_capabilitys_response_schema(test_engine):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET is_active = false WHERE id = :id"),
                {"id": fixture["actor_id"]},
            )
        async with maker() as session:
            actor = await _call(session, _LOCK_ACTOR, actor_id=fixture["actor_id"])
            manager = await _call(
                session,
                _LOCK_MANAGER,
                actor_id=fixture["actor_id"],
                project_id=fixture["project_id"],
            )
            association = await _call(
                session,
                _ASSOCIATE,
                actor_id=fixture["actor_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
            )
            assert actor == {
                "schema": "aap-active-actor/v1",
                "ok": False,
                "error": {"code": "account_not_active"},
            }
            assert manager == {
                "schema": "aap-project-manager/v1",
                "ok": False,
                "error": {"code": "account_not_active"},
            }
            assert association == {
                "schema": "aap-dataset-association/v1",
                "ok": False,
                "error": {"code": "account_not_active"},
            }
            await session.rollback()
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.parametrize(
    ("platform_role", "member_role", "expected"),
    [
        ("employee", None, "project_missing"),
        ("employee", "annotator", "project_manager_required"),
        ("viewer", "annotator", "membership_incompatible"),
        ("employee", "legacy_role", "invalid_project_role"),
        ("project_admin", "annotator", "project_manager_required"),
    ],
)
async def test_membership_visibility_and_manager_failures_match_current_policy(
    test_engine, platform_role: str, member_role: str | None, expected: str
):
    fixture = await _seed(
        test_engine,
        platform_role=platform_role,
        actor_is_owner=False,
        member_role=member_role,
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            response = await _call(
                session,
                _LOCK_MANAGER,
                actor_id=fixture["actor_id"],
                project_id=fixture["project_id"],
            )
            assert response["ok"] is False
            assert response["error"]["code"] == expected
            await session.rollback()
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.parametrize(
    ("project_data_type", "dataset_data_type", "scene_mode", "has_scenes"),
    [
        ("point_cloud", "lidar", True, True),
        ("lidar", "pointcloud", False, False),
        ("pointcloud", "point_cloud", False, False),
        ("", "", False, False),
    ],
)
async def test_association_uses_media_aliases_counts_items_and_replays_existing_link(
    test_engine,
    project_data_type: str,
    dataset_data_type: str,
    scene_mode: bool,
    has_scenes: bool,
):
    fixture = await _seed(
        test_engine,
        project_data_type=project_data_type,
        dataset_data_type=dataset_data_type,
        scene_mode=scene_mode,
        has_scenes=has_scenes,
        item_count=2,
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            response = await _call(
                session,
                _ASSOCIATE,
                actor_id=fixture["actor_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
            )
            assert response["schema"] == "aap-dataset-association/v1"
            assert response["ok"] is True
            assert response["data"]["existing"] is False
            assert response["data"]["item_count"] == 2
            assert response["data"]["project_data_type"] == project_data_type
            link_id = response["data"]["link_id"]
            await session.commit()

        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.datasets SET data_type = 'image' WHERE id = :id"),
                {"id": fixture["dataset_id"]},
            )
        async with maker() as session:
            response = await _call(
                session,
                _ASSOCIATE,
                actor_id=fixture["actor_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
            )
            assert response["ok"] is True
            assert response["data"] == {
                "link_id": link_id,
                "existing": True,
                "item_count": 0,
                "project_data_type": project_data_type,
            }
            await session.rollback()
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.parametrize(
    ("project_data_type", "dataset_data_type", "scene_mode", "has_scenes", "expected"),
    [
        ("image", "video", False, False, "media_kind_mismatch"),
        ("image", "image", True, False, "scene_kind_mismatch"),
        ("image", "image", False, True, "scene_kind_mismatch"),
    ],
)
async def test_association_mismatch_returns_frozen_source_facts(
    test_engine,
    project_data_type: str,
    dataset_data_type: str,
    scene_mode: bool,
    has_scenes: bool,
    expected: str,
):
    fixture = await _seed(
        test_engine,
        project_data_type=project_data_type,
        dataset_data_type=dataset_data_type,
        scene_mode=scene_mode,
        has_scenes=has_scenes,
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            response = await _call(
                session,
                _ASSOCIATE,
                actor_id=fixture["actor_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
            )
            assert response["ok"] is False
            assert response["error"]["code"] == expected
            assert response["error"]["details"] == {
                "project_data_type": project_data_type,
                "dataset_data_type": dataset_data_type,
                "project_scene_mode": scene_mode,
                "dataset_has_scenes": has_scenes,
            }
            await session.rollback()
    finally:
        await _cleanup(test_engine, fixture)


async def test_authority_nowait_contention_and_project_lock_survive_until_transaction_end(
    test_engine,
):
    fixture = await _seed(
        test_engine,
        platform_role="employee",
        member_role="annotator",
        actor_is_owner=False,
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    holder, contender = maker(), maker()
    try:
        await holder.begin()
        await holder.execute(
            text(
                "SELECT id FROM public.project_members WHERE project_id = :id FOR UPDATE"
            ),
            {"id": fixture["project_id"]},
        )
        response = await _call(
            contender,
            _LOCK_MANAGER,
            actor_id=fixture["actor_id"],
            project_id=fixture["project_id"],
        )
        assert response["error"]["code"] == "membership_busy"
        await contender.rollback()
        await holder.rollback()

        await holder.begin()
        await holder.execute(
            text("SELECT id FROM public.projects WHERE id = :id FOR UPDATE"),
            {"id": fixture["project_id"]},
        )
        response = await _call(
            contender,
            _LOCK_MANAGER,
            actor_id=fixture["actor_id"],
            project_id=fixture["project_id"],
        )
        assert response["error"]["code"] == "project_busy"
        await contender.rollback()
        await holder.rollback()

        await holder.begin()
        response = await _call(
            holder,
            _LOCK_MANAGER,
            actor_id=fixture["owner_id"],
            project_id=fixture["project_id"],
        )
        assert response["ok"] is True
        with pytest.raises(DBAPIError) as transfer:
            await contender.execute(
                text(
                    "SELECT id FROM public.projects WHERE id = :id FOR NO KEY UPDATE NOWAIT"
                ),
                {"id": fixture["project_id"]},
            )
        assert _sqlstate(transfer.value) == "55P03"
        await contender.rollback()
        await holder.commit()

        await contender.begin()
        assert (
            await contender.scalar(
                text("SELECT id FROM public.projects WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": fixture["project_id"]},
            )
            == fixture["project_id"]
        )
        await contender.rollback()
    finally:
        await holder.close()
        await contender.close()
        await _cleanup(test_engine, fixture)


async def test_identity_account_lock_is_nowait_and_released_on_rollback(test_engine):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    holder, contender = maker(), maker()
    try:
        await holder.begin()
        await holder.execute(
            text("SELECT id FROM public.users WHERE id = :id FOR UPDATE"),
            {"id": fixture["actor_id"]},
        )
        response = await _call(holder, _LOCK_ACTOR, actor_id=uuid.uuid4())
        assert response["error"]["code"] == "account_not_active"
        response = await _call(contender, _LOCK_ACTOR, actor_id=fixture["actor_id"])
        assert response["error"]["code"] == "account_busy"
        await contender.rollback()
        await holder.rollback()

        async with contender.begin():
            response = await _call(contender, _LOCK_ACTOR, actor_id=fixture["actor_id"])
            assert response["ok"] is True
    finally:
        await holder.close()
        await contender.close()
        await _cleanup(test_engine, fixture)
