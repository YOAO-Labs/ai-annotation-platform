"""Native fixed facts for the Workspace ProjectTemplate query owner."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import math
from pathlib import Path
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.organization import Organization, OrganizationMember
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from tests.factory import create_user

_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_SQL = _SQL_DIR / "0259_workspace_template_queries.sql"
_PROTOCOL = "aap-workspace-template-query/v1"
_POLICY = "aap_workspace_template_query_v1.template_query_policy()"
_STEP = "aap_workspace_template_query_v1.template_query_step"
_ROW_COLUMNS = (
    "id",
    "display_id",
    "name",
    "description",
    "type_label",
    "type_key",
    "data_type",
    "tool_bindings",
    "label_config",
    "ai_enabled",
    "sampling",
    "maximum_annotations",
    "show_overlap_first",
    "iou_dedup_threshold",
    "box_threshold",
    "text_threshold",
    "rendering_config",
    "annotation_guide",
    "scope",
    "organization_id",
    "created_by",
    "source_project_id",
    "usage_count",
    "created_at",
    "updated_at",
)


def _json_object(value):
    return json.loads(value) if isinstance(value, str) else value


def _sqlstate(error: BaseException) -> str | None:
    return getattr(error, "sqlstate", None) or getattr(
        getattr(error, "orig", None), "sqlstate", None
    )


def _filters(**values) -> str:
    return json.dumps(
        values,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _request(
    actor_id: uuid.UUID,
    role: str,
    operation: str,
    *,
    target_id: uuid.UUID | None = None,
    receipt: dict | None = None,
    filters_ascii: str | None = None,
) -> dict:
    return {
        "schema": _PROTOCOL,
        "request_id": str(uuid.uuid4()),
        "operation": operation,
        "admitted_actor": {"id": str(actor_id), "role": role},
        "target_id": str(target_id) if target_id is not None else None,
        "configuration_receipt": receipt,
        "filters_ascii": filters_ascii,
    }


async def _step(
    session,
    request: dict,
    action: str,
    *,
    parameters: dict | None = None,
    scope: str | None = None,
    type_keys: list[str] | None = None,
    search: str | None = None,
) -> dict:
    reply = await session.scalar(
        text(
            f"SELECT {_STEP}("
            "CAST(:request AS jsonb), :action, CAST(:parameters AS jsonb), "
            "CAST(:scope AS text), CAST(:type_keys AS text[]), CAST(:search AS text))"
        ),
        {
            "request": json.dumps(request, ensure_ascii=True, separators=(",", ":")),
            "action": action,
            "parameters": (
                json.dumps(parameters, ensure_ascii=True, separators=(",", ":"))
                if parameters is not None
                else None
            ),
            "scope": scope,
            "type_keys": type_keys,
            "search": search,
        },
    )
    decoded = _json_object(reply)
    assert decoded == {
        "schema": _PROTOCOL,
        "request": request,
        "action": action,
        "facts": decoded["facts"],
    }
    return decoded["facts"]


async def _expect_invalid_step(session, request: dict, action: str, **kwargs):
    with pytest.raises(DBAPIError) as error:
        await _step(session, request, action, **kwargs)
    assert _sqlstate(error.value) == "22023"
    await session.rollback()


async def _template(
    session,
    creator_id: uuid.UUID,
    suffix: str,
    name: str,
    *,
    type_key: str = "image-det",
    scope: str = "private",
    organization_id: uuid.UUID | None = None,
    **values,
) -> ProjectTemplate:
    template = ProjectTemplate(
        display_id=f"TQ-{suffix[:8]}-{uuid.uuid4().hex[:6]}",
        name=name,
        type_label="Template query fixture",
        type_key=type_key,
        data_type="image",
        scope=scope,
        organization_id=organization_id,
        created_by=creator_id,
        **values,
    )
    session.add(template)
    await session.flush()
    return template


def test_template_query_native_sql_exposes_only_fixed_read_facts():
    sql = _SQL.read_text(encoding="utf-8")
    assert (
        sql.count(
            "CREATE FUNCTION aap_workspace_template_query_v1.template_query_policy"
        )
        == 1
    )
    assert (
        sql.count("CREATE FUNCTION aap_workspace_template_query_v1.template_query_step")
        == 1
    )
    assert "RETURNS jsonb" in sql
    assert "STABLE" in sql
    assert "PARALLEL UNSAFE" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, pg_temp" in sql
    assert "SET TimeZone TO 'UTC'" in sql
    assert "_template_row_json" in sql
    assert "iou_dedup_threshold', (p_template).iou_dedup_threshold::text" in sql
    assert "box_threshold', (p_template).box_threshold::text" in sql
    assert "text_threshold', (p_template).text_threshold::text" in sql
    assert "aap_identity_v1.workspace_template_query_org_ids" in sql
    assert "aap_identity_v1.read_workspace_membership_accounts" in sql
    assert "workspace_configuration_operation_receipts" in sql
    assert "ORDER BY row_facts.created_at DESC" in sql
    assert "ILIKE '%' || p_search || '%'" in sql
    assert "INSERT INTO" not in sql.upper()
    assert "UPDATE PUBLIC." not in sql.upper()
    assert "DELETE FROM" not in sql.upper()
    assert "FOR UPDATE" not in sql.upper()
    assert "FOR SHARE" not in sql.upper()
    assert "COMMIT;" not in sql.upper()
    assert "ROLLBACK;" not in sql.upper()


@pytest.mark.asyncio
async def test_list_rows_preserve_visibility_and_legacy_filter_phases(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_ids: list[uuid.UUID] = []
    organization_ids: list[uuid.UUID] = []
    template_ids: list[uuid.UUID] = []
    try:
        async with maker.begin() as session:
            actor = await create_user(
                session,
                "employee",
                f"template-query-list-actor-{suffix}@example.test",
                "Template query actor",
            )
            other = await create_user(
                session,
                "employee",
                f"template-query-list-other-{suffix}@example.test",
                "Template query other creator",
            )
            user_ids = [actor.id, other.id]
            visible_org = Organization(
                name=f"Template query visible {suffix}",
                slug=f"template-query-visible-{suffix}",
                created_by=actor.id,
            )
            retired_org = Organization(
                name=f"Template query retired {suffix}",
                slug=f"template-query-retired-{suffix}",
                created_by=actor.id,
            )
            session.add_all([visible_org, retired_org])
            await session.flush()
            organization_ids = [visible_org.id, retired_org.id]
            session.add_all(
                [
                    OrganizationMember(
                        organization_id=visible_org.id,
                        user_id=actor.id,
                        role="unrecognized-role",
                    ),
                    OrganizationMember(
                        organization_id=retired_org.id,
                        user_id=actor.id,
                        role="member",
                        deleted_at=datetime.now(UTC),
                    ),
                ]
            )
            templates = [
                await _template(
                    session,
                    other.id,
                    suffix,
                    "Org 50XY雪 video",
                    type_key="video-track",
                    scope="organization",
                    organization_id=visible_org.id,
                ),
                await _template(
                    session,
                    other.id,
                    suffix,
                    "Org literal_name video",
                    type_key="video-track",
                    scope="organization",
                    organization_id=visible_org.id,
                ),
                await _template(
                    session,
                    other.id,
                    suffix,
                    "Org image",
                    scope="organization",
                    organization_id=visible_org.id,
                ),
                await _template(
                    session,
                    other.id,
                    suffix,
                    "Hidden organization video",
                    type_key="video-track",
                    scope="organization",
                    organization_id=retired_org.id,
                ),
                await _template(session, actor.id, suffix, "Own private template"),
                await _template(session, other.id, suffix, "Other private template"),
                await _template(
                    session,
                    other.id,
                    suffix,
                    "Public template",
                    scope="public",
                ),
            ]
            template_ids = [template.id for template in templates]
            await session.flush()

            policy = _json_object(await session.scalar(text(f"SELECT {_POLICY}")))
            assert policy == {"schema": _PROTOCOL}

            request = _request(
                actor.id,
                "legacy-historic-role",
                "list_templates",
                filters_ascii=_filters(
                    scope="organization",
                    type_key=["video-track", "video-track"],
                    search="50%_雪",
                ),
            )
            org_facts = await _step(session, request, "organization_ids")
            assert set(org_facts["organization_ids"]) == {str(visible_org.id)}
            rows_facts = await _step(
                session,
                request,
                "list_rows",
                parameters={"organization_ids": org_facts["organization_ids"]},
                scope="organization",
                type_keys=["video-track", "video-track"],
                search="50%_雪",
            )
            rows = json.loads(rows_facts["rows_json"])
            assert [row["id"] for row in rows] == [str(templates[0].id)]
            assert set(rows[0]) == set(_ROW_COLUMNS)

            literal_request = _request(
                actor.id,
                "legacy-historic-role",
                "list_templates",
                filters_ascii=_filters(
                    scope="organization",
                    type_key=["video-track"],
                    search=r"literal\_name",
                ),
            )
            literal_rows = await _step(
                session,
                literal_request,
                "list_rows",
                parameters={"organization_ids": org_facts["organization_ids"]},
                scope="organization",
                type_keys=["video-track"],
                search=r"literal\_name",
            )
            assert [row["id"] for row in json.loads(literal_rows["rows_json"])] == [
                str(templates[1].id)
            ]

            empty_filter_request = _request(
                actor.id,
                "legacy-historic-role",
                "list_templates",
                filters_ascii=_filters(scope="", type_key=[], search=""),
            )
            ordinary = await _step(
                session,
                empty_filter_request,
                "list_rows",
                parameters={"organization_ids": org_facts["organization_ids"]},
                scope="",
                type_keys=[],
                search="",
            )
            assert {row["id"] for row in json.loads(ordinary["rows_json"])} == {
                str(templates[index].id) for index in (0, 1, 2, 4, 6)
            }

            super_admin_request = _request(
                other.id,
                "super_admin",
                "list_templates",
                filters_ascii=_filters(scope=None, type_key=None, search=None),
            )
            super_admin_rows = await _step(
                session,
                super_admin_request,
                "list_rows",
                parameters={"organization_ids": []},
            )
            assert {row["id"] for row in json.loads(super_admin_rows["rows_json"])} == {
                str(template_id) for template_id in template_ids
            }

        malformed_request = dict(request, unexpected=True)
        async with maker() as session:
            await _expect_invalid_step(session, malformed_request, "organization_ids")
        mismatched_request = _request(
            actor.id,
            "legacy-historic-role",
            "list_templates",
            filters_ascii=_filters(scope="public", type_key=None, search=None),
        )
        async with maker() as session:
            await _expect_invalid_step(
                session,
                mismatched_request,
                "list_rows",
                parameters={"organization_ids": org_facts["organization_ids"]},
                scope="private",
            )

        async with maker.begin() as session:
            await session.execute(
                delete(ProjectTemplate).where(ProjectTemplate.id.in_(template_ids))
            )
            await session.execute(
                delete(OrganizationMember).where(
                    OrganizationMember.user_id.in_(user_ids)
                )
            )
            await session.execute(
                delete(Organization).where(Organization.id.in_(organization_ids))
            )
            await session.execute(delete(User).where(User.id.in_(user_ids)))
    except BaseException:
        async with maker.begin() as cleanup:
            if template_ids:
                await cleanup.execute(
                    delete(ProjectTemplate).where(ProjectTemplate.id.in_(template_ids))
                )
            if user_ids:
                await cleanup.execute(
                    delete(OrganizationMember).where(
                        OrganizationMember.user_id.in_(user_ids)
                    )
                )
            if organization_ids:
                await cleanup.execute(
                    delete(Organization).where(Organization.id.in_(organization_ids))
                )
            if user_ids:
                await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
        raise


@pytest.mark.asyncio
async def test_detail_row_preserves_raw_json_scalars_and_local_timezone(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = None
    template_ids: list[uuid.UUID] = []
    try:
        async with maker.begin() as session:
            actor = await create_user(
                session,
                "employee",
                f"template-query-detail-actor-{suffix}@example.test",
                "Template query detail actor",
            )
            creator = await create_user(
                session,
                "project_admin",
                f"template-query-detail-creator-{suffix}@example.test",
                "Template query raw creator",
            )
            user_id = actor.id
            template = await _template(
                session,
                creator.id,
                suffix,
                "Private inaccessible template",
                scope="private",
                iou_dedup_threshold=-0.0,
                tool_bindings=None,
                label_config={"nested": [None, {"values": [1, "雪"]}]},
                created_at=datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
                updated_at=datetime(2024, 2, 3, 4, 5, 6, tzinfo=UTC),
            )
            special_time = await _template(
                session,
                creator.id,
                suffix,
                "Special timestamp template",
            )
            template_ids = [template.id, special_time.id]
            await session.execute(
                text(
                    "UPDATE public.project_templates "
                    "SET tool_bindings='null'::jsonb "
                    "WHERE id=:template_id"
                ),
                {"template_id": template.id},
            )
            await session.execute(
                text(
                    "UPDATE public.project_templates "
                    "SET created_at='infinity'::timestamptz, "
                    "updated_at='-infinity'::timestamptz WHERE id=:template_id"
                ),
                {"template_id": special_time.id},
            )
            await session.execute(
                text("UPDATE public.users SET name='', is_active=false WHERE id=:id"),
                {"id": creator.id},
            )

            request = _request(
                actor.id,
                "unrecognized-historical-role",
                "get_template",
                target_id=template.id,
            )
            await session.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))
            before_timezone = await session.scalar(text("SHOW TimeZone"))
            row_facts = await _step(session, request, "get_row")
            after_timezone = await session.scalar(text("SHOW TimeZone"))
            row = json.loads(row_facts["row_json"])
            assert before_timezone == after_timezone == "America/Los_Angeles"
            assert set(row) == set(_ROW_COLUMNS)
            assert row["tool_bindings"] is None
            assert row["label_config"] == {"nested": [None, {"values": [1, "雪"]}]}
            assert row["iou_dedup_threshold"].startswith("-")
            assert math.copysign(1.0, float(row["iou_dedup_threshold"])) == -1.0
            assert datetime.fromisoformat(row["created_at"]) == datetime(
                2024, 1, 2, 3, 4, 5, tzinfo=UTC
            )
            assert datetime.fromisoformat(row["updated_at"]) == datetime(
                2024, 2, 3, 4, 5, 6, tzinfo=UTC
            )

            creator_facts = await _step(
                session,
                request,
                "creator_name",
                parameters={"creator_id": str(creator.id)},
            )
            assert creator_facts == {"name": ""}
            missing_creator = await _step(
                session,
                request,
                "creator_name",
                parameters={"creator_id": str(uuid.uuid4())},
            )
            assert missing_creator == {"name": None}

            special_request = _request(
                actor.id,
                "unrecognized-historical-role",
                "get_template",
                target_id=special_time.id,
            )
            special = await _step(session, special_request, "get_row")
            special_row = json.loads(special["row_json"])
            assert special_row["created_at"] == "infinity"
            assert special_row["updated_at"] == "-infinity"
        async with maker.begin() as session:
            await session.execute(
                delete(ProjectTemplate).where(ProjectTemplate.id.in_(template_ids))
            )
            await session.execute(delete(User).where(User.id == user_id))
            await session.execute(
                delete(User).where(
                    User.email == f"template-query-detail-creator-{suffix}@example.test"
                )
            )
    except BaseException:
        async with maker.begin() as cleanup:
            if template_ids:
                await cleanup.execute(
                    delete(ProjectTemplate).where(ProjectTemplate.id.in_(template_ids))
                )
            await cleanup.execute(
                delete(User).where(
                    User.email.in_(
                        [
                            f"template-query-detail-actor-{suffix}@example.test",
                            f"template-query-detail-creator-{suffix}@example.test",
                        ]
                    )
                )
            )
        raise


@pytest.mark.asyncio
async def test_projection_requires_matching_receipt_and_remains_read_only(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    actor_id = None
    creator_id = None
    template_id = None
    operation_id = uuid.uuid4()
    try:
        async with maker.begin() as session:
            actor = await create_user(
                session,
                "employee",
                f"template-query-projection-actor-{suffix}@example.test",
                "Projection admitted actor",
            )
            creator = await create_user(
                session,
                "employee",
                f"template-query-projection-creator-{suffix}@example.test",
                "Projection creator",
            )
            actor_id, creator_id = actor.id, creator.id
            template = await _template(
                session,
                creator.id,
                suffix,
                "Private projection template",
            )
            template_id = template.id
            receipt_result = {
                "resource_id": str(template.id),
                "template_id": str(template.id),
            }
            await session.execute(
                text(
                    """
                    INSERT INTO public.workspace_configuration_operation_receipts (
                        operation_id, actor_id, operation, target_id, request_hash,
                        selectors, result
                    ) VALUES (
                        :operation_id, :actor_id, 'create_template', NULL,
                        :request_hash, CAST(:selectors AS jsonb), CAST(:result AS jsonb)
                    )
                    """
                ),
                {
                    "operation_id": operation_id,
                    "actor_id": actor.id,
                    "request_hash": "a" * 64,
                    "selectors": "{}",
                    "result": json.dumps(receipt_result),
                },
            )

        receipt_count_query = text(
            "SELECT count(*) FROM public.workspace_configuration_operation_receipts "
            "WHERE operation_id=:operation_id"
        )
        receipt = {
            "operation_id": str(operation_id),
            "operation": "create_template",
        }
        projection = _request(
            actor_id,
            "admitted-actor-snapshot",
            "get_template_projection",
            target_id=template_id,
            receipt=receipt,
        )
        async with maker() as session:
            before = await session.scalar(
                receipt_count_query, {"operation_id": operation_id}
            )
            matching = await _step(session, projection, "projection_row")
            assert json.loads(matching["row_json"])["id"] == str(template_id)
            after = await session.scalar(
                receipt_count_query, {"operation_id": operation_id}
            )
            assert before == after == 1
            await session.rollback()

        wrong_actor = _request(
            uuid.uuid4(),
            "admitted-actor-snapshot",
            "get_template_projection",
            target_id=template_id,
            receipt=receipt,
        )
        async with maker() as session:
            await _expect_invalid_step(session, wrong_actor, "projection_row")

        wrong_operation = _request(
            actor_id,
            "admitted-actor-snapshot",
            "get_template_projection",
            target_id=template_id,
            receipt={
                "operation_id": str(operation_id),
                "operation": "update_template",
            },
        )
        async with maker() as session:
            await _expect_invalid_step(session, wrong_operation, "projection_row")

        wrong_receipt = _request(
            actor_id,
            "admitted-actor-snapshot",
            "get_template_projection",
            target_id=template_id,
            receipt={
                "operation_id": str(uuid.uuid4()),
                "operation": "create_template",
            },
        )
        async with maker() as session:
            await _expect_invalid_step(session, wrong_receipt, "projection_row")

        wrong_result = _request(
            actor_id,
            "admitted-actor-snapshot",
            "get_template_projection",
            target_id=uuid.uuid4(),
            receipt=receipt,
        )
        async with maker() as session:
            await _expect_invalid_step(session, wrong_result, "projection_row")

        async with maker.begin() as session:
            await session.execute(
                delete(ProjectTemplate).where(ProjectTemplate.id == template_id)
            )
        async with maker() as session:
            before = await session.scalar(
                receipt_count_query, {"operation_id": operation_id}
            )
            deleted = await _step(session, projection, "projection_row")
            after = await session.scalar(
                receipt_count_query, {"operation_id": operation_id}
            )
            assert deleted == {"row_json": None}
            assert before == after == 1
            await session.rollback()
    finally:
        async with maker.begin() as session:
            if template_id is not None:
                await session.execute(
                    delete(ProjectTemplate).where(ProjectTemplate.id == template_id)
                )
            await session.execute(
                text(
                    "DELETE FROM public.workspace_configuration_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            if actor_id is not None:
                await session.execute(delete(User).where(User.id == actor_id))
            if creator_id is not None:
                await session.execute(delete(User).where(User.id == creator_id))
