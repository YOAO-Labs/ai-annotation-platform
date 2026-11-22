"""Public Registry deletion keeps orphan and managed GPU fence guards intact."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.gpu_fence_helpers import cleanup_committed_backend
from tests.inference_registry_ingress_helpers import ingress_dsn
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _assert_pg_error(connection, statement, *args, sqlstate: str) -> None:
    with pytest.raises(asyncpg.PostgresError) as caught:
        async with connection.transaction():
            await connection.execute(statement, *args)
    assert caught.value.sqlstate == sqlstate


async def _insert_registry(connection, backend_id, *, gpu_resource_id=None) -> None:
    await connection.execute(
        """
        INSERT INTO public.ml_backend_registry (
            id, name, url, state, is_interactive, auth_method, extra_params,
            gpu_resource_id, vram_budget_mb, eviction_priority, source
        ) VALUES (
            $1, $2, $3, 'disconnected', false, 'none', '{}'::jsonb,
            $4, $5, 0, 'manual'
        )
        """,
        backend_id,
        f"gpu-delete-noop-{backend_id}",
        f"http://gpu-delete-noop-{backend_id}.example.test",
        gpu_resource_id,
        2048 if gpu_resource_id is not None else None,
    )


async def test_registry_delete_skips_only_missing_fence_rows(test_engine) -> None:
    await assert_owned_test_database(test_engine)
    owner_url = make_url(test_engine.url).set(drivername="postgresql")
    assert owner_url.database and owner_url.database.endswith(("_test", "_e2e"))
    connection = await asyncpg.connect(
        owner_url.render_as_string(hide_password=False),
        timeout=5,
        command_timeout=10,
    )
    no_fence_id, historical_fence_id, managed_id = (uuid.uuid4() for _ in range(3))
    transaction = connection.transaction()
    await transaction.start()
    try:
        function_config = await connection.fetchrow(
            """
            SELECT prosecdef, proconfig
            FROM pg_catalog.pg_proc
            WHERE oid = 'public.sync_gpu_backend_membership()'::regprocedure
            """
        )
        assert function_config["prosecdef"] is False
        search_path = next(
            setting.partition("=")[2]
            for setting in function_config["proconfig"]
            if setting.startswith("search_path=")
        )
        assert tuple(part.strip() for part in search_path.split(",")) == (
            "pg_catalog",
            "public",
            "pg_temp",
        )

        await _insert_registry(connection, no_fence_id)
        await _insert_registry(connection, historical_fence_id)
        await connection.execute(
            "INSERT INTO public.gpu_backend_fences (backend_registry_id) VALUES ($1)",
            historical_fence_id,
        )
        await _insert_registry(
            connection,
            managed_id,
            gpu_resource_id=f"node-delete-noop/GPU-{managed_id.hex[:8]}",
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.gpu_backend_memberships "
                "WHERE backend_registry_id = $1",
                managed_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.gpu_backend_fences "
                "WHERE backend_registry_id = $1",
                managed_id,
            )
            == 1
        )
        await connection.execute(
            "UPDATE public.gpu_backend_fences SET runtime_epoch_high_water = 1 "
            "WHERE backend_registry_id = $1",
            managed_id,
        )

        await connection.execute('SET LOCAL ROLE "identity_api_consumer"')
        privileges = await connection.fetchrow(
            """
            SELECT has_table_privilege(current_user,
                       'public.ml_backend_registry', 'DELETE') AS registry_delete,
                   has_table_privilege(current_user,
                       'public.gpu_backend_fences', 'DELETE') AS fence_delete
            """
        )
        assert privileges["registry_delete"] is False
        assert privileges["fence_delete"] is False
        for statement in (
            "DELETE FROM public.gpu_backend_fences WHERE false",
            "DELETE FROM public.ml_backend_registry WHERE false",
            "SELECT * FROM public.gpu_backend_fences",
        ):
            await _assert_pg_error(connection, statement, sqlstate="42501")

        delete = "SELECT yoao_inference_topology_v1.delete_registry($1::uuid,'null')"
        result = json.loads(await connection.fetchval(delete, no_fence_id))
        assert result == {"deleted": True, "pool_id": None}
        await _assert_pg_error(
            connection, delete, historical_fence_id, sqlstate="23514"
        )
        await _assert_pg_error(connection, delete, managed_id, sqlstate="P0001")

        await connection.execute("RESET ROLE")
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.ml_backend_registry WHERE id=$1",
                no_fence_id,
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.ml_backend_registry WHERE id=ANY($1::uuid[])",
                [historical_fence_id, managed_id],
            )
            == 2
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.gpu_backend_fences "
                "WHERE backend_registry_id=$1",
                historical_fence_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT state FROM public.gpu_backend_memberships "
                "WHERE backend_registry_id=$1",
                managed_id,
            )
            == "pending"
        )
        assert (
            await connection.fetchval(
                "SELECT runtime_epoch_high_water FROM public.gpu_backend_fences "
                "WHERE backend_registry_id=$1",
                managed_id,
            )
            == 1
        )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize(
    ("value", "json_type", "seed_tag_reaches_fence"),
    [
        pytest.param(None, "null", True, id="json-null"),
        pytest.param(False, "boolean", True, id="false"),
        pytest.param(0, "number", True, id="zero"),
        pytest.param("", "string", True, id="empty-string"),
        pytest.param("sensor", "string", False, id="string"),
        pytest.param([], "array", True, id="empty-array"),
        pytest.param(["sensor", 7], "array", False, id="array"),
    ],
)
async def test_scalar_registry_parameters_preserve_gpu_membership_fences(
    test_engine, value, json_type, seed_tag_reaches_fence
):
    await assert_owned_test_database(test_engine)
    owner_url = make_url(test_engine.url).set(drivername="postgresql")
    runtime_url = make_url(os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"])
    assert runtime_url.drivername == "postgresql+asyncpg"
    assert runtime_url.username == "legacy_runtime"
    assert (runtime_url.host, runtime_url.port, runtime_url.database) == (
        owner_url.host,
        owner_url.port,
        owner_url.database,
    )
    runtime_url = runtime_url.set(drivername="postgresql")
    ingress_url = make_url(ingress_dsn(test_engine.url))
    owner = await asyncpg.connect(
        owner_url.render_as_string(hide_password=False), timeout=5, command_timeout=10
    )
    runtime = None
    ingress = None
    backend_id = uuid.uuid4()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    created = False
    try:
        await _insert_registry(
            owner,
            backend_id,
            gpu_resource_id=f"node-scalar/GPU-{backend_id.hex[:8]}",
        )
        created = True
        await owner.execute(
            "UPDATE public.ml_backend_registry SET extra_params=NULL WHERE id=$1",
            backend_id,
        )
        assert await owner.fetchval(
            "SELECT extra_params IS NULL FROM public.ml_backend_registry WHERE id=$1",
            backend_id,
        )
        assert (
            await owner.fetchval(
                "SELECT membership_epoch FROM public.gpu_backend_memberships "
                "WHERE backend_registry_id=$1",
                backend_id,
            )
            == 1
        )
        runtime = await asyncpg.connect(
            runtime_url.render_as_string(hide_password=False),
            timeout=5,
            command_timeout=10,
        )
        ingress = await asyncpg.connect(
            ingress_url.render_as_string(hide_password=False),
            timeout=5,
            command_timeout=10,
        )
        for client, role in (
            (runtime, "legacy_runtime"),
            (ingress, "yoao_inference_registry_ingress_client"),
        ):
            assert await client.fetchval("SELECT current_user = session_user")
            assert await client.fetchval("SELECT current_user") == role

        snapshot = json.loads(
            await runtime.fetchval(
                "SELECT yoao_inference_topology_v1.registry_auth_snapshot($1::uuid)",
                backend_id,
            )
        )
        patch_sql = (
            "SELECT yoao_inference_topology_v1.patch_registry_auth_cas("
            "$1::uuid,$2::text,$3::text,$4::bigint)"
        )
        changed = json.loads(
            await runtime.fetchval(
                patch_sql,
                backend_id,
                json.dumps({"extra_params": value}),
                snapshot["url"],
                snapshot["auth_revision"],
            )
        )
        assert changed["extra_params"] == value
        observed = await owner.fetchrow(
            "SELECT extra_params IS NOT DISTINCT FROM $2::jsonb AS equal, "
            "jsonb_typeof(extra_params) AS kind FROM public.ml_backend_registry WHERE id=$1",
            backend_id,
            json.dumps(value),
        )
        assert observed["equal"] is True and observed["kind"] == json_type
        membership = await owner.fetchrow(
            "SELECT membership_epoch,state,max_concurrency FROM public.gpu_backend_memberships "
            "WHERE backend_registry_id=$1",
            backend_id,
        )
        assert dict(membership) == {
            "membership_epoch": 2,
            "state": "pending",
            "max_concurrency": 4,
        }
        await owner.execute(
            "UPDATE public.gpu_backend_fences SET runtime_epoch_high_water=1 "
            "WHERE backend_registry_id=$1",
            backend_id,
        )

        async def state():
            return tuple(
                await owner.fetchrow(
                    "SELECT to_jsonb(r)::text AS registry, to_jsonb(m)::text AS membership, "
                    "to_jsonb(f)::text AS fence FROM public.ml_backend_registry r "
                    "JOIN public.gpu_backend_memberships m ON m.backend_registry_id=r.id "
                    "JOIN public.gpu_backend_fences f ON f.backend_registry_id=r.id "
                    "WHERE r.id=$1",
                    backend_id,
                )
            )

        before_metadata = await state()
        updated = json.loads(
            await runtime.fetchval(
                patch_sql,
                backend_id,
                json.dumps({"name": f"renamed-{backend_id}", "extra_params": value}),
                snapshot["url"],
                snapshot["auth_revision"],
            )
        )
        assert updated["name"] == f"renamed-{backend_id}"
        assert updated["extra_params"] == value
        after_metadata = await state()
        assert after_metadata[1:] == before_metadata[1:]
        old_registry = json.loads(before_metadata[0])
        new_registry = json.loads(after_metadata[0])
        old_registry["name"] = new_registry["name"]
        old_registry["updated_at"] = new_registry["updated_at"]
        assert new_registry == old_registry
        before_denial = after_metadata

        with pytest.raises(asyncpg.PostgresError) as fenced_patch:
            await runtime.fetchval(
                patch_sql,
                backend_id,
                json.dumps({"url": f"http://changed-{backend_id}.example.test"}),
                snapshot["url"],
                snapshot["auth_revision"],
            )
        assert fenced_patch.value.sqlstate == "P0001"
        assert await state() == before_denial

        with pytest.raises(asyncpg.PostgresError) as fenced_tag:
            await ingress.fetchrow(
                "SELECT * FROM yoao_inference_registry_ingress_v1.tag_seed_registry("
                "$1::uuid,$2::text)",
                backend_id,
                json.dumps(
                    {
                        "managed_by": "screenshot-seed",
                        "profile": "screenshots",
                        "revision": "screenshots-2026-08-g",
                    }
                ),
            )
        if seed_tag_reaches_fence:
            assert fenced_tag.value.sqlstate == "23514"
            assert (
                fenced_tag.value.constraint_name
                == "ck_gpu_backend_membership_managed_mutation"
            )
        else:
            assert fenced_tag.value.sqlstate == "22023"
        assert await state() == before_denial
    finally:
        if ingress is not None:
            await ingress.close()
        if runtime is not None:
            await runtime.close()
        await owner.close()
        if created:
            await cleanup_committed_backend(factory, backend_id)
