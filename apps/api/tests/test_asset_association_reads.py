from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.user import User

pytestmark = pytest.mark.asyncio

_LIST_DATASET_PROJECTS = text(
    "SELECT aap_assets_v1.list_dataset_projects("
    "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid))"
)
_LIST_PROJECT_DATASETS = text(
    "SELECT aap_assets_v1.list_project_datasets("
    "CAST(:actor_id AS uuid), CAST(:project_id AS uuid))"
)
_ROLE_ACCEPTANCE = os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
_CATALOG_URL = "AAP_MODULE_DATA_ASSET_CATALOG_URL"
_CATALOG_FUNCTIONS = {
    "aap_assets_v1.list_catalog_datasets(uuid,text,text,boolean,integer,bigint)",
    "aap_assets_v1.get_catalog_dataset(uuid,uuid)",
    "aap_assets_v1.list_catalog_items(uuid,uuid,integer,bigint)",
    "aap_assets_v1.create_catalog_dataset(uuid,uuid,jsonb)",
    "aap_assets_v1.update_catalog_dataset(uuid,uuid,uuid,jsonb)",
    "aap_assets_v1.list_dataset_projects(uuid,uuid)",
    "aap_assets_v1.list_project_datasets(uuid,uuid)",
}


def _success(reply: dict, result_key: str) -> list[dict]:
    assert set(reply) == {"schema", "ok", "data"}
    assert reply["schema"] == "aap-assets-associations/v1"
    assert reply["ok"] is True
    parent_key = "dataset_id" if result_key == "projects" else "project_id"
    assert set(reply["data"]) == {parent_key, result_key}
    assert str(uuid.UUID(reply["data"][parent_key])) == reply["data"][parent_key]
    return reply["data"][result_key]


def _failure(reply: dict, code: str) -> None:
    assert reply == {
        "schema": "aap-assets-associations/v1",
        "ok": False,
        "error": {"code": code},
    }


async def _seed_user(db_session, *, role: str, active: bool = True) -> uuid.UUID:
    user_id = uuid.uuid4()
    database = await db_session.scalar(text("SELECT current_database()"))
    if not database or not database.endswith("_test"):
        pytest.fail("refusing to seed Assets association facts outside a test database")
    db_session.add(
        User(
            id=user_id,
            email=f"association-read-{user_id.hex}@example.test",
            name="Association read fixture",
            password_hash="test-only",
            role=role,
            is_active=active,
        )
    )
    await db_session.flush()
    return user_id


async def _seed_project(
    db_session,
    *,
    owner_id: uuid.UUID,
    name: str,
    total_tasks: int = 0,
    completed_tasks: int = 0,
    review_tasks: int = 0,
    in_progress_tasks: int = 0,
    batch_summary: dict | None = None,
    tool_bindings: dict | None = None,
) -> Project:
    project_id = uuid.uuid4()
    project = Project(
        id=project_id,
        display_id=f"AR{project_id.hex[:12]}",
        name=name,
        type_label="Image detection",
        type_key="image-det",
        data_type="image",
        owner_id=owner_id,
        total_tasks=total_tasks,
        completed_tasks=completed_tasks,
        review_tasks=review_tasks,
        in_progress_tasks=in_progress_tasks,
        batch_summary=batch_summary or {},
        tool_bindings=tool_bindings or {},
    )
    db_session.add(project)
    await db_session.flush()
    return project


async def _seed_dataset(db_session, *, created_by: uuid.UUID, name: str) -> Dataset:
    dataset_id = uuid.uuid4()
    dataset = Dataset(
        id=dataset_id,
        display_id=f"AD{dataset_id.hex[:12]}",
        name=name,
        data_type="image",
        file_count=0,
        created_by=created_by,
    )
    db_session.add(dataset)
    await db_session.flush()
    return dataset


