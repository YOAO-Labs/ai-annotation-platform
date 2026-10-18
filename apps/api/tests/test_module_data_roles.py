"""Opt-in PostgreSQL acceptance for the split module data capabilities.

These tests use real LOGIN roles. They are skipped unless the isolated verifier
sets AAP_MODULE_DATA_ROLE_ACCEPTANCE=1 and supplies its fixture URLs.
"""

from __future__ import annotations

import json
import os
import subprocess
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]


def _role_url(name: str, expected_user: str) -> str:
    url = os.environ.get(name)
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not url or not test_url:
        pytest.fail("the module-data verifier did not supply its fixture URLs")
    parsed, test_parsed = make_url(url), make_url(test_url)
    if (
        parsed.drivername != "postgresql+asyncpg"
        or parsed.username != expected_user
        or parsed.host != "127.0.0.1"
        or parsed.port != test_parsed.port
        or parsed.database != test_parsed.database
    ):
        pytest.fail("the module-data role URL does not match the isolated test DB")
    return url


def _engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )


@pytest_asyncio.fixture
async def append_engine() -> AsyncEngine:
    engine = _engine(_role_url("AAP_MODULE_DATA_APPEND_URL", "append_client"))
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT current_user")) == "append_client"
            )
            assert (
                await connection.scalar(text("SELECT session_user")) == "append_client"
            )
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def legacy_runtime_engine() -> AsyncEngine:
    engine = _engine(_role_url("AAP_MODULE_DATA_LEGACY_RUNTIME_URL", "legacy_runtime"))
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT current_user")) == "legacy_runtime"
            )
        yield engine
    finally:
        await engine.dispose()


async def _seed_case(test_engine: AsyncEngine, item_count: int) -> dict[str, object]:
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.db.models.project import Project
    from app.db.models.user import User

    suffix = uuid4().hex
    case = {
        "user_id": uuid4(),
        "project_id": uuid4(),
        "dataset_id": uuid4(),
        "association_id": uuid4(),
        "item_ids": tuple(uuid4() for _ in range(item_count)),
        "suffix": suffix,
    }
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as owner:
        owner.add(
            User(
                id=case["user_id"],
                email=f"module-data-{suffix}@example.test",
                name="Module data acceptance",
                password_hash="unused-test-hash",
            )
        )
        await owner.flush()
        owner.add_all(
            [
                Dataset(
                    id=case["dataset_id"],
                    display_id=f"D-MOD-{suffix[:12]}",
                    name="Module data acceptance dataset",
                    data_type="image",
                    created_by=case["user_id"],
                ),
                Project(
                    id=case["project_id"],
                    display_id=f"P-MOD-{suffix[:12]}",
                    name="Module data acceptance project",
                    type_label="图像-检测",
                    type_key="image-det",
                    owner_id=case["user_id"],
                    total_tasks=0,
                ),
            ]
        )
        await owner.flush()
        owner.add(
            ProjectDataset(
                id=case["association_id"],
                project_id=case["project_id"],
                dataset_id=case["dataset_id"],
            )
        )
        owner.add_all(
            [
                DatasetItem(
                    id=item_id,
                    dataset_id=case["dataset_id"],
                    file_name=f"asset-{index}.jpg",
                    file_path=f"module-data/{suffix}/{index}.jpg",
                    file_type="image",
                )
                for index, item_id in enumerate(case["item_ids"])
            ]
        )
        await owner.commit()
    return case


async def _cleanup_case(test_engine: AsyncEngine, case: dict[str, object]) -> None:
    async with test_engine.begin() as connection:
        for statement in (
            "DELETE FROM public.tasks WHERE project_id = :project_id",
            "DELETE FROM public.task_append_operations WHERE dataset_id = :dataset_id",
            "DELETE FROM public.project_datasets WHERE id = :association_id",
            "DELETE FROM public.dataset_items WHERE dataset_id = :dataset_id",
            "DELETE FROM public.datasets WHERE id = :dataset_id",
            "DELETE FROM public.projects WHERE id = :project_id",
            "DELETE FROM public.users WHERE id = :user_id",
        ):
            await connection.execute(
                text(statement),
                {
                    "project_id": case["project_id"],
                    "dataset_id": case["dataset_id"],
                    "association_id": case["association_id"],
                    "user_id": case["user_id"],
                },
            )


async def _append(
    connection,
    dataset_id: UUID,
    item_ids: tuple[UUID, ...],
    operation_id: UUID,
) -> dict:
    value = await connection.scalar(
        text(
            "SELECT aap_labeling_v1.append_dataset_items("
            "CAST(:dataset_id AS uuid), CAST(:item_ids AS uuid[]), "
            "CAST(:operation_id AS uuid))"
        ),
        {
            "dataset_id": dataset_id,
            "item_ids": list(item_ids),
            "operation_id": operation_id,
        },
    )
    return json.loads(value) if isinstance(value, str) else value


