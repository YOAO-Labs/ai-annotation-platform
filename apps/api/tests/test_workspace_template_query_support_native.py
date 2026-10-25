"""Identity facts used by future Workspace template-query ownership."""

from __future__ import annotations

import json
import os
from pathlib import Path
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.organization import Organization, OrganizationMember
from app.db.models.user import User
from tests.factory import create_user

_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_SCHEMA = _SQL_DIR / "0258_workspace_template_query_support.schema.json"
_PROTOCOL = "aap-identity-workspace-template-query/v1"
_FUNCTION = "aap_identity_v1.workspace_template_query_org_ids(uuid)"


def _json_object(value):
    return json.loads(value) if isinstance(value, str) else value


def _sqlstate(error: BaseException) -> str | None:
    return getattr(error, "sqlstate", None) or getattr(
        getattr(error, "orig", None), "sqlstate", None
    )


def test_template_query_identity_fact_artifacts_are_narrow_and_aligned():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))

    assert schema["$id"] == _PROTOCOL
    assert schema["$ref"] == "#/$defs/WorkspaceTemplateQueryOrganizationIds"
    assert schema["$defs"]["WorkspaceTemplateQueryOrganizationIds"]["required"] == [
        "schema",
        "actor_id",
        "organization_ids",
    ]
    assert schema["x-postgresql-functions"] == [_FUNCTION]
    fact = schema["$defs"]["WorkspaceTemplateQueryOrganizationIds"]
    assert fact["additionalProperties"] is False
    assert set(fact["properties"]) == {"schema", "actor_id", "organization_ids"}


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the owned module-data verifier and function owner grants",
)
async def test_template_query_org_ids_returns_current_memberships_only(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_ids: list[uuid.UUID] = []
    organization_ids: list[uuid.UUID] = []
    try:
        async with maker.begin() as session:
            actor = await create_user(
                session,
                "super_admin",
                f"template-query-actor-{suffix}@example.test",
                "Template query actor",
            )
            inactive_actor = await create_user(
                session,
                "employee",
                f"template-query-inactive-{suffix}@example.test",
                "Inactive template query actor",
            )
            nonmember = await create_user(
                session,
                "employee",
                f"template-query-nonmember-{suffix}@example.test",
                "Template query nonmember",
            )
            inactive_actor.is_active = False
            user_ids = [actor.id, inactive_actor.id, nonmember.id]

            visible_unknown_role = Organization(
                name=f"Visible unknown role {suffix}",
                slug=f"template-query-visible-role-{suffix}",
                created_by=actor.id,
            )
            visible_member_role = Organization(
                name=f"Visible member role {suffix}",
                slug=f"template-query-visible-member-{suffix}",
                created_by=actor.id,
            )
            retired_membership_org = Organization(
                name=f"Retired membership {suffix}",
                slug=f"template-query-retired-{suffix}",
                created_by=actor.id,
            )
            inactive_actor_org = Organization(
                name=f"Inactive actor membership {suffix}",
                slug=f"template-query-inactive-org-{suffix}",
                created_by=actor.id,
            )
            session.add_all(
                [
                    visible_unknown_role,
                    visible_member_role,
                    retired_membership_org,
                    inactive_actor_org,
                ]
            )
            await session.flush()
            organization_ids = [
                visible_unknown_role.id,
                visible_member_role.id,
                retired_membership_org.id,
                inactive_actor_org.id,
            ]
            session.add_all(
                [
                    OrganizationMember(
                        organization_id=visible_unknown_role.id,
                        user_id=actor.id,
                        role="unknown-role",
                    ),
                    OrganizationMember(
                        organization_id=visible_member_role.id,
                        user_id=actor.id,
                        role="member",
                    ),
                    OrganizationMember(
                        organization_id=retired_membership_org.id,
                        user_id=actor.id,
                        role="member",
                        deleted_at=datetime.now(timezone.utc),
                    ),
                    OrganizationMember(
                        organization_id=inactive_actor_org.id,
                        user_id=inactive_actor.id,
                        role="member",
                    ),
                ]
            )
            await session.flush()

            query = text(
                "SELECT aap_identity_v1.workspace_template_query_org_ids("
                "CAST(:actor_id AS uuid))"
            )
            actor_reply = _json_object(
                await session.scalar(query, {"actor_id": actor.id})
            )
            inactive_reply = _json_object(
                await session.scalar(query, {"actor_id": inactive_actor.id})
            )
            nonmember_reply = _json_object(
                await session.scalar(query, {"actor_id": nonmember.id})
            )
            function_access = await session.execute(
                text(
                    """
                    SELECT namespace.nspname AS schema_name,
                           function.prosecdef AS security_definer,
                           function.provolatile::text AS volatility,
                           function.proparallel::text AS parallel_mode,
                           function.proconfig AS function_settings,
                           pg_catalog.pg_get_userbyid(function.proowner) AS owner_role,
                           pg_catalog.has_function_privilege(
                               'workspace_code', function.oid, 'EXECUTE'
                           ) AS workspace_can_execute,
                           pg_catalog.has_function_privilege(
                               current_user, function.oid, 'EXECUTE'
                           ) AS current_user_can_execute,
                           EXISTS (
                               SELECT 1
                               FROM pg_catalog.aclexplode(COALESCE(
                                   function.proacl,
                                   pg_catalog.acldefault('f', function.proowner)
                               )) AS privilege
                               WHERE privilege.grantee = 0
                                 AND privilege.privilege_type = 'EXECUTE'
                           ) AS public_can_execute,
                           pg_catalog.to_regprocedure(
                               'public.workspace_template_query_org_ids(uuid)'
                           ) IS NULL AS absent_from_public
                    FROM pg_catalog.pg_proc AS function
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = function.pronamespace
                    WHERE function.oid = pg_catalog.to_regprocedure(:signature)
                    """
                ),
                {"signature": _FUNCTION},
            )
            access = function_access.mappings().one()

        assert set(actor_reply) == {"schema", "actor_id", "organization_ids"}
        assert actor_reply["schema"] == _PROTOCOL
        assert actor_reply["actor_id"] == str(user_ids[0])
        assert set(actor_reply["organization_ids"]) == {
            str(organization_ids[0]),
            str(organization_ids[1]),
        }
        assert len(actor_reply["organization_ids"]) == 2
        assert inactive_reply == {
            "schema": _PROTOCOL,
            "actor_id": str(user_ids[1]),
            "organization_ids": [str(organization_ids[3])],
        }
        assert nonmember_reply == {
            "schema": _PROTOCOL,
            "actor_id": str(user_ids[2]),
            "organization_ids": [],
        }
        assert access["schema_name"] == "aap_identity_v1"
        assert access["security_definer"] is True
        assert access["volatility"] == "s"
        assert access["parallel_mode"] == "u"
        assert access["function_settings"] == ["search_path=pg_catalog, pg_temp"]
        assert access["owner_role"] == "identity_code"
        assert access["workspace_can_execute"] is True
        assert access["current_user_can_execute"] is True
        assert access["public_can_execute"] is False
        assert access["absent_from_public"] is True

        async with maker() as session:
            with pytest.raises(DBAPIError) as invalid_actor:
                await session.scalar(
                    text(
                        "SELECT aap_identity_v1.workspace_template_query_org_ids(NULL::uuid)"
                    )
                )
            assert _sqlstate(invalid_actor.value) == "22023"
            await session.rollback()
    finally:
        if user_ids:
            async with maker.begin() as session:
                await session.execute(
                    delete(OrganizationMember).where(
                        OrganizationMember.user_id.in_(user_ids)
                    )
                )
                if organization_ids:
                    await session.execute(
                        delete(Organization).where(
                            Organization.id.in_(organization_ids)
                        )
                    )
                await session.execute(delete(User).where(User.id.in_(user_ids)))
