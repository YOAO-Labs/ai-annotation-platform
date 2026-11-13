"""Qualify image cutover from its real historical schema, without downgrades."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_asset_storage_cleanup_runtime import _verify_postgres

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable module-data verifier",
    ),
]
_API = Path(__file__).resolve().parents[1]
_TASK_ADDITIONS = [
    "media_source_generation", "media_object_identity", "media_source_drifted"
]
_GENERATION_ADDITIONS = [
    "job_id", "job_generation", "source_generation", "source_snapshot",
    "output_prefix", "manifest", "policy",
]


def _upgrade(url, revision):
    config = Config(str(_API / "alembic.ini"))
    config.set_main_option("script_location", str(_API / "alembic"))
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    command.upgrade(config, revision)


@pytest.fixture
async def image_cutover_database(test_engine):
    _verify_postgres(test_engine.url)
    assert test_engine.url.host == "127.0.0.1"
    assert test_engine.url.username == "owner"
    assert test_engine.url.database.endswith("_test")
    async with test_engine.connect() as source:
        acl = (
            await source.execute(text(
                "SELECT COALESCE(role.rolname,'PUBLIC'), entry.privilege_type, "
                "entry.is_grantable FROM pg_database AS database "
                "CROSS JOIN LATERAL aclexplode(COALESCE(database.datacl, "
                "acldefault('d',database.datdba))) AS entry "
                "LEFT JOIN pg_roles AS role ON role.oid=entry.grantee "
                "WHERE database.datname=current_database()"
            ))
        ).all()
    name = f"aap_image_cutover_{uuid4().hex[:12]}_test"
    ownership = f"image-cutover:{uuid4()}"
    url = test_engine.url.set(database=name)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    historical = create_async_engine(url)
    created = False
    tagged = False
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
            created = True
            await connection.execute(
                text(f"COMMENT ON DATABASE \"{name}\" IS '{ownership}'")
            )
            tagged = True
            quote = connection.dialect.identifier_preparer.quote_identifier
            await connection.execute(text(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC'))
            for grantee, privilege, grantable in acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                suffix = " WITH GRANT OPTION" if grantable else ""
                await connection.execute(
                    text(f'GRANT {privilege} ON DATABASE "{name}" TO {role}{suffix}')
                )
        await asyncio.to_thread(_upgrade, url, "bo0219")
        yield url, historical
    finally:
        await historical.dispose()
        try:
            if created:
                _verify_postgres(test_engine.url)
                async with admin.connect() as connection:
                    marker = await connection.scalar(text(
                        "SELECT shobj_description(oid,'pg_database') "
                        "FROM pg_database WHERE datname=:name"
                    ), {"name": name})
                    assert marker == ownership or (not tagged and marker is None)
                    # Only an untagged partial CREATE may lack our comment.
                    await connection.execute(text(f'DROP DATABASE "{name}"'))
        finally:
            await admin.dispose()


async def _seed_legacy_source(connection, status="ready"):
    actor_id, project_id, task_id, asset_id = (uuid4() for _ in range(4))
    await connection.execute(text(
        "INSERT INTO public.users(id,email,name,password_hash,role) "
        "VALUES (:id,:email,'image cutover','x','super_admin')"
    ), {"id": actor_id, "email": f"image-cutover-{actor_id}@test.local"})
    await connection.execute(text(
        "INSERT INTO public.projects(id,display_id,name,type_label,type_key,owner_id) "
        "VALUES (:id,:display,'image cutover','detection','image-det',:owner)"
    ), {"id": project_id, "display": f"P-{project_id.hex[:12]}", "owner": actor_id})
    await connection.execute(text(
        "INSERT INTO public.tasks(id,project_id,display_id,file_name,file_path,file_type) "
        "VALUES (:id,:project,:display,'guard.png',:path,'image')"
    ), {
        "id": task_id, "project": project_id, "display": f"T-{task_id.hex[:12]}",
        "path": f"images/{task_id}.png",
    })
    await connection.execute(text(
        "INSERT INTO public.image_pyramid_assets(id,task_id,profile_version) "
        "VALUES (:id,:task,'pyramid-v1')"
    ), {"id": asset_id, "task": task_id})
    await connection.execute(text(
        "INSERT INTO public.image_pyramid_generations("
        "asset_id,generation,source_identity,status,normalization_version) "
        "VALUES (:id,1,'registered',:status,'exif-autorotate-srgb-v1')"
    ), {"id": asset_id, "status": status})
    return task_id


async def _source_rows(connection):
    result = {}
    for table, additions in (
        ("tasks", _TASK_ADDITIONS),
        ("image_pyramid_assets", []),
        ("image_pyramid_generations", _GENERATION_ADDITIONS),
        ("dataset_import_deliveries", []),
        ("dataset_source_ingest_deliveries", []),
    ):
        result[table] = (
            await connection.scalars(text(
                f"SELECT to_jsonb(source) - CAST(:additions AS text[]) "
                f"FROM public.{table} AS source ORDER BY to_jsonb(source)::text"
            ), {"additions": additions})
        ).all()
    return result


async def _schema_state(connection):
    columns = (
        await connection.execute(text(
            "SELECT table_name,column_name,data_type,column_default,is_nullable "
            "FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name IN ('tasks','image_pyramid_assets','image_pyramid_generations') "
            "ORDER BY table_name,ordinal_position"
        ))
    ).all()
    return (
        await connection.scalar(text("SELECT version_num FROM public.alembic_version")),
        columns,
        await connection.scalar(text("SELECT to_regclass('public.asset_image_jobs')")),
        await connection.scalar(text(
            "SELECT to_regprocedure('aap_labeling_v1._image_source_snapshot(public.tasks)')"
        )),
    )


@pytest.mark.parametrize("legacy", ["pending", "building", "import", "ingest"])
async def test_upgrade_refuses_legacy_writers_atomically(image_cutover_database, legacy):
    url, historical = image_cutover_database
    async with historical.begin() as connection:
        task_id = await _seed_legacy_source(
            connection, legacy if legacy in {"pending", "building"} else "ready"
        )
        if legacy in {"import", "ingest"}:
            table, parent = (
                ("dataset_import_deliveries", "job_id")
                if legacy == "import"
                else ("dataset_source_ingest_deliveries", "operation_id")
            )
            await connection.execute(text(
                f"INSERT INTO public.{table}(id,{parent},kind,target_id) "
                "VALUES (:id,:parent,'thumbnail',:target)"
            ), {"id": uuid4(), "parent": uuid4(), "target": task_id})
        before_rows = await _source_rows(connection)
        before_schema = await _schema_state(connection)
        assert before_schema[0] == "bo0219"
        assert before_schema[2:] == (None, None)

    with pytest.raises(DBAPIError) as refused:
        await asyncio.to_thread(_upgrade, url, "bo0220")
    assert refused.value.orig.sqlstate == "55000"
    async with historical.begin() as connection:
        assert await _schema_state(connection) == before_schema
        assert await _source_rows(connection) == before_rows
        # Resolve exactly the legacy work covered by the cutover's drain
        # contract; no forward guard or dependent object is removed.
        if legacy in {"pending", "building"}:
            await connection.execute(text(
                "UPDATE public.image_pyramid_generations SET status='failed'"
            ))
        else:
            await connection.execute(text(
                f"UPDATE public.{table} SET delivered_at=now()"
            ))
        drained = await _source_rows(connection)
    await asyncio.to_thread(_upgrade, url, "bo0220")
    async with historical.connect() as connection:
        assert await connection.scalar(
            text("SELECT version_num FROM public.alembic_version")
        ) == "bo0220"
        assert await _source_rows(connection) == drained
        assert await connection.scalar(text(
            "SELECT to_regclass('public.asset_image_jobs') IS NOT NULL"
        ))


async def test_forward_cutover_preserves_retained_source_evidence(image_cutover_database):
    url, historical = image_cutover_database
    async with historical.begin() as connection:
        task_id = await _seed_legacy_source(connection)
        before = await _source_rows(connection)
    await asyncio.to_thread(_upgrade, url, "bo0220")
    async with historical.begin() as connection:
        assert await _source_rows(connection) == before
        facts = (await connection.execute(text(
            "SELECT media_source_generation,media_object_identity,media_source_drifted "
            "FROM public.tasks WHERE id=:id"
        ), {"id": task_id})).one()
        assert facts == (1, None, False)
        await connection.execute(text(
            "UPDATE public.tasks SET media_object_identity="
            "'{\"etag\":\"registered\",\"byte_size\":128}'::jsonb WHERE id=:id"
        ), {"id": task_id})
    async with historical.begin() as connection:
        with pytest.raises(DBAPIError) as refused:
            async with connection.begin_nested():
                await connection.execute(text(
                    "UPDATE public.tasks SET media_source_generation=0 WHERE id=:id"
                ), {"id": task_id})
        assert refused.value.orig.sqlstate == "23514"
        assert await connection.scalar(text(
            "SELECT media_object_identity FROM public.tasks WHERE id=:id"
        ), {"id": task_id}) == {"etag": "registered", "byte_size": 128}
