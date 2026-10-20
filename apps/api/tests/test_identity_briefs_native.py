"""Native Identity owner qualification for batched UserBrief reads."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aap_identity
from aap_identity.briefs_contract import decode_briefs_request
from aap_identity.briefs_manager import BriefsManager
from aap_identity.briefs_postgres import BriefsPostgresData
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_project

_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_SCHEMA = _SQL_DIR / "0249_identity_briefs.schema.json"
_SUPPORT = _SQL_DIR / "0249_identity_briefs_support.sql"
_ARTIFACT = _SQL_DIR / "0249_identity_briefs.sql"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0249_identity_briefs.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_ROLE_GRANTS = _ROOT / "infra/docker/identity-briefs-grants.sql"
_LAN_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"
_VERIFIER = _ROOT / "scripts/verify_module_data_boundary.py"
_FIXTURE = Path(__file__).parent / "fixtures/identity_briefs_legacy.json"
_CANONICAL_SCHEMA = (
    _ROOT / "apps/identity/src/aap_identity/native_briefs_protocol.schema.json"
)
if not _CANONICAL_SCHEMA.is_file():
    _CANONICAL_SCHEMA = (
        Path(aap_identity.__file__).resolve().parent
        / "native_briefs_protocol.schema.json"
    )


def _briefs_dsn(test_engine=None, database: str | None = None) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity briefs acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_BRIEFS_URL")
    if not value:
        pytest.skip("Identity briefs client DSN is not configured")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_briefs_client"
    assert os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    if database is not None:
        parsed = parsed.set(database=database)
    elif test_engine is not None:
        assert (parsed.host, parsed.port, parsed.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _request(operation: str, user_ids: list[uuid.UUID], project_id=None):
    payload = {"user_ids": [str(user_id) for user_id in user_ids]}
    if operation == "get_project_user_briefs":
        payload["project_id"] = str(project_id)
    return decode_briefs_request(
        {"schema": "aap-identity-briefs/v1", "operation": operation, "payload": payload}
    )


def _native_manager(test_engine, database: str | None = None) -> BriefsManager:
    return BriefsManager(BriefsPostgresData(_briefs_dsn(test_engine, database)))


async def _seed_legacy_world(test_engine, fixture: dict):
    inputs = fixture["inputs"]
    ids = {item["key"]: uuid.uuid4() for item in inputs}
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        for item in inputs:
            is_active = item["is_active"]
            session.add(
                User(
                    id=ids[item["key"]],
                    email=item["email"],
                    name=item["name"],
                    password_hash="test-only-identity-briefs-hash",
                    role=item["role"],
                    is_active=is_active,
                    status="online" if is_active else "offline",
                    disabled_kind=None if is_active else "suspended",
                    disabled_reason=None
                    if is_active
                    else "preserved historical detail",
                    disabled_at=None if is_active else datetime.now(UTC),
                    avatar_ref=item["avatar_ref"],
                )
            )
        await session.flush()
        project = await create_project(
            session, owner_id=ids["owner"], name="Identity briefs native test"
        )
        project_id = project.id
        role_by_key = {
            "staff": "reviewer",
            "historical": "annotator",
            "combining": "annotator",
            "empty": "",
        }
        for key, role in role_by_key.items():
            session.add(
                ProjectMember(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    user_id=ids[key],
                    role=role,
                    assigned_by=ids["owner"],
                )
            )
        await session.flush()
        inactive_id = ids["inactive"]
        pending_requested = datetime.now(UTC)
        await session.execute(
            text(
                "UPDATE public.users SET deactivation_requested_at=:requested, "
                "deactivation_scheduled_at=:scheduled, "
                "deactivation_reason='pending lifecycle request' WHERE id=:id"
            ),
            {
                "requested": pending_requested,
                "scheduled": pending_requested + timedelta(days=7),
                "id": inactive_id,
            },
        )
    return ids, project_id


async def _cleanup_briefs_world(test_engine, *, user_ids=(), project_ids=()):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(
            text(
                "DELETE FROM public.project_members WHERE project_id=ANY(CAST(:projects AS uuid[])) "
                "OR user_id=ANY(CAST(:users AS uuid[]))"
            ),
            {"projects": list(project_ids), "users": list(user_ids)},
        )
        await session.execute(
            text("DELETE FROM public.projects WHERE id=ANY(CAST(:ids AS uuid[]))"),
            {"ids": list(project_ids)},
        )
        await session.execute(
            text("DELETE FROM public.users WHERE id=ANY(CAST(:ids AS uuid[]))"),
            {"ids": list(user_ids)},
        )


class _NoBriefsData:
    def transaction(self):
        raise AssertionError("empty brief reads must not call the data port")


@pytest.mark.asyncio
async def test_empty_brief_ids_skip_the_owner_call():
    reply = await BriefsManager(_NoBriefsData()).execute(
        _request("get_user_briefs", [])
    )
    assert reply.data.project_id is None
    assert reply.data.users == []


def test_frozen_briefs_schema_and_function_only_boundary():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    canonical = json.loads(_CANONICAL_SCHEMA.read_text(encoding="utf-8"))
    support = _SUPPORT.read_text(encoding="utf-8")
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    role_grants = _ROLE_GRANTS.read_text(encoding="utf-8")
    lan_grants = _LAN_GRANTS.read_text(encoding="utf-8")
    verifier = _VERIFIER.read_text(encoding="utf-8")
    normalized_support = " ".join(support.split())
    compact_migration = "".join(migration.split())
    compact_module_grants = "".join(module_grants.split())
    compact_role_grants = "".join(role_grants.split())
    compact_lan_grants = "".join(lan_grants.split())

    assert schema == canonical
    assert _SCHEMA.read_bytes() == _CANONICAL_SCHEMA.read_bytes()
    assert schema["$id"] == "aap-identity-briefs-native/v1"
    assert 'revision = "0249"' in migration
    assert 'down_revision = "0248"' in migration
    assert migration.index('"0249_identity_briefs_support.sql"') < migration.index(
        '"0249_identity_briefs.sql"'
    )
    assert (
        "CREATE FUNCTION aap_workspace_v1.identity_brief_project_roles( "
        "p_project_id uuid, p_user_ids uuid[] ) RETURNS TABLE(user_id uuid, role text)"
        in normalized_support
    )
    assert (
        "aap_workspace_v1.identity_brief_project_roles(uuid,uuid[])"
        in compact_migration
    )
    assert "SECURITY DEFINER" in support
    assert "SET search_path = pg_catalog, pg_temp" in support
    assert "CREATE TABLE" not in support + artifact
    assert "INSERT INTO" not in support + artifact
    assert "UPDATE public." not in support + artifact
    assert "DELETE FROM" not in support + artifact
    assert "audit_logs" not in support + artifact
    assert (
        "aap_workspace_v1.identity_brief_project_roles(uuid,uuid[])"
        in compact_module_grants
    )
    assert "GRANTSELECT(id,project_id,user_id,role)" in compact_module_grants
    assert 'ONpublic.project_membersTO:"identity_owner"' not in compact_module_grants
    assert "identity_briefs_client" in role_grants
    assert "aap_identity_briefs_v1.briefs_policy()" in compact_role_grants
    assert "aap_identity_briefs_v1.execute_briefs_command(jsonb)" in compact_role_grants
    assert "aap_identity_briefs_v1" in lan_grants
    assert "aap_identity_briefs_v1.execute_briefs_command(jsonb)" in compact_lan_grants
    assert '"BRIEFS": "identity_briefs_client"' in verifier
    assert '"0249_identity_briefs.sql"' in verifier
    assert '"0249_identity_briefs.schema.json"' in verifier


@pytest.mark.asyncio
async def test_native_briefs_match_legacy_fixture_for_large_and_project_reads(
    test_engine,
):
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    manager = _native_manager(test_engine)
    ids, project_id = await _seed_legacy_world(test_engine, fixture)
    user_ids = [ids[item["key"]] for item in fixture["inputs"]]
    old_to_new = {item["id"]: str(ids[item["key"]]) for item in fixture["inputs"]}

    def expected_map(rows):
        return {
            old_to_new[old_id]: {**row, "id": old_to_new[old_id]}
            for old_id, row in rows.items()
        }

    try:
        global_reply = await manager.execute(
            _request(
                "get_user_briefs",
                [*user_ids, user_ids[0], uuid.uuid4()],
            )
        )
        assert global_reply.data.project_id is None
        assert {
            str(user.id): user.model_dump(mode="json")
            for user in global_reply.data.users
        } == expected_map(fixture["responses"]["global"])

        project_reply = await manager.execute(
            _request(
                "get_project_user_briefs",
                [*user_ids, user_ids[1], uuid.uuid4()],
                project_id,
            )
        )
        assert project_reply.data.project_id == project_id
        assert {
            str(user.id): user.model_dump(mode="json")
            for user in project_reply.data.users
        } == expected_map(fixture["responses"]["project"])

        large_ids = [ids["staff"], ids["staff"], *[uuid.uuid4() for _ in range(503)]]
        large_reply = await manager.execute(_request("get_user_briefs", large_ids))
        assert {
            str(user.id): user.model_dump(mode="json")
            for user in large_reply.data.users
        } == expected_map(fixture["responses"]["large"])

        empty_reply = await manager.execute(_request("get_user_briefs", []))
        assert empty_reply.data.users == []
    finally:
        await _cleanup_briefs_world(
            test_engine,
            user_ids=ids.values(),
            project_ids=[project_id],
        )