async def _seed_item(db_session, *, dataset_id: uuid.UUID, name: str) -> DatasetItem:
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset_id,
        file_name=f"{name}.png",
        file_path=f"associations/{name}.png",
        file_type="image",
        file_size=128,
    )
    db_session.add(item)
    await db_session.flush()
    return item


async def _seed_task(
    db_session,
    *,
    project_id: uuid.UUID,
    item_id: uuid.UUID | None,
    status: str,
) -> Task:
    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        project_id=project_id,
        dataset_item_id=item_id,
        display_id=f"AT{task_id.hex[:12]}",
        file_name="task.png",
        file_path="associations/task.png",
        file_type="image",
        status=status,
    )
    db_session.add(task)
    await db_session.flush()
    return task


async def _add_member(
    db_session,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    assigned_by: uuid.UUID,
    role: str,
) -> None:
    db_session.add(
        ProjectMember(
            project_id=project_id,
            user_id=user_id,
            assigned_by=assigned_by,
            role=role,
        )
    )
    await db_session.flush()


async def _list_project_datasets(
    db_session, actor_id: uuid.UUID, project_id: uuid.UUID
):
    return await db_session.scalar(
        _LIST_PROJECT_DATASETS,
        {"actor_id": actor_id, "project_id": project_id},
    )


async def test_association_schema_freezes_both_read_capabilities():
    root = Path(__file__).parents[1] / "alembic" / "sql"
    schema = json.loads((root / "0377_asset_association_reads.schema.json").read_text())
    assert schema["$id"] == "urn:aap:assets-associations:v1"
    assert set(schema["x-postgresql-functions"]) == {
        "aap_assets_v1.list_dataset_projects(uuid, uuid)",
        "aap_assets_v1.list_project_datasets(uuid, uuid)",
    }
    workspace = schema["$defs"]["workspaceProjectSnapshot"]
    assert workspace["additionalProperties"] is False
    assert "ml_backend_pool_id" not in workspace["properties"]
    linked = schema["$defs"]["linkedProjectSnapshot"]
    assert linked["additionalProperties"] is False
    assert {
        "total_tasks",
        "completed_tasks",
        "review_tasks",
        "in_progress_tasks",
        "batch_summary",
    } <= set(linked["properties"])
    dataset = schema["$defs"]["projectDatasetSummary"]
    assert dataset["additionalProperties"] is False
    assert dataset["properties"]["items_count"]["minimum"] == 0
    assert dataset["properties"]["tasks_in_project"]["minimum"] == 0


