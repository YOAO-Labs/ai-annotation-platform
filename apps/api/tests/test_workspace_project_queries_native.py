"""Owned database acceptance for the fixed Workspace Project query owner."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned Workspace Project query role and disposable database",
    ),
]

_PROTOCOL = "aap-workspace-project-query/v1"
_CLIENT = "workspace_project_query_client"
_POLICY = "aap_workspace_project_query_v1.project_query_policy()"
_STEP = "aap_workspace_project_query_v1.project_query_step"
_ROW_COLUMNS = {
    "id",
    "organization_id",
    "display_id",
    "name",
    "type_label",
    "type_key",
    "data_type",
    "owner_id",
    "status",
    "ai_enabled",
    "ml_backend_pool_id",
    "default_variants",
    "preannotate_pipeline",
    "label_config",
    "tool_bindings",
    "sampling",
    "maximum_annotations",
    "show_overlap_first",
    "iou_dedup_threshold",
    "box_threshold",
    "text_threshold",
    "ai_interactive_enabled",
    "raster_mask_native_editing_enabled",
    "mask_qc_config",
    "point_cloud_quality_config",
    "rendering_config",
    "annotation_guide",
    "guide_assets",
    "guide_assets_revision",
    "model_version",
    "task_lock_ttl_seconds",
    "total_tasks",
    "completed_tasks",
    "review_tasks",
    "in_progress_tasks",
    "batch_summary",
    "video_sampling",
    "video_collaboration",
    "scene_mode",
    "prefer_same_scene_continuation",
    "scene_continuation_window_min",
    "due_date",
    "created_at",
    "updated_at",
}


def _json_object(value):
    return json.loads(value) if isinstance(value, str) else value


def _filters(**values) -> dict:
    return {
        "status": values.get("status"),
        "search": values.get("search"),
        "type_key": values.get("type_key"),
        "data_type": values.get("data_type"),
        "member_id": str(values["member_id"])
        if values.get("member_id") is not None
        else None,
        "project_role": values.get("project_role"),
        "created_from": values.get("created_from"),
        "created_to": values.get("created_to"),
    }


def _filter_text(filters: dict) -> str:
    return json.dumps(
        filters,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _filter_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _epoch_microseconds(value: datetime | None) -> str | None:
    if value is None:
        return None
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return str(
        delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    )


def _date_bound_parameters(filters: dict | None) -> dict:
    binds = _filter_binds(filters)
    return {
        "created_from_us": _epoch_microseconds(binds["created_from"]),
        "created_to_us": _epoch_microseconds(binds["created_to"]),
    }


def _request(
    actor_id: uuid.UUID,
    actor_role: str,
    operation: str,
    *,
    target_id: uuid.UUID | None = None,
    filters: dict | None = None,
    pagination: dict | None = None,
    projection_evidence: dict | None = None,
) -> dict:
    return {
        "schema": _PROTOCOL,
        "request_id": str(uuid.uuid4()),
        "operation": operation,
        "admitted_actor": {"id": str(actor_id), "role": actor_role},
        "target_id": str(target_id) if target_id is not None else None,
        "filters_ascii": _filter_text(filters) if filters is not None else None,
        "pagination": pagination,
        "projection_evidence": projection_evidence,
    }


def _filter_binds(filters: dict | None) -> dict:
    filters = filters or _filters()
    return {
        "status": filters["status"],
        "search": filters["search"],
        "type_keys": filters["type_key"],
        "data_types": filters["data_type"],
        "member_id": uuid.UUID(filters["member_id"])
        if filters["member_id"] is not None
        else None,
        "project_role": filters["project_role"],
        "created_from": _filter_datetime(filters["created_from"]),
        "created_to": _filter_datetime(filters["created_to"]),
    }


async def _step(
    connection: asyncpg.Connection,
    request: dict,
    action: str,
    *,
    parameters: dict | None = None,
    binds: dict | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> dict:
    binds = binds or {}
    if action in ("list_rows", "count_rows") and parameters is None:
        parameters = {"created_from_us": None, "created_to_us": None}
    raw = await connection.fetchval(
        f"""SELECT {_STEP}(
            CAST($1 AS jsonb), $2::text, CAST($3 AS jsonb), $4::text,
            $5::text, $6::text[], $7::text[], $8::uuid, $9::text,
            $10::timestamptz, $11::timestamptz, $12::bigint, $13::integer
        )""",
        json.dumps(request, ensure_ascii=True, separators=(",", ":")),
        action,
        json.dumps(parameters, ensure_ascii=True, separators=(",", ":"))
        if parameters is not None
        else None,
        binds.get("status"),
        binds.get("search"),
        binds.get("type_keys"),
        binds.get("data_types"),
        binds.get("member_id"),
        binds.get("project_role"),
        binds.get("created_from"),
        binds.get("created_to"),
        offset,
        limit,
    )
    reply = _json_object(raw)
    expected = {
        "schema": _PROTOCOL,
        "request": request,
        "action": action,
        "parameters": parameters,
        "facts": reply["facts"],
    }
    assert reply == expected
    return reply["facts"]


async def _expect_invalid(connection, request, action, **kwargs) -> None:
    with pytest.raises(asyncpg.PostgresError) as error:
        await _step(connection, request, action, **kwargs)
    assert error.value.sqlstate == "22023"


def _project_query_dsn(test_engine) -> str:
    value = os.environ.get("AAP_MODULE_DATA_WORKSPACE_PROJECT_QUERY_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.username == _CLIENT
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


async def _cleanup_world(test_engine, world: dict) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        if world["operation_ids"]:
            await session.execute(
                text(
                    "DELETE FROM public.workspace_configuration_operation_receipts "
                    "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": world["operation_ids"]},
            )
            await session.execute(
                text(
                    "DELETE FROM public.workspace_owner_transfer_operation_receipts "
                    "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": world["operation_ids"]},
            )
        await session.execute(
            delete(ProjectMember).where(
                ProjectMember.project_id.in_(world["project_ids"])
            )
        )
        await session.execute(
            delete(Project).where(Project.id.in_(world["project_ids"]))
        )
        await session.execute(delete(User).where(User.id.in_(world["user_ids"])))


async def _replace_transfer_result(test_engine, world: dict, result: dict) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(
            text(
                "UPDATE public.workspace_owner_transfer_operation_receipts "
                "SET result = CAST(:result AS jsonb) WHERE operation_id = :operation_id"
            ),
            {
                "operation_id": world["transfer_operation_id"],
                "result": json.dumps(result, separators=(",", ":")),
            },
        )


async def _seed_world(test_engine) -> dict:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    world = {
        "user_ids": [],
        "project_ids": [],
        "operation_ids": [],
    }
    async with maker.begin() as session:
        actor = await create_user(
            session,
            "project_admin",
            f"project-query-owner-{suffix}@example.test",
            "Project query owner",
        )
        other_owner = await create_user(
            session,
            "employee",
            f"project-query-other-{suffix}@example.test",
            "Project query other owner",
        )
        member = await create_user(
            session,
            "employee",
            f"project-query-member-{suffix}@example.test",
            "Project query member",
        )
        rename_actor = await create_user(
            session,
            "super_admin",
            f"project-query-rename-{suffix}@example.test",
            "Project query rename actor",
        )
        world["user_ids"] = [actor.id, other_owner.id, member.id, rename_actor.id]

        owned = await create_project(
            session,
            owner_id=actor.id,
            name=f"Shared owned project {suffix}",
        )
        shared = await create_project(
            session,
            owner_id=other_owner.id,
            name=f"Shared member project {suffix}",
        )
        hidden = await create_project(
            session,
            owner_id=other_owner.id,
            name=f"Unshared project {suffix}",
        )
        world["project_ids"] = [owned.id, shared.id, hidden.id]
        transfer_updated_at = datetime.now(timezone.utc)

        for project_id in (owned.id, shared.id):
            await create_membership(
                session,
                project_id=project_id,
                user_id=actor.id,
                role="reviewer",
            )
            await create_membership(
                session,
                project_id=project_id,
                user_id=member.id,
                role="annotator" if project_id == owned.id else "reviewer",
            )

        await session.execute(
            text(
                "UPDATE public.projects SET "
                "default_variants = 'null'::jsonb, "
                "preannotate_pipeline = NULL, "
                "label_config = 'null'::jsonb, "
                "batch_summary = 'null'::jsonb, "
                "iou_dedup_threshold = '-0'::double precision, "
                "box_threshold = 0.375, "
                "text_threshold = 0.625, "
                "due_date = 'infinity'::date, "
                "created_at = 'infinity'::timestamptz, "
                "updated_at = :updated_at, "
                "total_tasks = 37, completed_tasks = 12, "
                "review_tasks = 3, in_progress_tasks = 2 "
                "WHERE id = :project_id"
            ),
            {"project_id": owned.id, "updated_at": transfer_updated_at},
        )
        await session.execute(
            text(
                "UPDATE public.projects SET video_collaboration = CAST(:value AS jsonb), "
                "updated_at = '-infinity'::timestamptz "
                "WHERE id = :project_id"
            ),
            {
                "project_id": shared.id,
                "value": json.dumps(
                    {"members": {str(member.id): {"enabled": False}}},
                    separators=(",", ":"),
                ),
            },
        )

        configuration_operation_id = uuid.uuid4()
        transfer_operation_id = uuid.uuid4()
        world["operation_ids"] = [
            configuration_operation_id,
            transfer_operation_id,
        ]
        await session.execute(
            text(
                "INSERT INTO public.workspace_configuration_operation_receipts "
                "(operation_id, actor_id, operation, target_id, request_hash, "
                "selectors, result) VALUES (:operation_id, :actor_id, 'update_project', "
                ":target_id, :request_hash, '{}'::jsonb, CAST(:result AS jsonb))"
            ),
            {
                "operation_id": configuration_operation_id,
                "actor_id": actor.id,
                "target_id": owned.id,
                "request_hash": "a" * 64,
                "result": json.dumps(
                    {"resource_id": str(owned.id), "template_id": None},
                    separators=(",", ":"),
                ),
            },
        )
        await session.execute(
            text(
                "INSERT INTO public.workspace_owner_transfer_operation_receipts "
                "(operation_id, actor_id, project_id, new_owner_id, request_hash, result) "
                "VALUES (:operation_id, :actor_id, :project_id, :new_owner_id, "
                ":request_hash, CAST(:result AS jsonb))"
            ),
            {
                "operation_id": transfer_operation_id,
                "actor_id": rename_actor.id,
                "project_id": owned.id,
                "new_owner_id": actor.id,
                "request_hash": "b" * 64,
                "result": json.dumps(
                    {
                        "schema": "aap-workspace-owner-transfer/v1",
                        "ok": True,
                        "operation_id": str(transfer_operation_id),
                        "actor_id": str(rename_actor.id),
                        "project_id": str(owned.id),
                        "new_owner_id": str(actor.id),
                        "data": {
                            "previous_owner_id": str(actor.id),
                            "updated_at": transfer_updated_at.isoformat(),
                            "changed": False,
                        },
                    },
                    separators=(",", ":"),
                ),
            },
        )

    world.update(
        actor_id=actor.id,
        actor_role="project_admin",
        other_owner_id=other_owner.id,
        member_id=member.id,
        rename_actor_id=rename_actor.id,
        owned_project_id=owned.id,
        shared_project_id=shared.id,
        hidden_project_id=hidden.id,
        configuration_operation_id=configuration_operation_id,
        transfer_operation_id=transfer_operation_id,
        transfer_actor_id=rename_actor.id,
        transfer_new_owner_id=actor.id,
        transfer_updated_at=transfer_updated_at,
        transfer_result={
            "schema": "aap-workspace-owner-transfer/v1",
            "ok": True,
            "operation_id": str(transfer_operation_id),
            "actor_id": str(rename_actor.id),
            "project_id": str(owned.id),
            "new_owner_id": str(actor.id),
            "data": {
                "previous_owner_id": str(actor.id),
                "updated_at": transfer_updated_at.isoformat(),
                "changed": False,
            },
        },
    )
    return world


async def test_workspace_project_query_login_exposes_only_fixed_step_rpcs(test_engine):
    connection = await asyncpg.connect(_project_query_dsn(test_engine))
    try:
        assert await connection.fetchval("SELECT current_user") == _CLIENT
        assert await connection.fetchval("SELECT current_user = session_user") is True
        assert (
            await connection.fetchval(
                "SELECT NOT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_auth_members AS membership "
                "JOIN pg_catalog.pg_roles AS client ON client.oid = membership.member "
                "WHERE client.rolname = current_user)"
            )
            is True
        )
        function_ids = await connection.fetchrow(
            "SELECT "
            "'aap_workspace_project_query_v1.project_query_policy()'::regprocedure::oid AS policy, "
            "'aap_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)'::regprocedure::oid AS step"
        )
        executable = await connection.fetch(
            "SELECT function.oid "
            "FROM pg_catalog.pg_proc AS function "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = function.pronamespace "
            "WHERE namespace.nspname = 'aap_workspace_project_query_v1' "
            "AND pg_catalog.has_function_privilege(current_user, function.oid, 'EXECUTE')"
        )
        assert {row["oid"] for row in executable} == {
            function_ids["policy"],
            function_ids["step"],
        }
        assert _json_object(await connection.fetchval(f"SELECT {_POLICY}")) == {
            "schema": _PROTOCOL
        }

        forbidden = (
            "SELECT id FROM public.projects LIMIT 1",
            "SELECT id FROM public.project_members LIMIT 1",
            "SELECT operation_id FROM public.workspace_configuration_operation_receipts LIMIT 1",
            "SELECT operation_id FROM public.workspace_owner_transfer_operation_receipts LIMIT 1",
            "SELECT aap_workspace_project_query_v1._has_exact_keys('{}'::jsonb, ARRAY[]::text[])",
            "SELECT aap_workspace_project_query_v1._project_query_step_impl("
            "NULL::jsonb,NULL::text,NULL::jsonb,NULL::text,NULL::text,"
            "NULL::text[],NULL::text[],NULL::uuid,NULL::text,"
            "NULL::timestamptz,NULL::timestamptz,NULL::bigint,NULL::integer)",
            "SELECT * FROM aap_workspace_project_query_v1.project_search_scope(NULL, 'employee')",
            "SELECT * FROM aap_workspace_project_query_v1.project_search_member_ids(NULL, 'employee')",
        )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


async def test_project_query_rows_filters_phases_and_projection_receipts(test_engine):
    world = await _seed_world(test_engine)
    connection = await asyncpg.connect(_project_query_dsn(test_engine))
    try:
        empty_filters = _filters()
        list_request = _request(
            world["actor_id"],
            world["actor_role"],
            "list_projects",
            filters=empty_filters,
        )
        list_facts = await _step(
            connection,
            list_request,
            "list_rows",
            binds=_filter_binds(empty_filters),
        )
        listed = json.loads(list_facts["rows_json"])
        assert {uuid.UUID(row["id"]) for row in listed} == {
            world["owned_project_id"],
            world["shared_project_id"],
        }
        assert all(set(row) == _ROW_COLUMNS for row in listed)
        owned_row = next(
            row for row in listed if uuid.UUID(row["id"]) == world["owned_project_id"]
        )
        assert owned_row["total_tasks"] == 37
        assert owned_row["completed_tasks"] == 12
        assert owned_row["review_tasks"] == 3
        assert owned_row["in_progress_tasks"] == 2

        query_filters = _filters(
            status="in_progress",
            search="Shared",
            type_key=["image-det"],
            data_type=["image"],
            member_id=world["member_id"],
            project_role="reviewer",
        )
        query_request = _request(
            world["actor_id"],
            world["actor_role"],
            "query_projects",
            filters=query_filters,
            pagination={"page": "1", "page_size": 1},
        )
        filtered_binds = _filter_binds(query_filters)
        count_facts = await _step(
            connection,
            query_request,
            "count_rows",
            binds=filtered_binds,
        )
        assert count_facts == {"count": "2"}
        page_facts = await _step(
            connection,
            query_request,
            "list_rows",
            binds=filtered_binds,
            offset=0,
            limit=1,
        )
        assert len(json.loads(page_facts["rows_json"])) == 1
        await _expect_invalid(
            connection,
            query_request,
            "count_rows",
            binds={**filtered_binds, "status": "completed"},
        )

        date_filters = _filters(
            created_from="2000-01-01T00:00:00+00:00",
            created_to="2099-01-01T00:00:00+00:00",
        )
        date_request = _request(
            world["actor_id"],
            world["actor_role"],
            "query_projects",
            filters=date_filters,
            pagination={"page": "1", "page_size": 1},
        )
        date_binds = _filter_binds(date_filters)
        date_parameters = _date_bound_parameters(date_filters)
        date_count = await _step(
            connection,
            date_request,
            "count_rows",
            parameters=date_parameters,
            binds=date_binds,
        )
        assert date_count == {"count": "1"}
        date_page = await _step(
            connection,
            date_request,
            "list_rows",
            parameters=date_parameters,
            binds=date_binds,
            offset=0,
            limit=1,
        )
        assert (
            uuid.UUID(json.loads(date_page["rows_json"])[0]["id"])
            == (world["shared_project_id"])
        )
        await _expect_invalid(
            connection,
            date_request,
            "count_rows",
            parameters={
                **date_parameters,
                "created_from_us": str(int(date_parameters["created_from_us"]) + 1),
            },
            binds=date_binds,
        )
        await _expect_invalid(
            connection,
            date_request,
            "list_rows",
            parameters={
                **date_parameters,
                "created_to_us": str(int(date_parameters["created_to_us"]) - 1),
            },
            binds=date_binds,
            offset=0,
            limit=1,
        )

        detail_request = _request(
            world["actor_id"],
            world["actor_role"],
            "get_project",
            target_id=world["owned_project_id"],
        )
        row_facts = await _step(connection, detail_request, "get_row")
        scalar_row = json.loads(row_facts["row_json"])
        assert set(scalar_row) == _ROW_COLUMNS
        assert scalar_row["default_variants"] == "null"
        assert scalar_row["preannotate_pipeline"] is None
        assert scalar_row["label_config"] == "null"
        assert scalar_row["batch_summary"] == "null"
        assert scalar_row["iou_dedup_threshold"] == "-0"
        assert scalar_row["box_threshold"] == "0.375"
        assert scalar_row["text_threshold"] == "0.625"
        assert scalar_row["due_date"] == "infinity"
        assert scalar_row["created_at"] == "infinity"
        assert (
            datetime.fromisoformat(scalar_row["updated_at"])
            == world["transfer_updated_at"]
        )

        shared_detail = _request(
            world["actor_id"],
            world["actor_role"],
            "get_project",
            target_id=world["shared_project_id"],
        )
        shared_row = json.loads(
            (await _step(connection, shared_detail, "get_row"))["row_json"]
        )
        assert shared_row["updated_at"] == "-infinity"

        actor_facts = await _step(connection, detail_request, "actor_status")
        assert actor_facts["actor"] == {
            "actor_id": str(world["actor_id"]),
            "platform_role": "project_admin",
            "is_active": True,
        }
        member_facts = await _step(connection, detail_request, "member_access")
        assert member_facts["member"] == {
            "id": str(member_facts["member"]["id"]),
            "role": "reviewer",
            "project_id": str(world["owned_project_id"]),
            "user_id": str(world["actor_id"]),
        }

        projection_request = _request(
            world["actor_id"],
            world["actor_role"],
            "get_project_projection",
            target_id=world["owned_project_id"],
            projection_evidence={
                "source": "configuration",
                "operation_id": str(world["configuration_operation_id"]),
                "operation": "update_project",
            },
        )
        projection = await _step(connection, projection_request, "projection_row")
        assert json.loads(projection["row_json"])["id"] == str(
            world["owned_project_id"]
        )

        transfer_request = _request(
            world["transfer_actor_id"],
            "super_admin",
            "get_project_projection",
            target_id=world["owned_project_id"],
            projection_evidence={
                "source": "owner_transfer",
                "operation_id": str(world["transfer_operation_id"]),
                "new_owner_id": str(world["transfer_new_owner_id"]),
            },
        )
        transferred = await _step(connection, transfer_request, "projection_row")
        captured_row_json = transferred["row_json"]
        assert json.loads(captured_row_json)["owner_id"] == str(
            world["transfer_new_owner_id"]
        )

        maker = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.projects SET name = :name WHERE id = :project_id"),
                {
                    "name": "Renamed after owner-transfer snapshot",
                    "project_id": world["owned_project_id"],
                },
            )
        refreshed_transfer = await _step(connection, transfer_request, "projection_row")
        assert refreshed_transfer["row_json"] != captured_row_json
        assert json.loads(refreshed_transfer["row_json"])["name"] == (
            "Renamed after owner-transfer snapshot"
        )
        for field, invalid_value in (("ok", False), ("schema", "invalid-schema")):
            tampered_result = dict(world["transfer_result"])
            tampered_result[field] = invalid_value
            await _replace_transfer_result(test_engine, world, tampered_result)
            await _expect_invalid(connection, transfer_request, "projection_row")
            await _replace_transfer_result(test_engine, world, world["transfer_result"])
        await _expect_invalid(
            connection,
            _request(
                world["actor_id"],
                world["actor_role"],
                "get_project_projection",
                target_id=world["owned_project_id"],
                projection_evidence=transfer_request["projection_evidence"],
            ),
            "projection_row",
        )

        rename_evidence = {
            "source": "class_rename",
            "operation_id": str(uuid.uuid4()),
            "source_operation": "rename_class",
            "actor_id": str(world["rename_actor_id"]),
            "project_id": str(world["owned_project_id"]),
            "owner_id": str(world["actor_id"]),
            "platform_role": "super_admin",
            "is_active": True,
        }
        rename_request = _request(
            world["rename_actor_id"],
            "super_admin",
            "get_project_projection",
            target_id=world["owned_project_id"],
            projection_evidence=rename_evidence,
        )
        rename_projection = await _step(connection, rename_request, "projection_row")
        assert json.loads(rename_projection["row_json"])["id"] == str(
            world["owned_project_id"]
        )
        await _expect_invalid(
            connection,
            _request(
                world["actor_id"],
                world["actor_role"],
                "get_project_projection",
                target_id=world["owned_project_id"],
                projection_evidence=rename_evidence,
            ),
            "projection_row",
        )
        await _expect_invalid(connection, list_request, "actor_status")
    finally:
        await connection.close()
        await _cleanup_world(test_engine, world)


async def test_project_search_relations_preserve_raw_scope_and_multiplicity(
    test_engine,
):
    world = await _seed_world(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    connection = await asyncpg.connect(_project_query_dsn(test_engine))
    try:
        search_filters = {"q": "Shared", "limit": 10}
        request = _request(
            world["actor_id"],
            world["actor_role"],
            "search_projects",
            filters=search_filters,
        )
        search_facts = await _step(
            connection,
            request,
            "search_rows",
            binds={"search": "Shared"},
            limit=10,
        )
        rows = json.loads(search_facts["rows_json"])
        assert {uuid.UUID(row["id"]) for row in rows} == {
            world["owned_project_id"],
            world["shared_project_id"],
        }
        assert all(set(row) == _ROW_COLUMNS for row in rows)

        async with maker() as session:
            scope_result = await session.execute(
                text(
                    "SELECT project_id, project_name, owner_id, "
                    "video_collaboration, member_role "
                    "FROM aap_workspace_project_query_v1.project_search_scope("
                    ":actor_id, :role)"
                ),
                {"actor_id": world["actor_id"], "role": world["actor_role"]},
            )
            scope_rows = [dict(row) for row in scope_result.mappings().all()]
            member_result = await session.execute(
                text(
                    "SELECT user_id FROM "
                    "aap_workspace_project_query_v1.project_search_member_ids("
                    ":actor_id, :role)"
                ),
                {"actor_id": world["actor_id"], "role": world["actor_role"]},
            )
            member_ids = [row[0] for row in member_result.all()]

        assert {row["project_id"] for row in scope_rows} == {
            world["owned_project_id"],
            world["shared_project_id"],
        }
        assert all(row["member_role"] == "reviewer" for row in scope_rows)
        shared_scope = next(
            row for row in scope_rows if row["project_id"] == world["shared_project_id"]
        )
        assert _json_object(shared_scope["video_collaboration"]) == {
            "members": {str(world["member_id"]): {"enabled": False}}
        }
        assert member_ids.count(world["actor_id"]) == 2
        assert member_ids.count(world["member_id"]) == 2
        assert world["hidden_project_id"] not in {
            row["project_id"] for row in scope_rows
        }
    finally:
        await connection.close()
        await _cleanup_world(test_engine, world)
