"""Actual Registry startup synchronization through the retained Inference owner."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

import pytest

from app.config import settings
from app.services import ml_backend_env_sync as env
from yoao_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_STATUS_PROTOCOL,
    RegistryEnvSyncStatusRequest,
    RegistryEnvSyncUnavailable,
)
from yoao_inference_contract.registry_ingress_client import RegistryEnvSyncHttpClient

from tests.inference_owner_container_helpers import (
    inference_owner_container,
    inference_owner_live,
)
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_registry_ingress_bindings import _receipt_ids
from tests.test_inference_registry_ingress_legacy import _server
from tests.test_inference_registry_ingress_native import _cleanup_registries, _connect
from tests.test_inference_routing_owner_integration import _OwnerHttpProxy

pytestmark = [pytest.mark.asyncio, inference_owner_live]


async def test_startup_uses_restricted_registry_image_survives_restart_and_has_no_fallback(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    before_receipts = await _receipt_ids(owner)
    registry_ids = []
    trace = []
    monkeypatch.setattr(settings, "inference_registry_ingress_token_file", "")
    try:
        async with inference_owner_container(test_engine, monkeypatch) as inference:
            token = inference.tokens["registry_ingress"]
            monkeypatch.setattr(settings, "inference_registry_ingress_token", token)
            with _server("new_ok", trace, host=inference.gateway) as backend_url:
                monkeypatch.setattr(env, "_observe_urls", lambda: [backend_url])
                port = urlsplit(inference.origin).port
                assert port is not None
                proxy = _OwnerHttpProxy(port, drop_path="/v1/registry/env-sync")
                proxy.drop_next_command_ack = True
                monkeypatch.setattr(
                    settings, "inference_registry_ingress_url", await proxy.start()
                )
                try:
                    await env.sync_env_backends()
                finally:
                    await proxy.close()
                assert not proxy.drop_next_command_ack and proxy.errors == []
                assert proxy.counts == {
                    "/v1/registry/env-sync": 1,
                    "/v1/registry/env-sync-status": 1,
                }
                first_receipts = await _receipt_ids(owner) - before_receipts
                assert len(first_receipts) == 1 and trace == ["http:/setup"]
                request_id = next(iter(first_receipts))
                command_sha256 = await owner.fetchval(
                    "SELECT command_sha256 FROM yoao_inference_registry_ingress_v1.env_sync_receipts "
                    "WHERE request_id=$1",
                    request_id,
                )
                row = await owner.fetchrow(
                    "SELECT id,state,last_checked_at FROM ml_backend_registry WHERE url=$1",
                    backend_url,
                )
                registry_ids.append(row["id"])
                assert (
                    row["state"] == "connected" and row["last_checked_at"] is not None
                )
                await inference.restart()
                monkeypatch.setattr(
                    settings, "inference_registry_ingress_url", inference.origin
                )
                status_client = RegistryEnvSyncHttpClient(inference.origin, token)
                receipt = await status_client.status(
                    RegistryEnvSyncStatusRequest(
                        schema=REGISTRY_ENV_SYNC_STATUS_PROTOCOL,
                        request_id=request_id,
                        command_sha256=command_sha256,
                    )
                )
                assert receipt.state == "committed" and receipt.observed_urls == 1
                assert await _receipt_ids(owner) - before_receipts == first_receipts
                assert trace == ["http:/setup"]
                inference.evidence["lost_ack_recovered_without_repost"] = True
                inference.evidence["receipt_survived_restart"] = str(request_id)
                await env.sync_env_backends()
                limiter = asyncio.Semaphore(4)

                async def measured():
                    async with limiter:
                        started = time.monotonic()
                        await env.sync_env_backends()
                        return time.monotonic() - started

                samples = sorted(await asyncio.gather(*(measured() for _ in range(16))))
                inference.evidence["local_sample"] = {
                    "requests": 16,
                    "concurrency": 4,
                    "p50_seconds": samples[7],
                    "p95_seconds": samples[15],
                    "api_database_backend_outside_container_budget": True,
                }
                assert len(trace) == 18
                receipts = await _receipt_ids(owner) - before_receipts
                assert len(receipts) == 18
                assert (
                    await owner.fetchval(
                        "SELECT count(*) FROM ml_backend_registry WHERE url=$1",
                        backend_url,
                    )
                    == 1
                )
                assert (
                    await owner.fetchval(
                        "SELECT count(*) FROM ml_backend_pool_members WHERE registry_id=$1",
                        row["id"],
                    )
                    == 1
                )
                inference.stop()
                with pytest.raises(RegistryEnvSyncUnavailable):
                    await env.sync_env_backends()
                assert (
                    len(trace) == 18
                    and await _receipt_ids(owner) - before_receipts == receipts
                )
                inference.evidence["registry_fail_closed_verified"] = True
                inference.evidence["committed_receipts"] = len(receipts)
    finally:
        try:
            await _cleanup_registries(
                owner, registry_ids, await _receipt_ids(owner) - before_receipts
            )
        finally:
            await owner.close()