async def test_dataset_projects_keep_global_read_policy_raw_workspace_and_stored_counters(
    db_session,
):
    actor_id = await _seed_user(db_session, role="future_platform_role")
    owner_id = await _seed_user(db_session, role="project_admin")
    dataset = await _seed_dataset(db_session, created_by=owner_id, name="shared")
    older_project = await _seed_project(
        db_session,
        owner_id=owner_id,
        name="older association",
        total_tasks=91,
        completed_tasks=13,
        review_tasks=7,
        in_progress_tasks=4,
        batch_summary={"total": 8, "assigned": 3, "in_review": 2},
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [{"name": "vehicle", "color": "#123456", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    older_project.label_config = {"opaque_extension": {"kept": True}}
    await db_session.flush()
    newer_project = await _seed_project(
        db_session,
        owner_id=owner_id,
        name="newer association",
        total_tasks=92,
        completed_tasks=14,
        review_tasks=8,
        in_progress_tasks=5,
        batch_summary={"total": 9, "assigned": 4, "in_review": 1},
    )
    older_link_id, newer_link_id = sorted((uuid.uuid4(), uuid.uuid4()))
    older_linked_at = datetime(2026, 1, 1, tzinfo=UTC)
    newer_linked_at = datetime(2026, 2, 1, tzinfo=UTC)
    db_session.add_all(
        [
            ProjectDataset(
                id=older_link_id,
                project_id=older_project.id,
                dataset_id=dataset.id,
                created_at=older_linked_at,
            ),
            ProjectDataset(
                id=newer_link_id,
                project_id=newer_project.id,
                dataset_id=dataset.id,
                created_at=newer_linked_at,
            ),
        ]
    )
    await db_session.flush()

    reply = await db_session.scalar(
        _LIST_DATASET_PROJECTS,
        {"actor_id": actor_id, "dataset_id": dataset.id},
    )
    projects = _success(reply, "projects")
    assert [project["id"] for project in projects] == [
        str(newer_project.id),
        str(older_project.id),
    ]
    older = projects[1]
    schema = json.loads(
        (
            Path(__file__).parents[1]
            / "alembic"
            / "sql"
            / "0377_asset_association_reads.schema.json"
        ).read_text()
    )
    expected_project_fields = set(
        schema["$defs"]["workspaceProjectSnapshot"]["required"]
    ) | {
        "total_tasks",
        "completed_tasks",
        "review_tasks",
        "in_progress_tasks",
        "batch_summary",
    }
    assert set(older) == expected_project_fields
    assert older["label_config"]["opaque_extension"] == {"kept": True}
    assert older["total_tasks"] == 91
    assert older["completed_tasks"] == 13
    assert older["review_tasks"] == 7
    assert older["in_progress_tasks"] == 4
    assert older["batch_summary"] == {"total": 8, "assigned": 3, "in_review": 2}
    assert "ml_backend_pool_id" not in older
    assert "ml_backend_id" not in older
    assert "owner_name" not in older
    assert "owner_avatar_ref" not in older
    assert "member_count" not in older
    assert "my_project_role" not in older
    assert "ai_completed_tasks" not in older
    assert "capability_warnings" not in older

    missing_dataset = await db_session.scalar(
        _LIST_DATASET_PROJECTS,
        {"actor_id": actor_id, "dataset_id": uuid.uuid4()},
    )
    assert _success(missing_dataset, "projects") == []
    invalid_request = await db_session.scalar(
        _LIST_DATASET_PROJECTS,
        {"actor_id": actor_id, "dataset_id": None},
    )
    _failure(invalid_request, "invalid_request")

    inactive_actor = await _seed_user(db_session, role="employee", active=False)
    inactive = await db_session.scalar(
        _LIST_DATASET_PROJECTS,
        {"actor_id": inactive_actor, "dataset_id": dataset.id},
    )
    _failure(inactive, "account_not_active")


async def test_project_datasets_count_only_direct_primary_task_sources(db_session):
    owner_id = await _seed_user(db_session, role="project_admin")
    actor_id = await _seed_user(db_session, role="employee")
    project = await _seed_project(db_session, owner_id=owner_id, name="visible")
    await _add_member(
        db_session,
        project_id=project.id,
        user_id=actor_id,
        assigned_by=owner_id,
        role="annotator",
    )

    first_dataset = await _seed_dataset(
        db_session, created_by=owner_id, name="first-linked"
    )
    second_dataset = await _seed_dataset(
        db_session, created_by=owner_id, name="second-linked"
    )
    first_item = await _seed_item(
        db_session, dataset_id=first_dataset.id, name="first-a"
    )
    second_item = await _seed_item(
        db_session, dataset_id=first_dataset.id, name="first-b"
    )
    await _seed_item(db_session, dataset_id=first_dataset.id, name="first-unused")
    second_dataset_item = await _seed_item(
        db_session, dataset_id=second_dataset.id, name="second-a"
    )
    association_ids = sorted((uuid.uuid4(), uuid.uuid4()))
    linked_at = datetime(2026, 4, 5, 6, 7, tzinfo=UTC)
    db_session.add_all(
        [
            ProjectDataset(
                id=association_ids[0],
                project_id=project.id,
                dataset_id=first_dataset.id,
                created_at=linked_at,
            ),
            ProjectDataset(
                id=association_ids[1],
                project_id=project.id,
                dataset_id=second_dataset.id,
                created_at=linked_at,
            ),
        ]
    )
    first_pending = await _seed_task(
        db_session,
        project_id=project.id,
        item_id=first_item.id,
        status="pending",
    )
    await _seed_task(
        db_session,
        project_id=project.id,
        item_id=first_item.id,
        status="completed",
    )
    await _seed_task(
        db_session,
        project_id=project.id,
        item_id=second_item.id,
        status="review",
    )
    await _seed_task(
        db_session,
        project_id=project.id,
        item_id=second_dataset_item.id,
        status="in_progress",
    )
    source_less = await _seed_task(
        db_session,
        project_id=project.id,
        item_id=None,
        status="completed",
    )
    db_session.add(
        TaskDatasetItemLink(
            task_id=source_less.id,
            dataset_item_id=first_item.id,
            role="camera_front",
        )
    )
    db_session.add(
        TaskDatasetItemLink(
            task_id=source_less.id,
            dataset_item_id=second_item.id,
            role="primary_lidar",
        )
    )
    other_project = await _seed_project(
        db_session, owner_id=owner_id, name="other project"
    )
    await _seed_task(
        db_session,
        project_id=other_project.id,
        item_id=first_item.id,
        status="completed",
    )
    await db_session.flush()

    reply = await _list_project_datasets(db_session, actor_id, project.id)
    datasets = _success(reply, "datasets")
    assert [row["id"] for row in datasets] == [
        str(second_dataset.id),
        str(first_dataset.id),
    ]
    by_id = {row["id"]: row for row in datasets}
    assert by_id[str(first_dataset.id)]["items_count"] == 3
    assert by_id[str(first_dataset.id)]["tasks_in_project"] == 3
    assert by_id[str(second_dataset.id)]["items_count"] == 1
    assert by_id[str(second_dataset.id)]["tasks_in_project"] == 1
    assert by_id[str(second_dataset.id)]["linked_at"].endswith("+00:00")
    assert set(by_id[str(first_dataset.id)]) == {
        "id",
        "display_id",
        "name",
        "data_type",
        "linked_at",
        "items_count",
        "tasks_in_project",
    }
    assert first_pending.dataset_item_id == first_item.id

    empty_project = await _seed_project(
        db_session, owner_id=owner_id, name="visible but empty"
    )
    await _add_member(
        db_session,
        project_id=empty_project.id,
        user_id=actor_id,
        assigned_by=owner_id,
        role="viewer",
    )
    empty = await _list_project_datasets(db_session, actor_id, empty_project.id)
    assert _success(empty, "datasets") == []


async def test_project_dataset_visibility_preserves_current_error_and_role_policy(
    db_session,
):
    owner_id = await _seed_user(db_session, role="project_admin")
    project = await _seed_project(db_session, owner_id=owner_id, name="owner visible")
    super_admin_id = await _seed_user(db_session, role="super_admin")
    owner_read = await _list_project_datasets(db_session, owner_id, project.id)
    assert _success(owner_read, "datasets") == []
    admin_read = await _list_project_datasets(db_session, super_admin_id, project.id)
    assert _success(admin_read, "datasets") == []

    member_manager_id = await _seed_user(db_session, role="project_admin")
    await _add_member(
        db_session,
        project_id=project.id,
        user_id=member_manager_id,
        assigned_by=owner_id,
        role="reviewer",
    )
    manager_member_read = await _list_project_datasets(
        db_session, member_manager_id, project.id
    )
    assert _success(manager_member_read, "datasets") == []

    hidden_id = await _seed_user(db_session, role="employee")
    hidden = await _list_project_datasets(db_session, hidden_id, project.id)
    _failure(hidden, "project_missing")
    missing = await _list_project_datasets(db_session, super_admin_id, uuid.uuid4())
    _failure(missing, "project_missing")

    invalid_account_id = await _seed_user(db_session, role="future_platform_role")
    invalid_account = await _list_project_datasets(
        db_session, invalid_account_id, project.id
    )
    _failure(invalid_account, "invalid_account_role")
    invalid_account_missing_project = await _list_project_datasets(
        db_session, invalid_account_id, uuid.uuid4()
    )
    _failure(invalid_account_missing_project, "project_missing")

    invalid_member_id = await _seed_user(db_session, role="employee")
    await _add_member(
        db_session,
        project_id=project.id,
        user_id=invalid_member_id,
        assigned_by=owner_id,
        role="legacy_project_role",
    )
    invalid_member = await _list_project_datasets(
        db_session, invalid_member_id, project.id
    )
    _failure(invalid_member, "invalid_project_role")

    incompatible_viewer_id = await _seed_user(db_session, role="viewer")
    await _add_member(
        db_session,
        project_id=project.id,
        user_id=incompatible_viewer_id,
        assigned_by=owner_id,
        role="annotator",
    )
    incompatible = await _list_project_datasets(
        db_session, incompatible_viewer_id, project.id
    )
    _failure(incompatible, "incompatible_project_role")

    inactive_id = await _seed_user(db_session, role="employee", active=False)
    inactive = await _list_project_datasets(db_session, inactive_id, uuid.uuid4())
    _failure(inactive, "account_not_active")
    invalid_request = await _list_project_datasets(db_session, owner_id, None)
    _failure(invalid_request, "invalid_request")


@pytest.mark.skipif(
    not _ROLE_ACCEPTANCE,
    reason="requires the disposable module-data boundary verifier",
)
async def test_catalog_login_has_only_seven_public_reads_and_no_owner_relations():
    raw_url = os.environ.get(_CATALOG_URL)
    if not raw_url:
        pytest.fail(f"the module-data verifier did not provide {_CATALOG_URL}")
    url = make_url(raw_url)
    if url.username != "asset_catalog_client" or not url.database.endswith("_test"):
        pytest.fail("catalog LOGIN URL does not target the owned test database")
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as catalog:
            functions = await catalog.execute(
                text(
                    "SELECT proc.oid::regprocedure::text "
                    "FROM pg_catalog.pg_proc AS proc "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = proc.pronamespace "
                    "WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$' "
                    "AND pg_catalog.has_function_privilege(current_user, proc.oid, 'EXECUTE')"
                )
            )
            assert {row[0].replace(", ", ",") for row in functions} == {
                signature.replace(", ", ",") for signature in _CATALOG_FUNCTIONS
            }
            direct_relations = await catalog.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_class AS relation "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname NOT IN ('pg_catalog','information_schema') "
                    "AND relation.relkind IN ('r','p','v','m','f') "
                    "AND (pg_catalog.has_table_privilege(current_user, relation.oid, 'SELECT') "
                    "OR pg_catalog.has_any_column_privilege(current_user, relation.oid, 'SELECT') "
                    "OR pg_catalog.has_table_privilege(current_user, relation.oid, 'INSERT') "
                    "OR pg_catalog.has_any_column_privilege(current_user, relation.oid, 'INSERT') "
                    "OR pg_catalog.has_table_privilege(current_user, relation.oid, 'UPDATE') "
                    "OR pg_catalog.has_any_column_privilege(current_user, relation.oid, 'UPDATE') "
                    "OR pg_catalog.has_table_privilege(current_user, relation.oid, 'DELETE'))"
                    ")"
                )
            )
            assert direct_relations is False
            for schema in (
                "aap_identity_v1",
                "aap_workspace_v1",
                "aap_labeling_v1",
                "aap_jobs_v1",
                "aap_learning_v1",
                "aap_inference_v1",
                "aap_insights_v1",
                "aap_diagnostics_v1",
                "aap_export_v1",
                "aap_audit_v1",
            ):
                assert not await catalog.scalar(
                    text(
                        "SELECT pg_catalog.has_schema_privilege(current_user, :schema, 'USAGE')"
                    ),
                    {"schema": schema},
                )
            await catalog.rollback()
    finally:
        await engine.dispose()
