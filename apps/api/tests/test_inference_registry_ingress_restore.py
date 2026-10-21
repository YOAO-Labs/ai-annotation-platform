"""Restore nonempty ingress state, owner ACLs and committed receipts locally."""

from __future__ import annotations

import os
import stat
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.engine import make_url

from aap_inference.registry_ingress_manager import RegistryEnvSyncManager
from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.registry_ingress import (
    make_registry_env_sync_request,
    registry_env_sync_command_sha256,
    registry_env_sync_status_request,
)
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_identity_recovery import _postgres_tool
from tests.test_inference_backend_dependency_reads_restore import (
    _assert_clone_identity,
    _create_owned_clone,
    _database_acl,
    _database_head,
    _drop_owned_clone,
)
from tests.test_inference_registry_ingress_legacy import _server
from tests.test_inference_registry_ingress_native import (
    _connect,
    _create_registry,
    _cleanup_registries,
)
from tests.test_inference_registry_ingress_postgres import _client
from tests.test_module_data_roles import _role_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _native_state(engine):
    owner = await _connect(engine)
    try:
        rows = await owner.fetch(
            "SELECT pg_catalog.pg_get_functiondef(p.oid) AS definition, "
            "pg_catalog.pg_get_userbyid(p.proowner) AS owner,p.proacl::text AS acl,p.proconfig AS settings "
            "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='aap_inference_registry_ingress_v1' ORDER BY p.proname"
        )
        assert len(rows) == 11
        assert all(row["owner"] == "inference_code" for row in rows)
        receipts = await owner.fetch(
            "SELECT * FROM aap_inference_registry_ingress_v1.env_sync_receipts ORDER BY request_id"
        )
        assert receipts
        return [dict(row) for row in rows], [dict(row) for row in receipts]
    finally:
        await owner.close()


async def test_nonempty_restore_preserves_ingress_role_receipt_and_actual_owner_execution(
    test_engine, tmp_path
):
    await assert_owned_test_database(test_engine)
    assert await _database_head(test_engine) == "0278"
    clone = f"aap_registry_ingress_restore_{uuid.uuid4().hex[:10]}_test"
    ownership = "registry-ingress-restore:" + str(uuid.uuid4())
    created = set()
    restored = None
    manager = None
    registry_id = uuid.uuid4()
    request_id = uuid.uuid4()
    new_request_id = uuid.uuid4()
    dump = tmp_path / "registry-ingress.dump"
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    owner = await _connect(test_engine)
    client = await _client(test_engine)
    trace = []
    try:
        with _server("new_ok", trace) as origin:
            command = make_registry_env_sync_request(
                [origin],
                timeout_seconds=2,
                capability_limits=CapabilityLimits(),
                request_id=request_id,
            )
            digest = registry_env_sync_command_sha256(command)
            async with client.transaction():
                await _create_registry(
                    client, registry_id, "Before restore", origin, "env"
                )
                await client.fetchrow(
                    "SELECT * FROM aap_inference_registry_ingress_v1.create_singleton_pool($1,$2,$3,'Before restore','null',NULL)",
                    registry_id,
                    uuid.uuid4(),
                    uuid.uuid4(),
                )
                receipt = await client.fetchval(
                    "SELECT aap_inference_registry_ingress_v1.record_sync_result($1,$2,1)",
                    request_id,
                    digest,
                )
                assert "caller_pending" in receipt
            before = await _native_state(test_engine)
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
            assert await _native_state(restored) == before
            role_url = make_url(
                _role_url(
                    "AAP_MODULE_DATA_INFERENCE_REGISTRY_INGRESS_URL",
                    "inference_registry_ingress_client",
                )
            )
            assert role_url.database == test_engine.url.database
            dsn = role_url.set(
                drivername="postgresql", database=clone
            ).render_as_string(hide_password=False)
            manager = RegistryEnvSyncManager(dsn)
            assert await manager.readiness()
            assert (
                await manager.status(registry_env_sync_status_request(command))
            ).state == "committed"
            fresh_command = make_registry_env_sync_request(
                [origin],
                timeout_seconds=2,
                capability_limits=CapabilityLimits(),
                request_id=new_request_id,
            )
            assert (await manager.sync(fresh_command)).state == "committed"
            assert trace == ["http:/setup"]
            restored_owner = await _connect(restored)
            try:
                assert (
                    await restored_owner.fetchval(
                        "SELECT state FROM ml_backend_registry WHERE id=$1", registry_id
                    )
                    == "connected"
                )
                assert (
                    await restored_owner.fetchval(
                        "SELECT count(*) FROM aap_inference_registry_ingress_v1.env_sync_receipts WHERE request_id=$1",
                        new_request_id,
                    )
                    == 1
                )
            finally:
                await restored_owner.close()
            assert (
                await owner.fetchval(
                    "SELECT state FROM ml_backend_registry WHERE id=$1", registry_id
                )
                == "disconnected"
            )
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM aap_inference_registry_ingress_v1.env_sync_receipts WHERE request_id=$1",
                    new_request_id,
                )
                == 0
            )
    finally:
        if manager is not None:
            await manager.close()
        await client.close()
        if restored is not None:
            await restored.dispose()
        try:
            if clone in created:
                await _drop_owned_clone(admin, test_engine, clone, ownership)
        finally:
            await admin.dispose()
            try:
                await _cleanup_registries(
                    owner, [registry_id], [request_id, new_request_id]
                )
            finally:
                await owner.close()
                dump.unlink(missing_ok=True)
