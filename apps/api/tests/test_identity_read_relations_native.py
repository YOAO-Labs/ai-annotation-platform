"""Identity projections preserve SQL behavior with private tables inaccessible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib.resources import files
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import Uuid, create_engine, literal, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from yoao_identity.read_relation_contract import identity_read_relation_schema
from app.core.security import create_access_token, decode_access_token
from app.db.models.notification import Notification
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from app.services import comment_attachment_authority as authority
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_read_relations import (
    account_profiles,
    invitation_audit_targets,
    read_account_profile,
    read_account_profiles,
)
from app.services.identity_session_data import IdentitySessionData
from app.services.notification import NotificationService
from app.services.project_aggregates import member_user_ids, membership_project_ids
from tests.factory import create_project
from tests.frozen_namespaces import historical
from tests.test_identity_auth_native import _cleanup, _seed
from tests.test_identity_recovery import _postgres_tool

_ROOT = Path(__file__).parents[3]
_RELATIONS = {
    "account_profiles": [
        "id",
        "name",
        "email",
        "role",
        "is_active",
        "status",
        "avatar_ref",
    ],
    "invitation_audit_targets": ["id", "project_id"],
}
_FUNCTIONS = {
    "read_actor_facts(uuid)",
    "lock_accounts_share(uuid[],boolean)",
    "lock_accounts_update(uuid[],boolean)",
    "lock_accounts_key_share(uuid[],boolean)",
    "check_accepted_api_key(uuid,uuid,text[])",
    "read_accepted_api_key_facts(uuid,uuid)",
    "is_jti_revoked(uuid)",
    "get_credential_generation(uuid)",
}


def test_read_relation_descriptors_match_the_installed_portable_contract():
    expected = identity_read_relation_schema()
    assert (
        json.loads(
            files("yoao_identity").joinpath("read_relations.schema.json").read_text()
        )
        == expected
    )
    assert json.loads(
        (
            _ROOT / "apps/api/alembic/sql/0286_identity_read_relations.schema.json"
        ).read_text()
    ) == historical(expected)
    for name, columns in _RELATIONS.items():
        relation = expected["x-postgresql-relations"][f"yoao_identity_v1.{name}"]
        assert relation["columns"] == columns


@pytest.mark.asyncio
async def test_relation_enumeration_is_limited_to_required_owner_columns(
    test_engine,
):
    if os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("requires the verifier-owned database and roles")
    roles = (
        "workspace_code",
        "assets_code",
        "labeling_code",
        "jobs_code",
        "learning_code",
        "inference_code",
        "insights_code",
        "diagnostics_code",
        "export_code",
        "audit_code",
        "settings_code",
        "notifications_code",
        "yoao_identity_auth_client",
        "yoao_identity_key_client",
        "yoao_append_client",
    )
    async with test_engine.connect() as db:
        for relation in _RELATIONS:
            name = f"yoao_identity_v1.{relation}"
            for role in roles:
                table, column, readable_columns = (
                    await db.execute(
                        text(
                            "SELECT has_table_privilege(CAST(:role AS name),CAST(:relation AS regclass),'SELECT'),"
                            "has_any_column_privilege(CAST(:role AS name),CAST(:relation AS regclass),'SELECT'), "
                            "ARRAY(SELECT attribute.attname FROM pg_catalog.pg_attribute AS attribute "
                            "WHERE attribute.attrelid=CAST(:relation AS regclass) "
                            "AND attribute.attnum>0 AND NOT attribute.attisdropped "
                            "AND pg_catalog.has_column_privilege(CAST(:role AS name), "
                            "CAST(:relation AS regclass),attribute.attname,'SELECT'))"
                        ),
                        {"role": role, "relation": name},
                    )
                ).one()
                required = {
                    ("notifications_code", "account_profiles"): {
                        "id",
                        "role",
                        "is_active",
                    },
                    ("audit_code", "account_profiles"): {"id", "email"},
                }.get((role, relation), set())
                assert table is False and column is bool(required), (role, name)
                assert set(readable_columns) == required, (role, name, readable_columns)
        await db.rollback()
        for role in roles:
            await db.execute(text(f"SET LOCAL ROLE {role}"))
            with pytest.raises(DBAPIError) as denied:
                await db.execute(select(account_profiles.c.name).limit(0))
            assert getattr(denied.value.orig, "sqlstate", None) == "42501"
            await db.rollback()
        await db.execute(text("SET LOCAL ROLE identity_code"))
        await db.execute(select(account_profiles.c.id).limit(0))
        await db.execute(select(invitation_audit_targets.c.id).limit(0))
        await db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("many", [False, True])
async def test_profile_adapter_accepts_a_real_sqlalchemy_row_mapping(many):
    identity = uuid.uuid4()
    values = {
        "id": identity,
        "name": "Mapping result",
        "email": "mapping@example.test",
        "role": "employee",
        "is_active": True,
        "status": "offline",
        "avatar_ref": None,
    }
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            result = connection.execute(
                select(
                    *[
                        literal(value, type_=Uuid() if field == "id" else None).label(
                            field
                        )
                        for field, value in values.items()
                    ]
                )
            )
            db = SimpleNamespace(
                autoflush=False, execute=AsyncMock(return_value=result)
            )
            if many:
                (row,) = await read_account_profiles(db, select(account_profiles))
            else:
                row = await read_account_profile(db, identity)
            assert row.model_dump() == values
    finally:
        engine.dispose()


def _grants(test_engine, filename, **variables):
    _postgres_tool(
        test_engine,
        [
            "psql",
            "-U",
            "owner",
            "-d",
            test_engine.url.database,
            "-v",
            f"expected_database={test_engine.url.database}",
            "-v",
            "migration_role=owner",
            *[
                part
                for key, value in variables.items()
                for part in ("-v", f"{key}={value}")
            ],
        ],
        body=(_ROOT / "infra/docker" / filename).read_bytes(),
    )


@pytest.fixture
async def restricted_identity_consumer(test_engine):
    if os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("requires the verifier-owned database and LOGIN roles")
    _postgres_tool(
        test_engine,
        [
            "psql",
            "-U",
            "owner",
            "-d",
            test_engine.url.database,
            "-v",
            "ON_ERROR_STOP=1",
        ],
        body=b"GRANT SELECT(password_hash), UPDATE(name) ON users TO legacy_runtime;",
    )
    engine = create_async_engine(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    try:
        _grants(
            test_engine,
            "identity-consumer-grants.sql",
            identity_owner="identity_code",
            consumer_role="legacy_runtime",
        )
        _grants(
            test_engine,
            "identity-consumer-grants.sql",
            identity_owner="identity_code",
            consumer_role="legacy_runtime",
        )
        yield engine
    finally:
        await engine.dispose()
        _grants(
            test_engine,
            "lan-production-grants.sql",
            application_role="legacy_runtime",
            asset_cleanup_role="yoao_asset_cleanup_client",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {"expected_database": "wrong_identity_consumer_test"},
        {"migration_role": "legacy_runtime"},
        {"consumer_role": "identity_code"},
        {"identity_owner": "workspace_code"},
    ],
)
async def test_consumer_grants_reject_wrong_ownership_before_changing_acl(
    test_engine, invalid
):
    if os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("requires the verifier-owned database and LOGIN roles")
    query = text(
        "SELECT has_table_privilege('legacy_runtime','public.users','SELECT'),"
        "has_any_column_privilege('legacy_runtime','public.users','UPDATE'),"
        "has_function_privilege('legacy_runtime','yoao_identity_v1.authentication_policy()','EXECUTE')"
    )
    async with test_engine.connect() as observer:
        before = (await observer.execute(query)).one()
        assert before == (False, False, True)
        with pytest.raises(AssertionError):
            _grants(
                test_engine,
                "identity-consumer-grants.sql",
                **{
                    "identity_owner": "identity_code",
                    "consumer_role": "legacy_runtime",
                    **invalid,
                },
            )
        assert (await observer.execute(query)).one() == before


@pytest.mark.asyncio
async def test_views_keep_raw_fields_nullable_targets_and_identity_ownership(
    test_engine,
):
    fixture = await _seed(
        test_engine, active=False, role="historic_reviewer", create_key=False
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    invitation_id = uuid.uuid4()
    try:
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE users SET avatar_ref='avatars/raw-ref' WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            await db.execute(
                text(
                    "UPDATE identity_account_presence SET status='online' WHERE user_id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            db.add(
                UserInvitation(
                    id=invitation_id,
                    email=f"{invitation_id}@example.test",
                    role="employee",
                    invited_by=fixture["actor_id"],
                    project_id=None,
                    token=uuid.uuid4().hex,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        async with maker() as db:
            row = await read_account_profile(db, fixture["actor_id"])
            assert row is not None
            assert (row.is_active, row.role, row.status, row.avatar_ref) == (
                False,
                "historic_reviewer",
                "online",
                "avatars/raw-ref",
            )
            assert (
                await db.scalar(
                    select(invitation_audit_targets.c.project_id).where(
                        invitation_audit_targets.c.id == invitation_id
                    )
                )
                is None
            )
            assert (
                await db.execute(
                    select(invitation_audit_targets.c.id).where(
                        invitation_audit_targets.c.id == invitation_id
                    )
                )
            ).scalars().all() == [invitation_id]
            for view in ("account_profiles", "invitation_audit_targets"):
                barrier, owner = (
                    await db.execute(
                        text(
                            "SELECT reloptions @> ARRAY['security_barrier=true'],pg_get_userbyid(relowner) "
                            "FROM pg_class WHERE oid=CAST(:view AS regclass)"
                        ),
                        {"view": f"yoao_identity_v1.{view}"},
                    )
                ).one()
                assert barrier
                if os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1":
                    assert owner == "identity_code"
    finally:
        async with maker.begin() as db:
            await db.execute(
                UserInvitation.__table__.delete().where(
                    UserInvitation.id == invitation_id
                )
            )
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("many", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
async def test_profile_reads_honor_pending_work_no_autoflush_and_caller_rollback(
    test_engine, many, autoflush
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    identity = uuid.uuid4()

    async def read(db):
        if many:
            return await read_account_profiles(
                db, select(account_profiles).where(account_profiles.c.id == identity)
            )
        return await read_account_profile(db, identity)

    async with maker() as db:
        db.add(
            User(
                id=identity,
                name="Pending profile",
                email=f"{identity}@example.test",
                password_hash="private",
            )
        )
        with db.no_autoflush:
            assert not await read(db)
        db.autoflush = autoflush
        value = await read(db)
        assert bool(value) is autoflush
        if value:
            row = value[0] if many else value
            assert row.id == identity and row.name == "Pending profile"
        assert db.in_transaction()
        await db.rollback()
    async with maker() as observer:
        assert await read_account_profile(observer, identity) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ"])
async def test_restricted_profile_reads_follow_the_callers_snapshot(
    test_engine, restricted_identity_consumer, isolation
):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    reader = async_sessionmaker(
        restricted_identity_consumer.execution_options(isolation_level=isolation)
    )
    try:
        async with reader() as db:
            first = await read_account_profile(db, fixture["actor_id"])
            assert first is not None
            async with maker.begin() as writer:
                await writer.execute(
                    text("UPDATE users SET name='Changed by Identity' WHERE id=:id"),
                    {"id": first.id},
                )
            second = await read_account_profile(db, first.id)
            assert second.name == (
                first.name if isolation == "REPEATABLE READ" else "Changed by Identity"
            )
            await db.rollback()
            assert (
                await read_account_profile(db, first.id)
            ).name == "Changed by Identity"
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_independent_consumer_retains_only_approved_reads_and_transactional_capabilities(
    test_engine, restricted_identity_consumer
):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(restricted_identity_consumer, expire_on_commit=False)
    try:
        async with maker() as db:
            assert await db.scalar(text("SELECT current_user")) == "legacy_runtime"
            facts = await IdentityAccountLockData(db).read_actor_status(
                fixture["actor_id"]
            )
            assert facts.id == fixture["actor_id"]
            for mode in ("share", "update", "key_share"):
                locked = await IdentityAccountLockData(db).lock_accounts(
                    [facts.id], mode=mode, nowait=True
                )
                assert locked[facts.id].id == facts.id
            assert await IdentityAccountLockData(db).accepted_api_key_authorized(
                facts.id, fixture["key_id"], frozenset({"datasets:read"})
            )
            functions = set(
                (
                    await db.execute(
                        text(
                            "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname ~ '^(aap|yoao)_identity(_[a-z0-9]+)*_v[0-9]+$' "
                            "AND has_function_privilege(current_user,p.oid,'EXECUTE')"
                        )
                    )
                ).scalars()
            )
            assert functions == {f"yoao_identity_v1.{name}" for name in _FUNCTIONS}
            await db.rollback()
            statements = [
                "SELECT id FROM public.users",
                "SELECT password_hash FROM public.users",
                "SELECT token FROM public.user_invitations",
                "SELECT key_hash FROM public.api_keys",
                "SELECT credential_generation FROM public.users",
                "SELECT jti FROM public.identity_revoked_jtis",
                "UPDATE users SET name='bad' WHERE false",
                "DELETE FROM users WHERE false",
                "INSERT INTO users DEFAULT VALUES",
                "SELECT yoao_identity_v1.authentication_policy()",
                "SELECT yoao_identity_v1.advance_credential_generation(NULL::uuid)",
                "SELECT yoao_identity_v1.revoke_jti(NULL::uuid,now())",
                "SELECT yoao_identity_v1.retire_account_credentials(NULL::uuid)",
            ]
            for statement in statements:
                with pytest.raises(DBAPIError) as denied:
                    await db.execute(text(statement))
                assert getattr(denied.value.orig, "sqlstate", None) == "42501", (
                    statement
                )
                await db.rollback()
            for view in _RELATIONS:
                relation = f"yoao_identity_v1.{view}"
                assert not await db.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, :view, "
                        "'INSERT,UPDATE,DELETE') OR "
                        "has_any_column_privilege(current_user, :view, 'INSERT,UPDATE')"
                    ),
                    {"view": relation},
                )
                for statement in (
                    f"INSERT INTO {relation} DEFAULT VALUES",
                    f"UPDATE {relation} SET id=id WHERE false",
                    f"DELETE FROM {relation} WHERE false",
                ):
                    with pytest.raises(DBAPIError):
                        await db.execute(text(statement))
                    await db.rollback()
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["jti", "generation"])
async def test_consumer_reauthorization_rejects_revoked_jwt(
    test_engine, restricted_identity_consumer, revocation
):
    fixture = await _seed(test_engine, create_key=False)
    token = create_access_token(subject=str(fixture["actor_id"]), role="employee")
    jti = uuid.UUID(decode_access_token(token)["jti"])
    request = Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    )
    reader = async_sessionmaker(restricted_identity_consumer, expire_on_commit=False)
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with reader() as db:
            actor = await read_account_profile(db, fixture["actor_id"])
            _, proof = await authority.lock_authority(db, actor, request)
            await authority.reauthorize(db, proof)
            await db.rollback()
        async with owner.begin() as db:
            data = IdentitySessionData(db)
            if revocation == "jti":
                assert await data.revoke_jti(
                    jti, datetime.now(UTC) + timedelta(hours=1)
                )
            else:
                assert (
                    await data.advance_credential_generation(fixture["actor_id"]) == 1
                )
        async with reader() as db:
            with pytest.raises(HTTPException) as denied:
                await authority.reauthorize(db, proof)
            assert denied.value.status_code == 403
            assert denied.value.detail == {
                "code": "comment_attachment_authority_revoked"
            }
            await db.rollback()
    finally:
        await _cleanup(test_engine, fixture, jtis=[jti])


@pytest.mark.asyncio
async def test_restricted_set_and_delivery_queries_keep_role_pairing_and_inactive_scope(
    test_engine, restricted_identity_consumer
):
    fixtures = []
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    project_id = None
    try:
        for role, active in [
            ("project_admin", True),
            ("employee", True),
            ("employee", False),
            ("viewer", True),
            ("viewer", True),
            ("historic_reviewer", True),
            ("super_admin", True),
            ("super_admin", False),
        ]:
            fixtures.append(
                await _seed(test_engine, role=role, active=active, create_key=False)
            )
        ids = [row["actor_id"] for row in fixtures]
        async with maker.begin() as setup:
            project = await create_project(setup, owner_id=ids[0])
            project_id = project.id
            for identity, role in zip(
                ids[1:6], ["annotator", "reviewer", "viewer", "annotator", "reviewer"]
            ):
                setup.add(
                    ProjectMember(project_id=project.id, user_id=identity, role=role)
                )
        reader = async_sessionmaker(
            restricted_identity_consumer, expire_on_commit=False
        )
        async with reader() as db:
            assert set((await db.execute(member_user_ids(project_id))).scalars()) == {
                ids[1],
                ids[3],
            }
            for index in range(1, 6):
                actor = SimpleNamespace(id=ids[index])
                result = (
                    (await db.execute(membership_project_ids(actor))).scalars().all()
                )
                assert result == ([project_id] if index in (1, 3) else [])
            rows = [Notification(user_id=identity) for identity in ids]
            service = NotificationService(db)
            assert await service._allowed_delivery_indices(
                rows, [(True, project_id, False)] * len(ids)
            ) == {0, 1, 3, 6}
            assert await service._allowed_delivery_indices(
                rows, [(True, project_id, True)] * len(ids)
            ) == {0, 6}
    finally:
        if project_id is not None:
            async with maker.begin() as cleanup:
                await cleanup.execute(
                    Project.__table__.delete().where(Project.id == project_id)
                )
        for fixture in fixtures:
            await _cleanup(test_engine, fixture)
