"""Nonempty restore of observation keys, roles and original-transaction execution."""

from __future__ import annotations

from contextlib import AsyncExitStack
import json
import os
import stat
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_inference.health_observation_postgres import (
    BackendHealthObservationPostgresData,
)
from aap_inference_contract.health_observation import (
    BackendHealthObservationUnavailable,
)
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.ml_backend import MLBackendService
from tests.inference_health_observation_helpers import observation_dsn, select_binding
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_identity_recovery import _postgres_tool
from tests.test_inference_backend_dependency_reads_restore import (
    _assert_clone_identity,
    _create_owned_clone,
    _database_acl,
    _database_head,
    _drop_owned_clone,
)
from tests.test_inference_health_observation_bindings import backend_server
from tests.test_inference_health_observation_native import _capture, _connect

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _native_state(engine):
    connection = await _connect(engine)
    try:
        key = await connection.fetchrow(
            "SELECT singleton,pg_catalog.encode(public.digest(secret,'sha256'),'hex') AS fingerprint "
            "FROM aap_inference_health_observation_v1.observation_secret"
        )
        functions = await connection.fetch(
            "SELECT pg_catalog.pg_get_functiondef(p.oid) AS definition, "
            "pg_catalog.pg_get_userbyid(p.proowner) AS owner,p.proacl::text AS acl,p.proconfig AS settings "
            "FROM pg_catalog.pg_proc AS p JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace "
            "WHERE n.nspname IN ('aap_inference_health_observation_v1','aap_inference_health_data_v1') ORDER BY n.nspname,p.proname"
        )
        assert len(functions) == 16
        assert all(row["owner"] == "inference_code" for row in functions)
        return dict(key), [dict(row) for row in functions]
    finally:
        await connection.close()


async def test_restored_key_and_role_contract_keep_database_binding_and_caller_commit(
    test_engine, monkeypatch, tmp_path
):
    await assert_owned_test_database(test_engine)
    assert await _database_head(test_engine) == "0278"
    registry_id, request_id = uuid.uuid4(), uuid.uuid4()
    clone = f"aap_health_observation_restore_{uuid.uuid4().hex[:10]}_test"
    ownership = "health-observation-restore:" + str(uuid.uuid4())
    created = set()
    restored = None
    origin = None
    transaction = None
    data = None
    dump = tmp_path / "health-observation.dump"
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        with backend_server() as (url, calls):
            async with maker.begin() as seed:
                seed.add(
                    MLBackendRegistry(
                        id=registry_id,
                        name="Restored health",
                        url=url,
                        auth_method="token",
                        auth_token="restored-health-token",
                        extra_params={},
                    )
                )
            native_before = await _native_state(test_engine)
            origin = await _connect(test_engine, role="inference_health_origin_client")
            transaction = origin.transaction()
            await transaction.start()
            subject = await _capture(
                origin,
                registry_id,
                request_id,
                json.dumps(
                    {
                        "id": str(registry_id),
                        "url": url,
                        "auth_method": "token",
                        "auth_token": "restored-health-token",
                        "gpu_resource_id": None,
                        "vram_budget_mb": None,
                        "extra_params": {},
                    }
                ),
            )
            source_xid = await origin.fetchval(
                "SELECT pg_current_xact_id_if_assigned()::text"
            )
            dump.touch(mode=0o600)
            dump.write_bytes(
                _postgres_tool(
                    test_engine,
                    ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
                )
            )
            assert dump.read_bytes().startswith(b"PGDMP")
            assert stat.S_IMODE(dump.stat().st_mode) == 0o600
            await _create_owned_clone(
                admin,
                test_engine,
                clone,
                ownership,
                await _database_acl(test_engine),
                created,
            )
            _postgres_tool(
                test_engine,
                ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
                body=dump.read_bytes(),
            )
            restored = create_async_engine(
                test_engine.url.set(database=clone), hide_parameters=True
            )
            await _assert_clone_identity(restored, clone, ownership)
            assert await _database_head(restored) == "0278"
            assert await _native_state(restored) == native_before
            async with restored.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT pg_xact_status(CAST(:xid AS xid8))"),
                        {"xid": int(source_xid)},
                    )
                    == "in progress"
                )
            data = BackendHealthObservationPostgresData(observation_dsn(restored.url))
            assert await data.readiness()
            # The key is the same and the source transaction is still live; the
            # database binding must nevertheless reject its source-only subject.
            with pytest.raises(BackendHealthObservationUnavailable):
                await data.open_subject(subject, registry_id, request_id)
            async with AsyncExitStack() as stack:
                await select_binding(stack, monkeypatch, restored, "dsn")
                restored_maker = async_sessionmaker(restored, expire_on_commit=False)
                async with restored_maker() as caller:
                    assert await MLBackendService(caller).check_health(registry_id)
                    async with restored_maker() as outside:
                        assert (
                            await outside.get(MLBackendRegistry, registry_id)
                        ).last_checked_at is None
                    await caller.commit()
                async with restored_maker() as outside:
                    assert (
                        await outside.get(MLBackendRegistry, registry_id)
                    ).last_checked_at is not None
            assert calls == [
                ("/health", "Bearer restored-health-token"),
                ("/setup", "Bearer restored-health-token"),
            ]
            async with maker() as source:
                assert (
                    await source.get(MLBackendRegistry, registry_id)
                ).last_checked_at is None
    finally:
        if transaction is not None:
            await transaction.rollback()
        if origin is not None:
            await origin.close()
        if data is not None:
            await data.close()
        if restored is not None:
            await restored.dispose()
        try:
            if clone in created:
                await _drop_owned_clone(admin, test_engine, clone, ownership)
        finally:
            await admin.dispose()
            async with maker.begin() as cleanup:
                await cleanup.execute(
                    text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                    {"id": registry_id},
                )
            dump.unlink(missing_ok=True)
