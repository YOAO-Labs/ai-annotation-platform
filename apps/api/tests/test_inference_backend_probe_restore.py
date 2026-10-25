"""Nonempty two-clone restore of the probe key, grants and actual setup execution."""

from __future__ import annotations

import os
import stat
import sys
import uuid
import warnings

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.client import BackendProbeHttpClient
from aap_inference_contract.probe import BackendProbeUnavailable
from tests.inference_backend_probe_helpers import probe_provider
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_identity_recovery import _postgres_tool
from tests.test_inference_backend_dependency_reads_restore import (
    _assert_clone_identity,
    _create_owned_clone,
    _database_acl,
    _database_head,
    _drop_owned_clone,
)
from tests.test_inference_backend_probe_bindings import setup_backend
from tests.test_inference_backend_probe_native import (
    _assert_invalid_subject,
    _connect,
    _insert_backend,
    _workspace_snapshot,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _state(engine):
    connection = await _connect(engine)
    try:
        key = await connection.fetchrow(
            "SELECT singleton, pg_catalog.encode(public.digest(secret,'sha256'),'hex') AS fingerprint "
            "FROM aap_inference_backend_probe_v1.probe_secret"
        )
        functions = await connection.fetch(
            "SELECT pg_catalog.pg_get_functiondef(p.oid) AS definition, "
            "pg_catalog.pg_get_userbyid(p.proowner) AS owner, p.proacl::text AS acl, p.proconfig AS settings "
            "FROM pg_catalog.pg_proc AS p JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace "
            "WHERE n.nspname='aap_inference_backend_probe_v1' "
            "OR (n.nspname='aap_inference_v1' AND p.proname='workspace_configuration_backend') "
            "ORDER BY n.nspname,p.proname"
        )
        assert len(functions) == 3
        assert all(row["owner"] == "inference_code" for row in functions)
        acl = await connection.fetchrow(
            "SELECT pg_catalog.pg_get_userbyid(c.relowner) AS owner,c.relacl::text AS acl "
            "FROM pg_catalog.pg_class AS c WHERE c.oid='aap_inference_backend_probe_v1.probe_secret'::regclass"
        )
        rows = await connection.fetch(
            "SELECT id,name,url,auth_method,auth_token,extra_params::text AS extra_params "
            "FROM public.ml_backend_registry ORDER BY id"
        )
        return (
            dict(key),
            [dict(row) for row in functions],
            dict(acl),
            [dict(row) for row in rows],
        )
    finally:
        await connection.close()


async def test_probe_key_and_snapshot_execution_survive_nonempty_owned_restore(
    test_engine, tmp_path
):
    await assert_owned_test_database(test_engine)
    assert await _database_head(test_engine) == "0276"
    identity = uuid.uuid4().hex[:10]
    names = [
        f"aap_workspace_configuration_restore_{identity}_{part}_test"
        for part in ("s", "r")
    ]
    owners = {name: "inference-probe-restore:" + str(uuid.uuid4()) for name in names}
    created = set()
    engines = []
    base_dump, source_dump = tmp_path / "base.dump", tmp_path / "source.dump"
    acl = await _database_acl(test_engine)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    source_origin = None
    source_transaction = None
    try:
        for dump in (base_dump, source_dump):
            dump.touch(mode=0o600)
        base_dump.write_bytes(
            _postgres_tool(
                test_engine, ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database]
            )
        )
        await _create_owned_clone(
            admin, test_engine, names[0], owners[names[0]], acl, created
        )
        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", names[0]],
            body=base_dump.read_bytes(),
        )
        source = create_async_engine(
            test_engine.url.set(database=names[0]), hide_parameters=True
        )
        engines.append(source)
        await _assert_clone_identity(source, names[0], owners[names[0]])
        with setup_backend() as (url, calls):
            registry_id = uuid.uuid4()
            seed = await _connect(source)
            try:
                await _insert_backend(
                    seed,
                    registry_id,
                    url=url + "/retained",
                    token="restored-snapshot-token",
                    extra_params={"max_concurrency": 4, "unicode": "图像"},
                )
            finally:
                await seed.close()
            before = await _state(source)
            assert before[3]
            source_origin = await _connect(source)
            source_transaction = source_origin.transaction()
            await source_transaction.start()
            await source_origin.execute("SET LOCAL ROLE workspace_code")
            old_subject = (await _workspace_snapshot(source_origin, registry_id))[
                "backend"
            ]["probe_subject"]
            source_dump.write_bytes(
                _postgres_tool(test_engine, ["pg_dump", "-U", "owner", "-Fc", names[0]])
            )
            for dump in (base_dump, source_dump):
                assert dump.read_bytes().startswith(b"PGDMP")
                assert stat.S_IMODE(dump.stat().st_mode) == 0o600
            await _create_owned_clone(
                admin, test_engine, names[1], owners[names[1]], acl, created
            )
            _postgres_tool(
                test_engine,
                ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", names[1]],
                body=source_dump.read_bytes(),
            )
            restored = create_async_engine(
                test_engine.url.set(database=names[1]), hide_parameters=True
            )
            engines.append(restored)
            await _assert_clone_identity(restored, names[1], owners[names[1]])
            assert await _database_head(restored) == "0276"
            assert await _state(restored) == before
            probe = await _connect(restored, role="inference_backend_probe_client")
            try:
                # The key is preserved, but an active subject from the source database is rejected.
                await _assert_invalid_subject(probe, old_subject, registry_id)
                assert (
                    await probe.fetchval(
                        "SELECT has_table_privilege(current_user,'aap_inference_backend_probe_v1.probe_secret','SELECT')"
                    )
                    is False
                )
                assert (
                    await probe.fetchval(
                        "SELECT has_table_privilege(current_user,'public.ml_backend_registry','SELECT')"
                    )
                    is False
                )
            finally:
                await probe.close()
            origin = await _connect(restored)
            try:
                transaction = origin.transaction()
                await transaction.start()
                await origin.execute("SET LOCAL ROLE workspace_code")
                subject = (await _workspace_snapshot(origin, registry_id))["backend"][
                    "probe_subject"
                ]
                async with probe_provider(restored.url) as (provider, token):
                    client = BackendProbeHttpClient(provider, token)
                    assert await client.probe_modalities(
                        registry_id, subject, timeout=2, limits=CapabilityLimits()
                    ) == ["image", "video"]
                    assert calls == [
                        (
                            "/retained/setup",
                            "Bearer restored-snapshot-token",
                            "no-cache",
                        )
                    ]
                    await transaction.rollback()
                    with pytest.raises(BackendProbeUnavailable):
                        await client.probe_modalities(
                            registry_id, subject, timeout=2, limits=CapabilityLimits()
                        )
                    assert len(calls) == 1
            finally:
                await origin.close()
            assert await _state(source) == before
            assert await _state(restored) == before
    finally:
        original_error = sys.exc_info()[0] is not None
        errors = []
        if source_transaction is not None:
            try:
                await source_transaction.rollback()
            except Exception as error:
                errors.append(type(error).__name__)
        if source_origin is not None:
            await source_origin.close()
        for engine in reversed(engines):
            await engine.dispose()
        for name in reversed(names):
            if name in created:
                try:
                    await _drop_owned_clone(admin, test_engine, name, owners[name])
                except Exception as error:
                    errors.append(type(error).__name__)
        await admin.dispose()
        for dump in (base_dump, source_dump):
            dump.unlink(missing_ok=True)
        if errors:
            if original_error:
                warnings.warn("Owned probe restore cleanup failed: " + ",".join(errors))
            else:
                raise AssertionError(
                    "Owned probe restore cleanup failed: " + ",".join(errors)
                )
