from __future__ import annotations

import uuid

import pytest
from sqlalchemy import bindparam, delete, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.asset_storage_deletion import AssetStorageDeletion
from app.db.models.bug_report import BugComment, BugReport
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.image_pyramid import ImagePyramidAsset, ImagePyramidGeneration
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User

pytestmark = pytest.mark.asyncio

_ITEM_REFERENCES = text(
    "SELECT item_id FROM aap_assets_v1.dataset_item_references(:dataset_id, :item_id)"
).bindparams(
    bindparam("dataset_id", type_=PGUUID(as_uuid=True)),
    bindparam("item_id", type_=PGUUID(as_uuid=True)),
)
_SCENE_REFERENCES = text(
    "SELECT scene_id FROM aap_assets_v1.dataset_scene_references(:dataset_id)"
).bindparams(bindparam("dataset_id", type_=PGUUID(as_uuid=True)))
_REMOVE_ASSOCIATION = text(
    "SELECT aap_assets_v1.remove_dataset_association("
    ":dataset_id, :project_id, :association_id)"
).bindparams(
    bindparam("dataset_id", type_=PGUUID(as_uuid=True)),
    bindparam("project_id", type_=PGUUID(as_uuid=True)),
    bindparam("association_id", type_=PGUUID(as_uuid=True)),
)
_PREPARE_CLEANUP = text(
    "SELECT aap_assets_v1.prepare_dataset_unlink_cleanup("
    ":operation_id, :dataset_id, :project_id, :bucket)"
).bindparams(
    bindparam("operation_id", type_=PGUUID(as_uuid=True)),
    bindparam("dataset_id", type_=PGUUID(as_uuid=True)),
    bindparam("project_id", type_=PGUUID(as_uuid=True)),
)
_PENDING_DELETIONS = text(
    "SELECT * FROM aap_assets_v1.pending_storage_deletions(:operation_id, :limit)"
).bindparams(
    bindparam("operation_id", type_=PGUUID(as_uuid=True)),
    bindparam("limit"),
)
_COMPLETE_DELETION = text(
    "SELECT aap_assets_v1.complete_storage_deletion(:operation_id, :asset_id)"
).bindparams(
    bindparam("operation_id", type_=PGUUID(as_uuid=True)),
    bindparam("asset_id", type_=PGUUID(as_uuid=True)),
)
_DETACH_REPORTS = text(
    "SELECT aap_diagnostics_v1.detach_dataset_task_references(:dataset_id, :project_id)"
).bindparams(
    bindparam("dataset_id", type_=PGUUID(as_uuid=True)),
    bindparam("project_id", type_=PGUUID(as_uuid=True)),
)


