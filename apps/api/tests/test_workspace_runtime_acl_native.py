"""Default application credentials cannot bypass Workspace owner capabilities."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.project_access import get_workspace_project
from app.services.project_workspace import lock_project_row
from app.services.workspace_e2e_fixtures import create_fixture_project
from app.services.workspace_seed_data import create_media_seed_project
from tests.factory import build_tool_bindings, create_user
from tests.test_asset_storage_cleanup_runtime import (
    _sqlstate,
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_jobs_creation_runtime_native import _lan_runtime
from tests.test_workspace_consumer_profile_native import _grants, _PRIVATE, ROOT
from tests.test_worktree_identity_runtime_role_native import (
    _backend,
    _credentials_path,
    _role_row,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data PostgreSQL verifier",
    ),
]


@asynccontextmanager
async def _runtime(test_engine, tmp_path, grant_path):
    _verify_postgres(test_engine.url)
    if grant_path == "lan":
        engine = await _lan_runtime(test_engine)
        role = engine.url.username

        async def reapply():
            result = await _grants(
                test_engine, ROOT / "infra/docker/lan-production-grants.sql", role
            )
            assert result.returncode == 0, result.stderr

        try:
            yield engine, role, reapply
        finally:
            await engine.dispose()
        return

    manifest_url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    manifest = _verified_resource_manifest(test_engine, manifest_url)
    backend = _backend(tmp_path, test_engine, root=Path(manifest["root"]))
    role = backend.resources["database"] + "_app"
    quoted_role = test_engine.dialect.identifier_preparer.quote_identifier(role)
    credentials = _credentials_path(backend)
    assert await _role_row(test_engine, role) is None
    assert not credentials.exists()
    engine = None
    try:
        await backend.owned_runtime_role("create")
        await backend.grant_runtime()
        engine = create_async_engine(backend.runtime_url(), hide_parameters=True)
        yield engine, role, backend.grant_runtime
    finally:
        if engine is not None:
            await engine.dispose()
        if await _role_row(test_engine, role) is not None:
            async with test_engine.begin() as connection:
                await connection.exec_driver_sql(f"DROP OWNED BY {quoted_role}")
            await backend.owned_runtime_role("drop")
        credentials.unlink(missing_ok=True)


async def _assert_private_storage_denied(engine, role):
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == role
        assert await connection.scalar(text("SELECT session_user")) == role
        for relation in _PRIVATE:
            assert not await connection.scalar(
                text(
                    "SELECT has_table_privilege(current_user,:relation,"
                    "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                    "has_any_column_privilege(current_user,:relation,"
                    "'SELECT,INSERT,UPDATE,REFERENCES')"
                ),
                {"relation": "public." + relation},
            ), relation
        for sequence in ("display_seq_projects", "display_seq_project_templates"):
            assert not await connection.scalar(
                text(
                    "SELECT has_sequence_privilege(current_user,:sequence,"
                    "'USAGE,SELECT,UPDATE')"
                ),
                {"sequence": "public." + sequence},
            )
        assert not await connection.scalar(
            text(
                "SELECT has_schema_privilege(current_user,'aap_workspace_v1','CREATE')"
            )
        )

    for statement in (
        "SELECT id FROM public.projects LIMIT 1",
        "INSERT INTO public.projects DEFAULT VALUES",
        "UPDATE public.projects SET name=name WHERE false",
        "DELETE FROM public.project_members WHERE false",
        "SELECT id FROM public.project_templates LIMIT 1",
        "SELECT * FROM public.project_deletion_operations LIMIT 1",
        "SELECT * FROM aap_workspace_v1.project_catalog_configs LIMIT 1",
        "SELECT nextval('public.display_seq_projects')",
        "UPDATE aap_workspace_v1.project_facts SET name=name WHERE false",
    ):
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as denied:
                await connection.exec_driver_sql(statement)
            assert _sqlstate(denied.value) == "42501", statement
            await connection.rollback()


@pytest.mark.parametrize("grant_path", ["lan", "worktree"])
async def test_default_workspace_credentials_preserve_owner_seed_and_fixture_transactions(
    test_engine, tmp_path, grant_path
):
    async with _runtime(test_engine, tmp_path, grant_path) as (engine, role, _reapply):
        await _assert_private_storage_denied(engine, role)
        owner = async_sessionmaker(test_engine, expire_on_commit=False)
        async with owner.begin() as db:
            actor = await create_user(
                db, "project_admin", f"runtime-{uuid.uuid4()}@example.test", "Owner"
            )
        api = async_sessionmaker(engine, expire_on_commit=False)
        async with api() as db:
            transaction = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            seed_id = await create_media_seed_project(
                db,
                kind="coco8",
                display_id="P-ACL-" + uuid.uuid4().hex[:12],
                name="Default runtime seed",
                owner_id=actor.id,
                tool_bindings=build_tool_bindings(["car"]),
            )
            project = await get_workspace_project(db, project_id=seed_id)
            assert project.name == "Default runtime seed"
            assert (
                await lock_project_row(
                    db, seed_id, nowait=True, populate_existing=False
                )
                is project
            )
            fixture = await create_fixture_project(db, owner_id=actor.id)
            assert fixture.owner_id == actor.id and fixture.id != seed_id
            assert (
                await db.scalar(
                    text(
                        "SELECT name FROM aap_workspace_v1.project_facts WHERE id=:id"
                    ),
                    {"id": seed_id},
                )
                == "Default runtime seed"
            )
            await db.execute(
                text("SELECT * FROM aap_workspace_fixture_v1.templates LIMIT 1")
            )
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == transaction
            async with test_engine.connect() as observer:
                assert (
                    await observer.scalar(
                        text(
                            "SELECT count(*) FROM public.projects WHERE id IN (:seed,:fixture)"
                        ),
                        {"seed": seed_id, "fixture": fixture.id},
                    )
                    == 0
                )
            fixture_id = fixture.id
            await db.rollback()
        async with test_engine.connect() as observer:
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.projects WHERE id IN (:seed,:fixture)"
                    ),
                    {"seed": seed_id, "fixture": fixture_id},
                )
                == 0
            )


@pytest.mark.parametrize("grant_path", ["lan", "worktree"])
async def test_default_reapplication_fences_columns_partition_leaves_and_future_defaults(
    test_engine, tmp_path, grant_path
):
    async with _runtime(test_engine, tmp_path, grant_path) as (engine, role, reapply):
        quoted_role = test_engine.dialect.identifier_preparer.quote_identifier(role)
        suffix = uuid.uuid4().hex[:12]
        parent = f"public.workspace_acl_{suffix}"
        leaf = f"public.acl_leaf_{suffix}"
        sequence = parent + "_id_seq"
        future = f"aap_workspace_v1.acl_future_{suffix}"
        try:
            async with test_engine.begin() as db:
                await db.exec_driver_sql(
                    f"CREATE TABLE {parent}(id bigserial,secret text) PARTITION BY RANGE(id)"
                )
                await db.exec_driver_sql(
                    f"CREATE TABLE {leaf} PARTITION OF {parent} FOR VALUES FROM (0) TO (100)"
                )
                for target in (quoted_role, "PUBLIC"):
                    for statement in (
                        f"GRANT SELECT(id),UPDATE(name) ON public.projects TO {target}",
                        f"GRANT SELECT ON {leaf} TO {target}",
                        f"GRANT ALL ON SEQUENCE {sequence} TO {target}",
                        f"GRANT CREATE ON SCHEMA aap_workspace_v1 TO {target}",
                        f"GRANT UPDATE(name) ON aap_workspace_v1.project_facts TO {target}",
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA aap_workspace_v1 GRANT ALL ON TABLES TO {target}",
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA aap_workspace_v1 GRANT ALL ON SEQUENCES TO {target}",
                    ):
                        await db.exec_driver_sql(statement)
            async with engine.connect() as db:
                assert await db.scalar(
                    text(
                        "SELECT has_column_privilege(current_user,'public.projects','name','UPDATE')"
                    )
                )
            await reapply()
            async with test_engine.begin() as db:
                await db.exec_driver_sql(
                    f"CREATE TABLE {future}(id bigserial,secret text)"
                )
            await _assert_private_storage_denied(engine, role)
            for relation in (parent, leaf, future):
                async with engine.connect() as db:
                    with pytest.raises(DBAPIError) as denied:
                        await db.exec_driver_sql(f"SELECT id FROM {relation}")
                    assert _sqlstate(denied.value) == "42501"
            for item in (sequence, future + "_id_seq"):
                async with engine.connect() as db:
                    assert not await db.scalar(
                        text(
                            "SELECT has_sequence_privilege(current_user,:sequence,'USAGE,SELECT,UPDATE')"
                        ),
                        {"sequence": item},
                    )
        finally:
            async with test_engine.begin() as db:
                await db.exec_driver_sql(f"DROP TABLE IF EXISTS {future}")
                await db.exec_driver_sql(f"DROP TABLE IF EXISTS {parent} CASCADE")
                for target in (quoted_role, "PUBLIC"):
                    await db.exec_driver_sql(
                        f"REVOKE SELECT(id),UPDATE(name) ON public.projects FROM {target}"
                    )
                    await db.exec_driver_sql(
                        f"REVOKE CREATE ON SCHEMA aap_workspace_v1 FROM {target}"
                    )
                    await db.exec_driver_sql(
                        f"REVOKE UPDATE(name) ON aap_workspace_v1.project_facts FROM {target}"
                    )
                    await db.exec_driver_sql(
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA aap_workspace_v1 REVOKE ALL ON TABLES FROM {target}"
                    )
                    await db.exec_driver_sql(
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA aap_workspace_v1 REVOKE ALL ON SEQUENCES FROM {target}"
                    )
