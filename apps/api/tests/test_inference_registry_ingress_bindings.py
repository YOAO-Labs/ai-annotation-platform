"""Use the actual startup entry point and independent authenticated owner process."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.registry_ingress import (
    RegistryEnvSyncUnavailable,
    make_registry_env_sync_request,
    registry_env_sync_status_request,
)
from aap_inference_contract.registry_ingress_client import (
    RegistryEnvSyncHttpClient,
    _AmbiguousRegistryEnvSyncResponse,
)
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services import ml_backend_env_sync as env
from tests.inference_registry_ingress_helpers import ingress_provider, select_binding
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_registry_ingress_legacy import _server
from tests.test_inference_registry_ingress_native import _connect, _cleanup_registries

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def _receipt_ids(owner):
    return set(
        await owner.fetchval(
            "SELECT COALESCE(array_agg(request_id),ARRAY[]::uuid[]) FROM aap_inference_registry_ingress_v1.env_sync_receipts"
        )
    )


@pytest.mark.parametrize("binding", ["borrowed", "dsn", "http"])
async def test_actual_startup_bindings_preserve_manual_priority_failure_fields_and_empty_reconcile(
    test_engine,
    monkeypatch,
    binding,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    ids = [uuid.uuid4() for _ in range(3)]
    before = await _receipt_ids(owner)
    trace = []
    try:
        with _server("new_ok", trace) as origin:
            manual_url = origin + "/manual"
            async with maker.begin() as db:
                db.add_all(
                    [
                        MLBackendRegistry(
                            id=ids[0],
                            name="Before",
                            url=origin,
                            source="env",
                            error_message="keep error",
                        ),
                        MLBackendRegistry(
                            id=ids[1],
                            name="Manual",
                            url=manual_url,
                            source="manual",
                            state="connected",
                        ),
                        MLBackendRegistry(
                            id=ids[2],
                            name="Stale",
                            url=f"http://stale-{ids[2]}.example.test",
                            source="env",
                            state="connected",
                        ),
                    ]
                )

            @asynccontextmanager
            async def session():
                async with maker() as db:
                    yield db

            monkeypatch.setattr(env, "async_session", session)
            monkeypatch.setattr(env, "_observe_urls", lambda: [origin, manual_url])
            async with AsyncExitStack() as stack:
                await select_binding(stack, monkeypatch, test_engine, binding)
                await env.sync_env_backends()
                async with maker() as db:
                    observed, manual, stale = [
                        await db.get(MLBackendRegistry, value) for value in ids
                    ]
                    assert (
                        observed.state == "connected"
                        and observed.error_message == "keep error"
                    )
                    assert (
                        observed.name == "Controlled"
                        and observed.health_meta["capabilities"]
                    )
                    assert manual.name == "Manual" and manual.last_checked_at is None
                    assert (
                        stale.state == "disconnected" and stale.last_checked_at is None
                    )
                monkeypatch.setattr(env, "_observe_urls", lambda: [])
                await env.sync_env_backends()
                async with maker() as db:
                    assert (
                        await db.get(MLBackendRegistry, ids[0])
                    ).state == "disconnected"
                    assert (
                        await db.get(MLBackendRegistry, ids[1])
                    ).state == "connected"
            assert sorted(trace) == ["http:/manual/setup", "http:/setup"]
    finally:
        try:
            await _cleanup_registries(owner, ids, await _receipt_ids(owner) - before)
        finally:
            await owner.close()


async def test_actual_process_lost_reply_recovers_receipt_without_second_command(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    request_id = uuid.uuid4()
    registry_ids = []
    trace = []
    try:
        with _server("new_ok", trace) as origin:
            async with ingress_provider(test_engine.url) as (url, token):
                client = RegistryEnvSyncHttpClient(url, token)
                command = make_registry_env_sync_request(
                    [origin],
                    timeout_seconds=2,
                    capability_limits=CapabilityLimits(),
                    request_id=request_id,
                )
                original_post = client._post
                calls = []

                async def drop_ack(path, body):
                    calls.append(path)
                    result = await original_post(path, body)
                    if path == "/v1/registry/env-sync":
                        raise _AmbiguousRegistryEnvSyncResponse()
                    return result

                monkeypatch.setattr(client, "_post", drop_ack)
                result = await client.sync(command)
                assert result.state == "committed" and result.observed_urls == 1
                assert calls == [
                    "/v1/registry/env-sync",
                    "/v1/registry/env-sync-status",
                ]
                row = await owner.fetchrow(
                    "SELECT id,updated_at FROM ml_backend_registry WHERE url=$1", origin
                )
                registry_ids.append(row["id"])
                monkeypatch.setattr(client, "_post", original_post)
                assert (await client.sync(command)).state == "committed"
                assert (
                    await owner.fetchval(
                        "SELECT updated_at FROM ml_backend_registry WHERE id=$1",
                        row["id"],
                    )
                    == row["updated_at"]
                )
                assert (
                    await owner.fetchval(
                        "SELECT count(*) FROM aap_inference_registry_ingress_v1.env_sync_receipts WHERE request_id=$1",
                        request_id,
                    )
                    == 1
                )
            assert trace == ["http:/setup", "http:/setup"]
    finally:
        try:
            await _cleanup_registries(owner, registry_ids, [request_id])
        finally:
            await owner.close()


async def test_actual_process_failed_batch_has_no_partial_rows_or_success_receipt(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    request_id = uuid.uuid4()
    trace = []
    urls = []
    try:
        with _server("new_ok", trace) as first, _server("bad_name", trace) as second:
            urls = [first, second]
            async with ingress_provider(test_engine.url) as (url, token):
                client = RegistryEnvSyncHttpClient(url, token)
                command = make_registry_env_sync_request(
                    urls,
                    timeout_seconds=2,
                    capability_limits=CapabilityLimits(),
                    request_id=request_id,
                )
                with pytest.raises(RegistryEnvSyncUnavailable):
                    await client.sync(command)
                assert (
                    await client.status(registry_env_sync_status_request(command))
                ).state == "unknown"
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM ml_backend_registry WHERE url=ANY($1::text[])",
                    urls,
                )
                == 0
            )
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM aap_inference_registry_ingress_v1.env_sync_receipts WHERE request_id=$1",
                    request_id,
                )
                == 0
            )
            assert trace == ["http:/setup", "http:/setup"]
    finally:
        try:
            ids = await owner.fetchval(
                "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM ml_backend_registry WHERE url=ANY($1::text[])",
                urls,
            )
            await _cleanup_registries(owner, ids, [request_id])
        finally:
            await owner.close()