async def _seed(test_engine) -> dict[str, uuid.UUID]:
    suffix = uuid.uuid4().hex[:10].upper()
    fixture = {
        "user_id": uuid.uuid4(),
        "dataset_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "other_project_id": uuid.uuid4(),
        "scene_a": uuid.uuid4(),
        "scene_b": uuid.uuid4(),
        "item_a": uuid.uuid4(),
        "item_b": uuid.uuid4(),
        "item_c": uuid.uuid4(),
        "association_id": uuid.uuid4(),
        "other_association_id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "other_task_id": uuid.uuid4(),
        "task_asset_id": uuid.uuid4(),
        "other_task_asset_id": uuid.uuid4(),
        "item_asset_id": uuid.uuid4(),
        "report_target_id": uuid.uuid4(),
        "report_unrelated_id": uuid.uuid4(),
        "comment_id": uuid.uuid4(),
        "operation_id": uuid.uuid4(),
        "other_operation_id": uuid.uuid4(),
    }
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        session.add(
            User(
                id=fixture["user_id"],
                email=f"dataset-unlink-{suffix}@example.test",
                name="Dataset unlink fixture",
                password_hash="test-only",
                role="super_admin",
                is_active=True,
            )
        )
        await session.flush()
        session.add_all(
            [
                Dataset(
                    id=fixture["dataset_id"],
                    display_id=f"DU-{suffix}",
                    name=f"dataset-unlink-{suffix}",
                    data_type="image",
                    created_by=fixture["user_id"],
                    file_count=3,
                ),
                Project(
                    id=fixture["project_id"],
                    display_id=f"PU-{suffix}",
                    name="Dataset unlink project",
                    type_label="Image",
                    type_key="image",
                    data_type="image",
                    owner_id=fixture["user_id"],
                ),
                Project(
                    id=fixture["other_project_id"],
                    display_id=f"PX-{suffix}",
                    name="Other dataset unlink project",
                    type_label="Image",
                    type_key="image",
                    data_type="image",
                    owner_id=fixture["user_id"],
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Scene(
                    id=fixture["scene_a"],
                    display_id=f"SA-{suffix}",
                    dataset_id=fixture["dataset_id"],
                    name="Scene A",
                    created_by=fixture["user_id"],
                ),
                Scene(
                    id=fixture["scene_b"],
                    display_id=f"SB-{suffix}",
                    dataset_id=fixture["dataset_id"],
                    name="Scene B",
                    created_by=fixture["user_id"],
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                DatasetItem(
                    id=fixture["item_a"],
                    dataset_id=fixture["dataset_id"],
                    file_name="a.jpg",
                    file_path=f"{suffix}/a.jpg",
                    file_type="image",
                    file_size=10,
                    scene_id=fixture["scene_a"],
                ),
                DatasetItem(
                    id=fixture["item_b"],
                    dataset_id=fixture["dataset_id"],
                    file_name="b.jpg",
                    file_path=f"{suffix}/b.jpg",
                    file_type="image",
                    file_size=11,
                    scene_id=fixture["scene_a"],
                ),
                DatasetItem(
                    id=fixture["item_c"],
                    dataset_id=fixture["dataset_id"],
                    file_name="c.jpg",
                    file_path=f"{suffix}/c.jpg",
                    file_type="image",
                    file_size=12,
                    scene_id=fixture["scene_b"],
                ),
                ProjectDataset(
                    id=fixture["association_id"],
                    project_id=fixture["project_id"],
                    dataset_id=fixture["dataset_id"],
                ),
                ProjectDataset(
                    id=fixture["other_association_id"],
                    project_id=fixture["other_project_id"],
                    dataset_id=fixture["dataset_id"],
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Task(
                    id=fixture["task_id"],
                    project_id=fixture["project_id"],
                    dataset_item_id=fixture["item_a"],
                    display_id=f"TU-{suffix}",
                    file_name="a.jpg",
                    file_path=f"{suffix}/a.jpg",
                    file_type="image",
                ),
                Task(
                    id=fixture["other_task_id"],
                    project_id=fixture["other_project_id"],
                    dataset_item_id=fixture["item_b"],
                    display_id=f"TX-{suffix}",
                    file_name="b.jpg",
                    file_path=f"{suffix}/b.jpg",
                    file_type="image",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                ImagePyramidAsset(
                    id=fixture["task_asset_id"],
                    task_id=fixture["task_id"],
                    profile_version="task-v1",
                ),
                ImagePyramidAsset(
                    id=fixture["other_task_asset_id"],
                    task_id=fixture["other_task_id"],
                    profile_version="other-task-v1",
                ),
                ImagePyramidAsset(
                    id=fixture["item_asset_id"],
                    dataset_item_id=fixture["item_a"],
                    profile_version="item-v1",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            ImagePyramidGeneration(
                asset_id=asset_id,
                generation=1,
                source_identity=f"fixture:{asset_id}",
                status="ready",
                normalization_version="test-v1",
            )
            for asset_id in (
                fixture["task_asset_id"],
                fixture["other_task_asset_id"],
                fixture["item_asset_id"],
            )
        )
        session.add_all(
            [
                BugReport(
                    id=fixture["report_target_id"],
                    display_id=f"BRU-{suffix}",
                    reporter_id=fixture["user_id"],
                    route="/project",
                    user_role="super_admin",
                    project_id=fixture["project_id"],
                    task_id=fixture["task_id"],
                    title="Target project task report",
                    description="Keep the report while detaching only its task.",
                ),
                BugReport(
                    id=fixture["report_unrelated_id"],
                    display_id=f"BRX-{suffix}",
                    reporter_id=fixture["user_id"],
                    route="/other",
                    user_role="super_admin",
                    project_id=fixture["other_project_id"],
                    task_id=fixture["other_task_id"],
                    title="Unrelated task report",
                    description="Leave unrelated task references untouched.",
                ),
            ]
        )
        await session.flush()
        session.add(
            BugComment(
                id=fixture["comment_id"],
                bug_report_id=fixture["report_target_id"],
                author_id=fixture["user_id"],
                body="Comment retained after task unlink.",
            )
        )
        await session.commit()
    return fixture


async def _cleanup(test_engine, fixture: dict[str, uuid.UUID]) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(
            delete(AssetStorageDeletion).where(
                AssetStorageDeletion.operation_id.in_(
                    [fixture["operation_id"], fixture["other_operation_id"]]
                )
            )
        )
        await session.execute(
            delete(BugComment).where(
                BugComment.bug_report_id.in_(
                    [
                        fixture["report_target_id"],
                        fixture["report_unrelated_id"],
                    ]
                )
            )
        )
        await session.execute(
            delete(BugReport).where(BugReport.reporter_id == fixture["user_id"])
        )
        await session.execute(
            delete(ImagePyramidAsset).where(
                ImagePyramidAsset.id.in_(
                    [
                        fixture["task_asset_id"],
                        fixture["other_task_asset_id"],
                        fixture["item_asset_id"],
                    ]
                )
            )
        )
        await session.execute(
            delete(Task).where(
                Task.id.in_([fixture["task_id"], fixture["other_task_id"]])
            )
        )
        await session.execute(
            delete(ProjectDataset).where(
                ProjectDataset.id.in_(
                    [fixture["association_id"], fixture["other_association_id"]]
                )
            )
        )
        await session.execute(
            delete(DatasetItem).where(
                DatasetItem.id.in_(
                    [
                        fixture["item_a"],
                        fixture["item_b"],
                        fixture["item_c"],
                    ]
                )
            )
        )
        await session.execute(
            delete(Scene).where(Scene.id.in_([fixture["scene_a"], fixture["scene_b"]]))
        )
        await session.execute(
            delete(Project).where(
                Project.id.in_([fixture["project_id"], fixture["other_project_id"]])
            )
        )
        await session.execute(
            delete(Dataset).where(Dataset.id == fixture["dataset_id"])
        )
        await session.execute(delete(User).where(User.id == fixture["user_id"]))


def _sqlstate(error: DBAPIError) -> str | None:
    pending = [error.orig]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state:
            return state
        pending.extend(
            nested
            for nested in (
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            )
            if nested is not None
        )
    return None


async def _rows(session, statement, **parameters):
    return (await session.execute(statement, parameters)).mappings().all()


async def test_frozen_artifact_and_asset_storage_model_match_contract():
    import json
    from pathlib import Path

    root = Path(__file__).parents[1] / "alembic/sql"
    schema = json.loads(
        (root / "0203_dataset_unlink_assets.schema.json").read_text(encoding="utf-8")
    )
    artifact = (root / "0203_dataset_unlink_assets.sql").read_text(encoding="utf-8")
    expected = {
        "aap_assets_v1.dataset_item_references(uuid, uuid)",
        "aap_assets_v1.dataset_scene_references(uuid)",
        "aap_assets_v1.remove_dataset_association(uuid, uuid, uuid)",
        "aap_assets_v1.prepare_dataset_unlink_cleanup(uuid, uuid, uuid, text)",
        "aap_assets_v1.pending_storage_deletions(uuid, integer)",
        "aap_assets_v1.complete_storage_deletion(uuid, uuid)",
        "aap_diagnostics_v1.detach_dataset_task_references(uuid, uuid)",
    }
    assert set(schema["x-postgresql-functions"]) == expected
    assert schema["x-postgresql-dependencies"] == [
        "aap_labeling_v1.dataset_task_references(uuid, uuid)"
    ]
    assert artifact.count("CREATE FUNCTION") == len(expected)
    assert artifact.count("REVOKE ALL ON FUNCTION") == len(expected)
    assert artifact.count("SECURITY DEFINER") == len(expected)
    assert artifact.count("SET search_path = pg_catalog, pg_temp") == len(expected)
    assert "FOREIGN KEY" not in artifact.upper()
    assert set(AssetStorageDeletion.__table__.foreign_keys) == set()
    assert "CREATE TABLE public.asset_storage_deletions" in artifact


async def test_dataset_item_and_scene_reference_functions_are_scoped_and_nullable(
    test_engine,
):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            all_items = (
                (
                    await session.execute(
                        _ITEM_REFERENCES,
                        {"dataset_id": fixture["dataset_id"], "item_id": None},
                    )
                )
                .scalars()
                .all()
            )
            one_item = (
                (
                    await session.execute(
                        _ITEM_REFERENCES,
                        {
                            "dataset_id": fixture["dataset_id"],
                            "item_id": fixture["item_a"],
                        },
                    )
                )
                .scalars()
                .all()
            )
            other_dataset_items = (
                (
                    await session.execute(
                        _ITEM_REFERENCES,
                        {"dataset_id": uuid.uuid4(), "item_id": None},
                    )
                )
                .scalars()
                .all()
            )
            scenes = (
                (
                    await session.execute(
                        _SCENE_REFERENCES, {"dataset_id": fixture["dataset_id"]}
                    )
                )
                .scalars()
                .all()
            )
            assert all_items == sorted(
                [fixture["item_a"], fixture["item_b"], fixture["item_c"]]
            )
            assert one_item == [fixture["item_a"]]
            assert other_dataset_items == []
            assert scenes == sorted([fixture["scene_a"], fixture["scene_b"]])
    finally:
        await _cleanup(test_engine, fixture)


async def test_unlink_cleanup_queues_only_task_pyramids_and_completes_idempotently(
    test_engine,
):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            assert (
                await session.scalar(
                    _PREPARE_CLEANUP,
                    {
                        "operation_id": fixture["operation_id"],
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["project_id"],
                        "bucket": "media-cache-test",
                    },
                )
                == 1
            )
            rows = await _rows(
                session,
                _PENDING_DELETIONS,
                operation_id=fixture["operation_id"],
                limit=20,
            )
            assert rows == [
                {
                    "operation_id": fixture["operation_id"],
                    "asset_id": fixture["task_asset_id"],
                    "bucket": "media-cache-test",
                    "prefix": f"image-pyramids/{fixture['task_asset_id']}/",
                }
            ]
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id"
                    ),
                    {"id": fixture["task_asset_id"]},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.image_pyramid_generations WHERE asset_id=:id"
                    ),
                    {"id": fixture["task_asset_id"]},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id"
                    ),
                    {"id": fixture["other_task_asset_id"]},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id"
                    ),
                    {"id": fixture["item_asset_id"]},
                )
                == 1
            )
            assert (
                await session.scalar(
                    _PREPARE_CLEANUP,
                    {
                        "operation_id": fixture["other_operation_id"],
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["other_project_id"],
                        "bucket": "media-cache-test",
                    },
                )
                == 1
            )
            await session.commit()

        async with maker() as session:
            global_rows = await _rows(
                session,
                _PENDING_DELETIONS,
                operation_id=None,
                limit=20,
            )
            assert {row["operation_id"] for row in global_rows} == {
                fixture["operation_id"],
                fixture["other_operation_id"],
            }
            assert (
                await session.scalar(
                    _COMPLETE_DELETION,
                    {
                        "operation_id": fixture["operation_id"],
                        "asset_id": fixture["task_asset_id"],
                    },
                )
                is True
            )
            assert (
                await _rows(
                    session,
                    _PENDING_DELETIONS,
                    operation_id=fixture["operation_id"],
                    limit=20,
                )
                == []
            )
            remaining = await _rows(
                session,
                _PENDING_DELETIONS,
                operation_id=None,
                limit=20,
            )
            assert len(remaining) == 1
            assert remaining[0]["operation_id"] == fixture["other_operation_id"]
            assert (
                await session.scalar(
                    _COMPLETE_DELETION,
                    {
                        "operation_id": fixture["other_operation_id"],
                        "asset_id": fixture["other_task_asset_id"],
                    },
                )
                is True
            )
            completed_at = await session.scalar(
                text(
                    "SELECT completed_at FROM public.asset_storage_deletions "
                    "WHERE operation_id=:operation_id AND asset_id=:asset_id"
                ),
                {
                    "operation_id": fixture["operation_id"],
                    "asset_id": fixture["task_asset_id"],
                },
            )
            assert completed_at is not None
            assert (
                await session.scalar(
                    _COMPLETE_DELETION,
                    {
                        "operation_id": fixture["operation_id"],
                        "asset_id": fixture["task_asset_id"],
                    },
                )
                is True
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT completed_at FROM public.asset_storage_deletions "
                        "WHERE operation_id=:operation_id AND asset_id=:asset_id"
                    ),
                    {
                        "operation_id": fixture["operation_id"],
                        "asset_id": fixture["task_asset_id"],
                    },
                )
                == completed_at
            )
            assert (
                await session.scalar(
                    _COMPLETE_DELETION,
                    {
                        "operation_id": uuid.uuid4(),
                        "asset_id": fixture["task_asset_id"],
                    },
                )
                is False
            )
            assert (
                await _rows(
                    session,
                    _PENDING_DELETIONS,
                    operation_id=fixture["operation_id"],
                    limit=20,
                )
                == []
            )
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.parametrize("bucket", ["../outside", "ab", "UPPERCASE", "127.0.0.1"])
async def test_prepare_rejects_invalid_bucket_without_deleting_metadata(
    test_engine, bucket
):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            with pytest.raises(DBAPIError) as invalid:
                await session.scalar(
                    _PREPARE_CLEANUP,
                    {
                        "operation_id": fixture["operation_id"],
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["project_id"],
                        "bucket": bucket,
                    },
                )
            assert _sqlstate(invalid.value) == "22023"
            await session.rollback()
        async with maker() as verify:
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id"
                    ),
                    {"id": fixture["task_asset_id"]},
                )
                == 1
            )
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_storage_deletions "
                        "WHERE operation_id=:id"
                    ),
                    {"id": fixture["operation_id"]},
                )
                == 0
            )
    finally:
        await _cleanup(test_engine, fixture)