async def _owner_counts(
    test_engine: AsyncEngine, case: dict[str, object], item_id: UUID, operation_id: UUID
):
    async with test_engine.connect() as owner:
        task_count = await owner.scalar(
            text(
                "SELECT count(*) FROM public.tasks "
                "WHERE project_id = :project_id AND dataset_item_id = :item_id"
            ),
            {"project_id": case["project_id"], "item_id": item_id},
        )
        receipt_count = await owner.scalar(
            text(
                "SELECT count(*) FROM public.task_append_operations "
                "WHERE operation_id = :operation_id"
            ),
            {"operation_id": operation_id},
        )
        total_tasks = await owner.scalar(
            text("SELECT total_tasks FROM public.projects WHERE id = :project_id"),
            {"project_id": case["project_id"]},
        )
    return task_count, receipt_count, total_tasks


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
        pending.extend(
            nested
            for nested in (
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            )
            if nested is not None
        )
    return None


async def _assert_permission_denied(
    connection, statement: str, parameters=None
) -> None:
    with pytest.raises(DBAPIError) as error:
        await connection.execute(text(statement), parameters or {})
    assert _sqlstate(error.value) == "42501"
    await connection.rollback()


async def test_append_client_cannot_read_or_write_private_tables_or_escalate(
    append_engine: AsyncEngine, legacy_runtime_engine: AsyncEngine
):
    async with legacy_runtime_engine.connect() as legacy:
        assert await legacy.scalar(
            text(
                "SELECT has_any_column_privilege(current_user, 'public.projects', 'SELECT')"
            )
        )
        assert await legacy.scalar(
            text(
                "SELECT has_table_privilege(current_user, 'public.datasets', "
                "'SELECT,INSERT,UPDATE,DELETE')"
            )
        )
        assert (
            await legacy.scalar(
                text("SELECT aap_workspace_v1.lock_project_reference(:id)"),
                {"id": uuid4()},
            )
            is None
        )
        assert (
            await legacy.scalar(
                text("SELECT aap_assets_v1.lock_dataset_reference(:id)"),
                {"id": uuid4()},
            )
            is None
        )
        assert (await _append(legacy, uuid4(), (), uuid4()))["created"] == 0
        await legacy.rollback()

    relations = (
        "public.users",
        "public.project_members",
        "public.async_jobs",
        "public.projects",
        "public.datasets",
        "public.dataset_items",
        "public.project_datasets",
        "public.tasks",
        "public.task_append_operations",
        "public.task_creation_operations",
        "public.task_creation_chunks",
    )
    async with append_engine.connect() as connection:
        for relation in relations:
            assert not await connection.scalar(
                text(
                    "SELECT has_any_column_privilege(current_user, :relation, "
                    "'SELECT,INSERT,UPDATE,REFERENCES') OR "
                    "has_table_privilege(current_user, :relation, "
                    "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                ),
                {"relation": relation},
            )

        for statement, parameters in (
            ("SELECT id FROM public.dataset_items LIMIT 1", {}),
            (
                "INSERT INTO public.task_append_operations "
                "(operation_id, dataset_id, item_ids, result) "
                "VALUES (:operation_id, :dataset_id, ARRAY[]::uuid[], NULL)",
                {"operation_id": uuid4(), "dataset_id": uuid4()},
            ),
            ("UPDATE public.projects SET total_tasks = 0 WHERE false", {}),
            ("DELETE FROM public.tasks WHERE false", {}),
        ):
            await _assert_permission_denied(connection, statement, parameters)

        for role in (
            "identity_code",
            "workspace_code",
            "assets_code",
            "labeling_code",
            "jobs_code",
        ):
            await _assert_permission_denied(connection, f"SET ROLE {role}")

        for signature in (
            "aap_jobs_v1._task_creation_job_failure(text,text)",
            "aap_jobs_v1._task_creation_job_payload_valid(uuid,jsonb)",
            "aap_jobs_v1.update_task_creation_progress(uuid,uuid,bigint,bigint,bigint)",
            "aap_assets_v1.lock_task_creation_items(uuid,uuid[])",
        ):
            assert not await connection.scalar(
                text(
                    "SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"
                ),
                {"signature": signature},
            ), signature
        await _assert_permission_denied(
            connection,
            "SELECT aap_identity_v1.lock_active_actor(CAST(:id AS uuid))",
            {"id": uuid4()},
        )

        assert not await connection.scalar(
            text(
                "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
            )
        )
        assert not await connection.scalar(
            text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
        )
        await _assert_permission_denied(
            connection,
            "CREATE TABLE public.append_client_forbidden_probe (id integer)",
        )


async def test_append_receipt_replay_is_frozen_after_association_change(
    test_engine: AsyncEngine, append_engine: AsyncEngine
):
    case = await _seed_case(test_engine, item_count=1)
    item_id = case["item_ids"][0]
    operation_id = uuid4()
    try:
        async with append_engine.begin() as caller:
            first = await _append(caller, case["dataset_id"], (item_id,), operation_id)
        assert first["schema"] == "aap-task-append/v1"
        assert first["created"] == 1
        assert first["operation_id"] == str(operation_id)

        async with test_engine.begin() as owner:
            await owner.execute(
                text("DELETE FROM public.project_datasets WHERE id = :association_id"),
                {"association_id": case["association_id"]},
            )

        async with append_engine.begin() as caller:
            replay = await _append(caller, case["dataset_id"], (item_id,), operation_id)
        assert replay == first
        assert await _owner_counts(test_engine, case, item_id, operation_id) == (
            1,
            1,
            1,
        )
    finally:
        await _cleanup_case(test_engine, case)


async def test_append_client_rollback_and_native_psql_share_caller_transaction(
    test_engine: AsyncEngine, append_engine: AsyncEngine
):
    case = await _seed_case(test_engine, item_count=2)
    first_item, second_item = case["item_ids"]
    sqlalchemy_operation, psql_operation = uuid4(), uuid4()
    try:
        async with append_engine.connect() as caller:
            transaction = await caller.begin()
            receipt = await _append(
                caller,
                case["dataset_id"],
                (first_item,),
                sqlalchemy_operation,
            )
            assert receipt["created"] == 1
            await transaction.rollback()
        assert await _owner_counts(
            test_engine, case, first_item, sqlalchemy_operation
        ) == (0, 0, 0)

        append_url = make_url(os.environ["AAP_MODULE_DATA_APPEND_URL"])
        container_id = os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
        if not container_id:
            pytest.fail(
                "the module-data verifier did not supply its owned container id"
            )
        database = append_url.database
        native_sql = """\
\\set ON_ERROR_STOP on
BEGIN;
SELECT current_user || '|' || session_user || '|' || current_database();
SELECT aap_labeling_v1.append_dataset_items(
    :'dataset_id'::uuid,
    ARRAY[:'item_id'::uuid],
    :'operation_id'::uuid
);
ROLLBACK;
"""
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-e",
                f"PGPASSWORD={append_url.password}",
                container_id,
                "psql",
                "-X",
                "-q",
                "-A",
                "-t",
                "-h",
                "127.0.0.1",
                "-p",
                "5432",
                "-U",
                "append_client",
                "-d",
                database,
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"dataset_id={case['dataset_id']}",
                "-v",
                f"item_id={second_item}",
                "-v",
                f"operation_id={psql_operation}",
                "-f",
                "-",
            ],
            input=native_sql,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, "native psql append-client probe failed"
        lines = result.stdout.splitlines()
        assert lines[0] == f"append_client|append_client|{database}"
        native_receipt = json.loads(lines[1])
        assert native_receipt["created"] == 1
        assert native_receipt["operation_id"] == str(psql_operation)
        assert await _owner_counts(test_engine, case, second_item, psql_operation) == (
            0,
            0,
            0,
        )
    finally:
        await _cleanup_case(test_engine, case)


