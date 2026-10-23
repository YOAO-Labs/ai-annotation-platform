"""Native PostgreSQL acceptance for per-worktree embedded runtime roles."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.deps import get_db
from app.main import app
from app.schemas.user import UserOut
from app.services.identity_gateway import IdentityGateway, get_identity_gateway
from tests.factory import DEFAULT_PASSWORD, create_user
from tests.test_identity_recovery import _postgres_tool
from tests.test_asset_storage_cleanup_runtime import (
    _verified_resource_manifest,
    _verify_postgres,
)

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from worktree_env import WorktreeError, atomic_json, load_identity, state_path, topology  # noqa: E402
from worktree_resources import LocalResources  # noqa: E402

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable PostgreSQL verifier",
    ),
]


def _render(url) -> str:
    return make_url(url).render_as_string(hide_password=False)


def _backend(
    tmp_path,
    test_engine,
    *,
    database_url=None,
    migration_url=None,
    root=None,
    mode="test",
):
    owner_url = _render(test_engine.url)
    root = root or tmp_path / f"worktree-{uuid.uuid4().hex}"
    resources = topology(root, load_identity(root, create=True), mode)
    configured = settings.model_copy(
        update={
            "database_url": database_url or owner_url,
            "migration_database_url": migration_url or owner_url,
        }
    )
    return LocalResources(resources, configured, code_root=ROOT)


def _credentials_path(backend) -> Path:
    return state_path(
        backend.root, backend.resources["mode"], "runtime-credentials.json"
    )


def _write_credentials(backend, password: str) -> dict:
    credentials = {
        **backend._runtime_credential_identity(),
        "password": password,
    }
    atomic_json(_credentials_path(backend), credentials)
    return credentials


async def _role_row(engine, role: str) -> dict | None:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT r.rolname, r.rolcanlogin, r.rolsuper, r.rolinherit, "
                        "r.rolcreatedb, r.rolcreaterole, r.rolreplication, "
                        "r.rolbypassrls, r.rolconfig, r.rolvaliduntil, "
                        "shobj_description(r.oid, 'pg_authid') AS owner "
                        "FROM pg_catalog.pg_roles r WHERE r.rolname=:role"
                    ),
                    {"role": role},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


async def _assert_role_absent(engine, role: str) -> None:
    assert await _role_row(engine, role) is None


async def _catalog_membership(engine, member: str, role: str) -> tuple | None:
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT m.admin_option,m.inherit_option,m.set_option "
                    "FROM pg_catalog.pg_auth_members m "
                    "JOIN pg_catalog.pg_roles member ON member.oid=m.member "
                    "JOIN pg_catalog.pg_roles granted ON granted.oid=m.roleid "
                    "WHERE member.rolname=:member AND granted.rolname=:role"
                ),
                {"member": member, "role": role},
            )
        ).one_or_none()
        return tuple(row) if row is not None else None


async def _create_role(
    engine, role: str, *, comment: str, password: str, attributes: str
) -> None:
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", password)
    assert re.fullmatch(r"[A-Z ]+", attributes)
    owner = create_async_engine(engine.url, hide_parameters=True)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    "SELECT set_config('aap.test_role_name',:role,true),"
                    "set_config('aap.test_role_password',:password,true),"
                    "set_config('aap.test_role_comment',:comment,true),"
                    "set_config('aap.test_role_attributes',:attributes,true)"
                ),
                {
                    "role": role,
                    "password": password,
                    "comment": comment,
                    "attributes": attributes,
                },
            )
            await connection.exec_driver_sql(
                """DO $create_test_role$
                BEGIN
                    EXECUTE format('CREATE ROLE %I %s PASSWORD %L',
                        current_setting('aap.test_role_name'),
                        current_setting('aap.test_role_attributes'),
                        current_setting('aap.test_role_password'));
                    EXECUTE format('COMMENT ON ROLE %I IS %L',
                        current_setting('aap.test_role_name'),
                        current_setting('aap.test_role_comment'));
                END;
                $create_test_role$;"""
            )
    finally:
        await owner.dispose()


async def _drop_role(engine, role: str, *, revoke_connect: bool = False) -> None:
    quoted = engine.dialect.identifier_preparer.quote_identifier(role)
    async with engine.begin() as connection:
        if revoke_connect:
            await connection.exec_driver_sql(
                f"REVOKE CONNECT ON DATABASE postgres FROM {quoted}"
            )
        await connection.exec_driver_sql(f"DROP ROLE IF EXISTS {quoted}")


async def _assert_private_identity_denied(engine, role: str) -> None:
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == role
        assert await connection.scalar(text("SELECT session_user")) == role
        leaked = (
            (
                await connection.execute(
                    text(
                        "SELECT n.nspname || '.' || c.relname "
                        "FROM pg_catalog.pg_class c "
                        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE c.relkind IN ('r','p','v','m','f') AND ("
                        "(n.nspname='public' AND (c.relname IN ("
                        "'users','api_keys','groups','organizations','organization_members',"
                        "'user_invitations','password_reset_tokens','email_verification_tokens') "
                        "OR left(c.relname,9)='identity_')) OR ("
                        "n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$' "
                        "AND c.relkind <> 'v')) AND ("
                        "has_table_privilege(current_user,c.oid,"
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                        "has_any_column_privilege(current_user,c.oid,"
                        "'SELECT,INSERT,UPDATE,REFERENCES'))"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert leaked == []
        await connection.execute(
            text("SELECT * FROM aap_identity_v1.account_profiles LIMIT 0")
        )
        await connection.execute(
            text("SELECT * FROM aap_identity_v1.invitation_audit_targets LIMIT 0")
        )
        await connection.execute(
            text("SELECT * FROM aap_identity_fixture_v1.account_profiles LIMIT 0")
        )

    for statement in (
        "SELECT password_hash FROM public.users LIMIT 0",
        "UPDATE public.users SET name=name WHERE false",
        "DELETE FROM public.user_invitations WHERE false",
        "INSERT INTO public.users DEFAULT VALUES",
        "SELECT * FROM public.identity_revoked_jtis LIMIT 0",
    ):
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as denied:
                await connection.execute(text(statement))
            assert getattr(denied.value.orig, "sqlstate", None) == "42501"
            await connection.rollback()


def _identity_secret_fields() -> tuple[str, ...]:
    fields = type(settings).model_fields
    names = [
        name.upper()
        for name in fields
        if name.startswith("identity_")
        and (name.endswith("_database_url") or name.endswith("_database_url_file"))
    ]
    names.extend(
        name.upper()
        for name in (
            "identity_url",
            "identity_token",
            "identity_recovery_mail_key",
            "identity_recovery_mail_key_file",
            "notification_publish_database_url",
            "notification_publish_database_url_file",
        )
        if name in fields
    )
    return tuple(dict.fromkeys(names))


@asynccontextmanager
async def _embedded_client(engine, monkeypatch):
    for name in _identity_secret_fields():
        field = name.lower()
        monkeypatch.setattr(settings, field, "")
    monkeypatch.setattr(settings, "database_url", _render(engine.url))
    monkeypatch.setattr(settings, "migration_database_url", "")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def request_db():
        async with maker() as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_db, request_db)
    gateway = IdentityGateway(signing_secret=settings.secret_key)
    monkeypatch.setitem(app.dependency_overrides, get_identity_gateway, lambda: gateway)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worktree-runtime-role.test",
    ) as client:
        yield client


async def _exercise_embedded_http(client, actor, target) -> None:
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": actor.email, "password": DEFAULT_PASSWORD},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    profile = await client.get("/api/v1/auth/me", headers=headers)
    assert profile.status_code == 200, profile.text
    assert set(profile.json()) == set(UserOut.model_fields)

    updated = await client.patch(
        "/api/v1/auth/me", headers=headers, json={"name": "Worktree runtime"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Worktree runtime"

    users = await client.get("/api/v1/users", headers=headers)
    assert users.status_code == 200, users.text
    assert str(target.id) in {row["id"] for row in users.json()}

    role = await client.patch(
        f"/api/v1/users/{target.id}/role",
        headers=headers,
        json={"role": "project_admin"},
    )
    assert role.status_code == 200, role.text
    assert role.json()["role"] == "project_admin"

    reset = await client.post(
        f"/api/v1/users/{target.id}/admin-reset-password", headers=headers
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["target_email"] == target.email

    inactive = await client.post(
        f"/api/v1/users/{target.id}/deactivate", headers=headers
    )
    assert inactive.status_code == 200 and not inactive.json()["is_active"], (
        inactive.text
    )
    active = await client.post(f"/api/v1/users/{target.id}/reactivate", headers=headers)
    assert active.status_code == 200 and active.json()["is_active"], active.text

    preview = await client.get(
        f"/api/v1/users/{target.id}/offboarding-preview", headers=headers
    )
    assert preview.status_code == 200, preview.text
    offboarded = await client.post(
        f"/api/v1/users/{target.id}/offboarding",
        headers=headers,
        json={"preview_version": preview.json()["preview_version"], "projects": []},
    )
    assert offboarded.status_code == 200, offboarded.text

    deleted = await client.delete(f"/api/v1/users/{target.id}", headers=headers)
    assert deleted.status_code == 200 and not deleted.json()["is_active"], deleted.text
    assert (
        await client.post("/api/v1/auth/logout", headers=headers)
    ).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def _seed_accounts(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker.begin() as session:
        actor = await create_user(
            session,
            "super_admin",
            f"worktree-role-admin-{uuid.uuid4().hex}@example.test",
            "Worktree administrator",
        )
        target = await create_user(
            session,
            "employee",
            f"worktree-role-target-{uuid.uuid4().hex}@example.test",
            "Worktree target",
        )
    return actor, target


@pytest.mark.asyncio
async def test_same_owner_child_prepare_http_and_destroy_use_owned_runtime_login(
    test_engine, tmp_path, monkeypatch, record_property
):
    _verify_postgres(test_engine.url)
    backend = _backend(tmp_path, test_engine)
    owner = backend.resources["owner"]
    credentials_path = _credentials_path(backend)
    owner_engine = None
    runtime_engine = None
    try:
        await asyncio.to_thread(backend.prepare)
        credentials = backend._runtime_credentials(required=True)
        assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o600
        assert credentials["owner"] == owner
        assert credentials["role"] == backend.resources["database"] + "_app"
        assert credentials["postgres"]["migration_role"] == backend.url.username
        assert backend.runtime_url().username == credentials["role"]
        assert hmac.compare_digest(
            backend.runtime_url().password or "", credentials["password"]
        )

        runtime_engine = create_async_engine(
            backend.runtime_url(), hide_parameters=True
        )
        await _assert_private_identity_denied(runtime_engine, credentials["role"])
        assert await backend.verify_runtime_login()

        child_environment = backend._environment(6397)
        assert (
            make_url(child_environment["DATABASE_URL"]).username == credentials["role"]
        )
        assert (
            make_url(child_environment["MIGRATION_DATABASE_URL"]).username
            == backend.url.username
        )
        assert (
            child_environment["TEST_DATABASE_URL"]
            == child_environment["MIGRATION_DATABASE_URL"]
        )
        for key in _identity_secret_fields():
            monkeypatch.setenv(
                key, "postgresql+asyncpg://shared:secret@localhost/shared"
            )
        inherited_environment = backend._environment(6397)
        assert all(
            inherited_environment[key] == "" for key in _identity_secret_fields()
        )

        from worktree_env import application_environment, frontend_environment

        api_environment = application_environment(inherited_environment)
        assert make_url(api_environment["DATABASE_URL"]).username == credentials["role"]
        assert hmac.compare_digest(
            make_url(api_environment["DATABASE_URL"]).password or "",
            credentials["password"],
        )
        assert api_environment["MIGRATION_DATABASE_URL"] == ""
        assert api_environment["TEST_DATABASE_URL"] == ""
        assert "DATABASE_URL" not in frontend_environment(inherited_environment)
        assert "MIGRATION_DATABASE_URL" not in frontend_environment(
            inherited_environment
        )

        previous_password = credentials["password"]
        from worktree_runtime import stop_environment

        await asyncio.to_thread(stop_environment, backend)
        assert credentials_path.exists()
        assert backend.runtime_url().username == credentials["role"]
        reopened = _backend(
            tmp_path,
            test_engine,
            database_url=_render(test_engine.url),
            migration_url=_render(test_engine.url),
            root=backend.root,
        )
        await asyncio.to_thread(reopened.prepare, skip_migrations=True)
        assert hmac.compare_digest(
            reopened._runtime_credentials(required=True)["password"], previous_password
        )
        await runtime_engine.dispose()
        runtime_engine = create_async_engine(
            reopened.runtime_url(), hide_parameters=True
        )
        await _assert_private_identity_denied(runtime_engine, credentials["role"])

        owner_engine = create_async_engine(reopened.url, hide_parameters=True)
        actor, target = await _seed_accounts(owner_engine)
        async with _embedded_client(runtime_engine, monkeypatch) as client:
            await _exercise_embedded_http(client, actor, target)

        state_query = text(
            "SELECT id,name,role,is_active,status,credential_generation FROM users "
            "WHERE id=ANY(:ids) ORDER BY id"
        )
        async with owner_engine.connect() as db:
            before_restore = (
                await db.execute(state_query, {"ids": [actor.id, target.id]})
            ).all()
            revoked_before = await db.scalar(
                text("SELECT count(*) FROM identity_revoked_jtis")
            )
            assert revoked_before > 0
        snapshot = tmp_path / "owned-runtime.dump"
        snapshot.touch(mode=0o600)
        dump = await asyncio.to_thread(
            _postgres_tool,
            owner_engine,
            ["pg_dump", "-U", "owner", "-Fc", reopened.resources["database"]],
        )
        assert dump.startswith(b"PGDMP")
        snapshot.write_bytes(dump)
        record_property(
            "worktree_runtime_backup_sha256", hashlib.sha256(dump).hexdigest()
        )
        await runtime_engine.dispose()
        await owner_engine.dispose()
        await reopened.owned_runtime_role("check_drop")
        # Preserve the global LOGIN and its sidecar while restoring its owned DB.
        await reopened.database("drop")
        assert hmac.compare_digest(
            reopened._runtime_credentials(required=True)["password"], previous_password
        )
        await reopened.database("create")
        await asyncio.to_thread(
            _postgres_tool,
            test_engine,
            [
                "pg_restore",
                "-U",
                "owner",
                "--exit-on-error",
                "--dbname",
                reopened.resources["database"],
            ],
            body=snapshot.read_bytes(),
        )
        assert await reopened.verify_runtime_login()
        runtime_engine = create_async_engine(
            reopened.runtime_url(), hide_parameters=True
        )
        owner_engine = create_async_engine(reopened.url, hide_parameters=True)
        await _assert_private_identity_denied(runtime_engine, credentials["role"])
        async with owner_engine.connect() as db:
            assert (
                await db.execute(state_query, {"ids": [actor.id, target.id]})
            ).all() == before_restore
            assert (
                await db.scalar(text("SELECT count(*) FROM identity_revoked_jtis"))
                == revoked_before
            )
        async with _embedded_client(runtime_engine, monkeypatch) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": actor.email, "password": DEFAULT_PASSWORD},
            )
            assert login.status_code == 200, login.text
            profile = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": "Bearer " + login.json()["access_token"]},
            )
            assert profile.status_code == 200 and profile.json()["id"] == str(
                actor.id
            ), profile.text
        record_property(
            "worktree_runtime_backup_restore",
            "same database, role and secret; data, revocations, ACL and HTTP verified",
        )
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()
        if owner_engine is not None:
            await owner_engine.dispose()
        resources_path = state_path(
            backend.root, backend.resources["mode"], "resources.json"
        )
        if credentials_path.exists() or resources_path.exists():
            await asyncio.to_thread(backend.destroy, backend.resources["confirmation"])

    async with test_engine.connect() as observer:
        assert (
            await observer.scalar(
                text("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=:role"),
                {"role": credentials["role"]},
            )
            is None
        )
        assert (
            await observer.scalar(
                text("SELECT 1 FROM pg_catalog.pg_database WHERE datname=:database"),
                {"database": backend.resources["database"]},
            )
            is None
        )
    assert not credentials_path.exists()


@pytest.mark.asyncio
async def test_distinct_runtime_profile_is_never_replaced_by_owned_login(
    test_engine, tmp_path
):
    _verify_postgres(test_engine.url)
    owner_url = make_url(test_engine.url)
    distinct_url = owner_url.set(username="already_separate", password="configured")
    backend = _backend(
        tmp_path,
        test_engine,
        database_url=_render(distinct_url),
        migration_url=_render(owner_url),
    )
    assert backend.runtime_url().username == "already_separate"
    assert backend.runtime_url().password == "configured"
    assert await backend.owned_runtime_role("inspect") is None
    assert not _credentials_path(backend).exists()


@pytest.mark.asyncio
async def test_same_owner_without_createrole_fails_before_owned_resources_change(
    test_engine, tmp_path
):
    _verify_postgres(test_engine.url)
    limited = f"worktree_limited_{uuid.uuid4().hex[:12]}"
    password = secrets.token_urlsafe(32)
    async with test_engine.begin() as connection:
        quoted = test_engine.dialect.identifier_preparer.quote_identifier(limited)
        await connection.exec_driver_sql(
            f"CREATE ROLE {quoted} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
            f"NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{password}'"
        )
        await connection.exec_driver_sql(
            f"GRANT CONNECT ON DATABASE postgres TO {quoted}"
        )
    owner_url = make_url(test_engine.url).set(
        database="postgres", username=limited, password=password
    )
    backend = _backend(
        tmp_path,
        test_engine,
        database_url=_render(owner_url),
        migration_url=_render(owner_url),
    )
    credentials_path = _credentials_path(backend)
    manifest_path = state_path(
        backend.root, backend.resources["mode"], "resources.json"
    )
    try:
        with pytest.raises(WorktreeError, match="CREATEROLE"):
            await asyncio.to_thread(backend.prepare)
        assert not credentials_path.exists()
        assert not manifest_path.exists()
        assert await backend.database("inspect") is None
        assert backend.redis_info() is None
        assert all(
            backend.bucket_info(name) is None
            for name in backend.resources["buckets"].values()
        )
    finally:
        await _drop_role(test_engine, limited, revoke_connect=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("comment", ["foreign-resource", "owned-resource"])
async def test_existing_role_without_matching_sidecar_is_never_adopted(
    test_engine, tmp_path, comment
):
    _verify_postgres(test_engine.url)
    backend = _backend(tmp_path, test_engine)
    role = backend.resources["database"] + "_app"
    comment = backend.resources["owner"] if comment == "owned-resource" else comment
    await _create_role(
        test_engine,
        role,
        comment=comment,
        password=secrets.token_urlsafe(32),
        attributes="LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS",
    )
    try:
        with pytest.raises(WorktreeError):
            await backend.owned_runtime_role("create")
        with pytest.raises(WorktreeError):
            await asyncio.to_thread(backend.destroy, backend.resources["confirmation"])
        assert not _credentials_path(backend).exists()
        observed = await _role_row(test_engine, role)
        assert observed is not None and observed["owner"] == comment
    finally:
        await _drop_role(test_engine, role)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["symlink", "permissions", "permissions-exec", "owner", "database"]
)
async def test_invalid_runtime_credential_state_is_rejected_before_role_mutation(
    test_engine, tmp_path, corruption
):
    _verify_postgres(test_engine.url)
    backend = _backend(tmp_path, test_engine)
    path = _credentials_path(backend)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials = {
        **backend._runtime_credential_identity(),
        "password": secrets.token_urlsafe(32),
    }
    if corruption == "owner":
        credentials["owner"] = "foreign-worktree"
    elif corruption == "database":
        credentials["postgres"] = {
            **credentials["postgres"],
            "host": "unowned-postgres-endpoint",
        }
    if corruption == "symlink":
        target = tmp_path / "credentials-target.json"
        atomic_json(target, credentials)
        path.symlink_to(target)
    else:
        atomic_json(path, credentials)
        if corruption == "permissions":
            path.chmod(0o640)
        elif corruption == "permissions-exec":
            path.chmod(0o700)
    try:
        with pytest.raises(WorktreeError):
            backend.runtime_url()
        with pytest.raises(WorktreeError):
            await backend.owned_runtime_role("create")
        role = backend.resources["database"] + "_app"
        assert await _role_row(test_engine, role) is None
    finally:
        if path.is_symlink() or path.exists():
            path.unlink()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attributes",
    [
        "LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
        "LOGIN NOINHERIT NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
    ],
)
async def test_owned_role_rejects_unsafe_postgres_role_attributes(
    test_engine, tmp_path, attributes
):
    _verify_postgres(test_engine.url)
    backend = _backend(tmp_path, test_engine)
    role = backend.resources["database"] + "_app"
    password = secrets.token_urlsafe(32)
    await _create_role(
        test_engine,
        role,
        comment=backend.resources["owner"],
        password=password,
        attributes=attributes,
    )
    _write_credentials(backend, password)
    try:
        with pytest.raises(WorktreeError):
            await backend.owned_runtime_role("inspect")
        observed = await _role_row(test_engine, role)
        assert observed is not None
        assert observed["owner"] == backend.resources["owner"]
    finally:
        await _drop_role(test_engine, role)
        _credentials_path(backend).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_owned_role_create_reuse_and_drop_are_real_postgres_operations(
    test_engine, tmp_path
):
    _verify_postgres(test_engine.url)
    backend = _backend(tmp_path, test_engine)
    path = _credentials_path(backend)
    try:
        created = await backend.owned_runtime_role("create")
        first = backend._runtime_credentials(required=True)
        assert created["owner"] == backend.resources["owner"]
        assert hmac.compare_digest(
            first["password"], backend.runtime_url().password or ""
        )
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert await backend.owned_runtime_role("inspect") is not None
        await backend.database("create")
        quoted_role = test_engine.dialect.identifier_preparer.quote_identifier(
            first["role"]
        )
        quoted_database = test_engine.dialect.identifier_preparer.quote_identifier(
            backend.resources["database"]
        )
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_role}"
            )
        second_backend = _backend(tmp_path, test_engine, root=backend.root)
        reused = await second_backend.owned_runtime_role("create")
        assert reused is not None
        assert hmac.compare_digest(
            second_backend._runtime_credentials(required=True)["password"],
            first["password"],
        )
        assert await second_backend.verify_runtime_login()
        await second_backend.owned_runtime_role("check_drop")
        await second_backend.database("drop")
        await second_backend.owned_runtime_role("drop")
        assert await _role_row(test_engine, first["role"]) is None
    finally:
        if await backend.database("inspect"):
            await backend.database("drop")
        role = backend.resources["database"] + "_app"
        if await _role_row(test_engine, role):
            await _drop_role(test_engine, role)
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["runtime_member", "foreign_member"])
async def test_owned_role_rejects_outgoing_and_foreign_incoming_memberships(
    test_engine, tmp_path, membership
):
    _verify_postgres(test_engine.url)
    backend = _backend(tmp_path, test_engine)
    role = backend.resources["database"] + "_app"
    foreign = f"{role}_member"
    password = secrets.token_urlsafe(32)
    await _create_role(
        test_engine,
        role,
        comment=backend.resources["owner"],
        password=password,
        attributes="LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS",
    )
    _write_credentials(backend, password)
    try:
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"CREATE ROLE {test_engine.dialect.identifier_preparer.quote_identifier(foreign)} NOLOGIN"
            )
            if membership == "runtime_member":
                await connection.exec_driver_sql(
                    f"GRANT {test_engine.dialect.identifier_preparer.quote_identifier(foreign)} "
                    f"TO {test_engine.dialect.identifier_preparer.quote_identifier(role)}"
                )
            else:
                await connection.exec_driver_sql(
                    f"GRANT {test_engine.dialect.identifier_preparer.quote_identifier(role)} "
                    f"TO {test_engine.dialect.identifier_preparer.quote_identifier(foreign)}"
                )
        with pytest.raises(WorktreeError):
            await backend.owned_runtime_role("inspect")
    finally:
        await _drop_role(test_engine, role)
        await _drop_role(test_engine, foreign)
        _credentials_path(backend).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_owned_role_rejects_external_database_dependency(test_engine, tmp_path):
    _verify_postgres(test_engine.url)
    backend = _backend(tmp_path, test_engine)
    role = backend.resources["database"] + "_app"
    await backend.owned_runtime_role("create")
    try:
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(
                "GRANT CONNECT ON DATABASE postgres TO "
                + test_engine.dialect.identifier_preparer.quote_identifier(role)
            )
        with pytest.raises(WorktreeError):
            await backend.owned_runtime_role("check_drop")
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(
                "REVOKE CONNECT ON DATABASE postgres FROM "
                + test_engine.dialect.identifier_preparer.quote_identifier(role)
            )
    finally:
        await backend.owned_runtime_role("drop")
        _credentials_path(backend).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_owned_role_refuses_drop_while_a_real_runtime_session_is_open(
    test_engine, tmp_path
):
    _verify_postgres(test_engine.url)
    backend = _backend(tmp_path, test_engine)
    role = backend.resources["database"] + "_app"
    credentials_path = _credentials_path(backend)
    runtime_engine = None
    try:
        await backend.owned_runtime_role("create")
        await backend.database("create")
        quoted_role = test_engine.dialect.identifier_preparer.quote_identifier(role)
        quoted_database = test_engine.dialect.identifier_preparer.quote_identifier(
            backend.resources["database"]
        )
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_role}"
            )
        runtime_engine = create_async_engine(
            backend.runtime_url(), hide_parameters=True
        )
        async with runtime_engine.connect() as runtime_connection:
            assert await runtime_connection.scalar(text("SELECT current_user")) == role
            with pytest.raises(WorktreeError):
                await backend.owned_runtime_role("check_drop")
        await runtime_engine.dispose()
        await backend.owned_runtime_role("check_drop")
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()
        if await backend.database("inspect"):
            await backend.database("drop")
        if await _role_row(test_engine, role):
            await backend.owned_runtime_role("drop")
        credentials_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_create_recovery_reuses_secret_and_rejects_usable_creator_membership(
    test_engine, tmp_path
):
    _verify_postgres(test_engine.url)
    creator = f"worktree_creator_{uuid.uuid4().hex[:12]}"
    creator_password = secrets.token_urlsafe(32)
    async with test_engine.begin() as connection:
        quoted = test_engine.dialect.identifier_preparer.quote_identifier(creator)
        await connection.exec_driver_sql(
            f"CREATE ROLE {quoted} LOGIN CREATEROLE NOINHERIT NOSUPERUSER "
            f"NOCREATEDB NOREPLICATION NOBYPASSRLS PASSWORD '{creator_password}'"
        )
        await connection.exec_driver_sql(
            f"GRANT CONNECT ON DATABASE postgres TO {quoted}"
        )
        await connection.exec_driver_sql(
            f"ALTER ROLE {quoted} SET createrole_self_grant='set,inherit'"
        )

    owner_url = make_url(test_engine.url).set(
        database="postgres", username=creator, password=creator_password
    )
    backend = _backend(
        tmp_path,
        test_engine,
        database_url=_render(owner_url),
        migration_url=_render(owner_url),
    )
    role = backend.resources["database"] + "_app"
    path = _credentials_path(backend)
    try:
        failure = False
        saved_password = None
        try:
            await backend.owned_runtime_role("create")
        except WorktreeError:
            failure = True
        if failure:
            await _assert_role_absent(test_engine, role)
            assert path.exists()
            saved_password = backend._runtime_credentials(required=True)["password"]
            async with test_engine.begin() as connection:
                await connection.exec_driver_sql(
                    f"ALTER ROLE {test_engine.dialect.identifier_preparer.quote_identifier(creator)} "
                    "RESET createrole_self_grant"
                )
            backend = _backend(
                tmp_path,
                test_engine,
                database_url=_render(owner_url),
                migration_url=_render(owner_url),
                root=backend.root,
            )
            await backend.owned_runtime_role("create")
        credentials = backend._runtime_credentials(required=True)
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", credentials["password"])
        if saved_password is not None:
            assert hmac.compare_digest(credentials["password"], saved_password)
        assert await _catalog_membership(test_engine, creator, role) == (
            True,
            False,
            False,
        )
        await backend.owned_runtime_role("check_drop")
        await backend.owned_runtime_role("drop")
        await _assert_role_absent(test_engine, role)
    finally:
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(
                f"ALTER ROLE {test_engine.dialect.identifier_preparer.quote_identifier(creator)} "
                "RESET createrole_self_grant"
            )
        if await _role_row(test_engine, role):
            await _drop_role(test_engine, role)
        path.unlink(missing_ok=True)
        await _drop_role(test_engine, creator, revoke_connect=True)


async def test_generated_runtime_grants_recheck_memberships_and_roll_back_atomically(
    test_engine, tmp_path
):
    _verify_postgres(test_engine.url)
    legacy = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    resources = _verified_resource_manifest(test_engine, legacy)
    backend = _backend(tmp_path, test_engine, root=Path(resources["root"]))
    path = _credentials_path(backend)
    assert not path.exists()
    role = backend.resources["database"] + "_app"
    foreign = "worktree_grant_" + uuid.uuid4().hex[:12]
    quoted = test_engine.dialect.identifier_preparer.quote_identifier(role)
    foreign_quoted = test_engine.dialect.identifier_preparer.quote_identifier(foreign)
    await backend.owned_runtime_role("create")
    try:
        async with test_engine.begin() as db:
            await db.exec_driver_sql(f"CREATE ROLE {foreign_quoted} NOLOGIN")
            await db.exec_driver_sql(f"GRANT {quoted} TO {foreign_quoted}")
        with pytest.raises(WorktreeError, match="成员资格"):
            await backend.grant_runtime()
        async with test_engine.connect() as db:
            assert not await db.scalar(
                text("SELECT has_table_privilege(:role,'public.projects','SELECT')"),
                {"role": role},
            )
            assert not await db.scalar(
                text(
                    "SELECT has_function_privilege(:role,'aap_identity_v1.authentication_policy()','EXECUTE')"
                ),
                {"role": role},
            )
    finally:
        async with test_engine.begin() as db:
            await db.exec_driver_sql(f"REVOKE {quoted} FROM {foreign_quoted}")
            await db.exec_driver_sql(f"DROP ROLE {foreign_quoted}")
            assert not await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_shdepend WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) "
                    "AND refclassid='pg_authid'::regclass AND refobjid=(SELECT oid FROM pg_roles WHERE rolname=:role) AND deptype='o')"
                ),
                {"role": role},
            )
            await db.exec_driver_sql(f"DROP OWNED BY {quoted}")
        await backend.owned_runtime_role("drop")
        path.unlink(missing_ok=True)


async def test_autovacuum_guard_keeps_real_clients_blocking_database_drop(test_engine):
    _verify_postgres(test_engine.url)
    from verify_worktree_isolation import LiveIsolationTests

    case = LiveIsolationTests(
        "test_autovacuum_does_not_hide_real_clients_or_block_database_drop"
    )
    await asyncio.to_thread(
        case.test_autovacuum_does_not_hide_real_clients_or_block_database_drop
    )