async def test_outbox_insert_and_task_metadata_delete_follow_caller_rollback(
    test_engine,
):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as caller:
            await caller.begin()
            assert (
                await caller.scalar(
                    _PREPARE_CLEANUP,
                    {
                        "operation_id": fixture["operation_id"],
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["project_id"],
                        "bucket": "media-cache-test",
                    },
                )
                == 1
            )
            assert (
                await caller.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_storage_deletions "
                        "WHERE operation_id=:id"
                    ),
                    {"id": fixture["operation_id"]},
                )
                == 1
            )
            assert (
                await caller.scalar(
                    text(
                        "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id"
                    ),
                    {"id": fixture["task_asset_id"]},
                )
                == 0
            )
            await caller.rollback()
        async with maker() as verify:
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM public.asset_storage_deletions "
                        "WHERE operation_id=:id"
                    ),
                    {"id": fixture["operation_id"]},
                )
                == 0
            )
            assert (
                await verify.scalar(
                    text(
                        "SELECT count(*) FROM public.image_pyramid_assets WHERE id=:id"
                    ),
                    {"id": fixture["task_asset_id"]},
                )
                == 1
            )
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.parametrize("limit", [0, 1001])
async def test_pending_deletion_limit_is_bounded(test_engine, limit):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        with pytest.raises(DBAPIError) as invalid:
            await session.execute(
                _PENDING_DELETIONS,
                {"operation_id": None, "limit": limit},
            )
        assert _sqlstate(invalid.value) == "22023"
        await session.rollback()