async def test_append_function_ignores_caller_temporary_table_names(
    test_engine: AsyncEngine, append_engine: AsyncEngine
):
    case = await _seed_case(test_engine, item_count=1)
    item_id = case["item_ids"][0]
    operation_id = uuid4()
    try:
        async with append_engine.connect() as caller:
            can_create_temp = await caller.scalar(
                text(
                    "SELECT has_database_privilege(current_user, current_database(), 'TEMP')"
                )
            )
            if not can_create_temp:
                pytest.skip(
                    "append_client has no TEMP privilege; spoofing is unavailable"
                )
            for statement in (
                "CREATE TEMP TABLE projects (id uuid, total_tasks integer) ON COMMIT DROP",
                "CREATE TEMP TABLE datasets (id uuid) ON COMMIT DROP",
                "CREATE TEMP TABLE project_datasets (id uuid, dataset_id uuid, project_id uuid) ON COMMIT DROP",
                "CREATE TEMP TABLE dataset_items (id uuid, dataset_id uuid, file_name text, file_path text, file_type text) ON COMMIT DROP",
                "CREATE TEMP TABLE tasks (id uuid, project_id uuid, dataset_item_id uuid, display_id text, file_name text, file_path text, file_type text, tags jsonb, status text, is_labeled boolean, overlap integer, total_annotations integer, total_predictions integer, assignee_is_override boolean, reviewer_is_override boolean, first_review_eligible boolean, annotation_contributor_ids uuid[], reopened_count integer, version integer) ON COMMIT DROP",
                "CREATE TEMP TABLE task_append_operations (operation_id uuid PRIMARY KEY, dataset_id uuid, item_ids uuid[], result jsonb, created_at timestamptz DEFAULT now()) ON COMMIT DROP",
            ):
                await caller.execute(text(statement))
            result = await _append(caller, case["dataset_id"], (item_id,), operation_id)
            await caller.commit()
            assert result["created"] == 1
            assert await _owner_counts(test_engine, case, item_id, operation_id) == (
                1,
                1,
                1,
            )
    finally:
        await _cleanup_case(test_engine, case)