async def test_association_removal_is_generation_checked(test_engine):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            assert (
                await session.scalar(
                    _REMOVE_ASSOCIATION,
                    {
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["other_project_id"],
                        "association_id": fixture["association_id"],
                    },
                )
                is False
            )
            assert (
                await session.scalar(
                    _REMOVE_ASSOCIATION,
                    {
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["project_id"],
                        "association_id": uuid.uuid4(),
                    },
                )
                is False
            )
            assert (
                await session.scalar(
                    _REMOVE_ASSOCIATION,
                    {
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["project_id"],
                        "association_id": fixture["association_id"],
                    },
                )
                is True
            )
            assert (
                await session.scalar(
                    _REMOVE_ASSOCIATION,
                    {
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["project_id"],
                        "association_id": fixture["association_id"],
                    },
                )
                is False
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM public.project_datasets WHERE id=:id"),
                    {"id": fixture["other_association_id"]},
                )
                == 1
            )
            await session.commit()
    finally:
        await _cleanup(test_engine, fixture)


async def test_diagnostics_detaches_only_matching_tasks_and_preserves_reports_comments(
    test_engine,
):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            assert (
                await session.scalar(
                    _DETACH_REPORTS,
                    {
                        "dataset_id": fixture["dataset_id"],
                        "project_id": fixture["project_id"],
                    },
                )
                == 1
            )
            await session.commit()
        async with maker() as verify:
            target = await verify.execute(
                text(
                    "SELECT project_id, task_id, title, description "
                    "FROM public.bug_reports WHERE id=:id"
                ),
                {"id": fixture["report_target_id"]},
            )
            target = target.mappings().one()
            assert target["project_id"] == fixture["project_id"]
            assert target["task_id"] is None
            assert target["title"] == "Target project task report"
            assert (
                target["description"]
                == "Keep the report while detaching only its task."
            )
            unrelated = (
                (
                    await verify.execute(
                        text(
                            "SELECT project_id, task_id FROM public.bug_reports WHERE id=:id"
                        ),
                        {"id": fixture["report_unrelated_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert unrelated == {
                "project_id": fixture["other_project_id"],
                "task_id": fixture["other_task_id"],
            }
            assert (
                await verify.scalar(
                    text("SELECT count(*) FROM public.bug_comments WHERE id=:id"),
                    {"id": fixture["comment_id"]},
                )
                == 1
            )
    finally:
        await _cleanup(test_engine, fixture)
