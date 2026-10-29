"""PostgreSQL acceptance for the Native plugin installation owner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference_contract.plugin import PreparePluginInstallation
from app.config import settings
from app.services.inference_backend_control import capture_backend_endpoint_subject
from app.services.inference_topology_data import InferenceTopologyData
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import (
    _cleanup,
    _connect,
    _identity_hash,
    _step,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _payload():
    installation_id = uuid4()
    return PreparePluginInstallation.model_validate(
        {
            "installation_id": installation_id,
            "name": f"Native plugin {installation_id}",
            "url": f"http://plugin-{installation_id}.invalid",
            "auth_token": "secret-" + uuid4().hex + uuid4().hex,
            "manifest": {
                "plugin_id": "libreyolo",
                "version": "1.0",
                "publisher": "example",
                "license": "MIT",
                "artifact": "registry.example/plugin@sha256:" + "a" * 64,
                "sdk_compatibility": ">=1",
                "entrypoint": ["python", "-m", "plugin"],
                "capabilities": ["inference", "detection"],
                "resource": {"device": "cpu", "memory_mb": 1024, "gpu_memory_mb": 0},
                "asset_manifest_sha256": "b" * 64,
                "allowed_asset_sources": [],
            },
            "resource": {"logical_device": "cpu"},
        }
    )


def _receipt(payload):
    return {
        "installation_id": str(payload.installation_id),
        "registry_id": str(payload.installation_id),
        "artifact": payload.manifest.artifact,
        "image_id": "sha256:" + "c" * 64,
        "container_id": "d" * 64,
        "resource": payload.resource.model_dump(mode="json"),
    }


def _evidence(payload, *, boot_id=None, control_epoch=None):
    return {
        **_receipt(payload),
        "endpoint": payload.url,
        "boot_id": boot_id or str(uuid4()),
        "control_epoch": control_epoch,
    }


async def test_cancel_prepared_is_revision_fenced_and_retains_history(
    db_session, test_engine
):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    await data.prepare_plugin_installation(payload)
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(
                payload.installation_id, 2, "cancel_prepared"
            )
    retired = await data.command_plugin_installation(
        payload.installation_id, 1, "cancel_prepared"
    )
    assert retired.installation.status == "retired"
    assert retired.installation.revision == 2
    row = (
        await db_session.execute(
            text(
                "SELECT auth_token, extra_params FROM public.ml_backend_registry WHERE id=:id"
            ),
            {"id": payload.installation_id},
        )
    ).one()
    assert (
        row.auth_token
        == "retired-sha256:" + hashlib.sha256(payload.auth_token.encode()).hexdigest()
    )
    assert row.extra_params["plugin_lifecycle"]["cancelled_prepared"] is True
    assert await data.get_plugin_installation(payload.installation_id) == retired


async def test_cancel_prepared_rejects_recorded_launch(db_session, test_engine):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    await data.prepare_plugin_installation(payload)
    recorded = await data.command_plugin_installation(
        payload.installation_id, 1, "record_launch", _receipt(payload)
    )
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(
                payload.installation_id, 2, "cancel_prepared"
            )
    assert await data.get_plugin_installation(payload.installation_id) == recorded


async def test_gpu_prepared_cancellation_requires_untouched_durable_fence(
    db_session, test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    monkeypatch.setattr(
        settings,
        "gpu_arbiter_resources_json",
        json.dumps(
            {
                "node/GPU-123": {
                    "node_id": "node",
                    "physical_device_token": "GPU-123",
                    "allocatable_mb": 8192,
                    "mode": "observe",
                },
            }
        ),
    )
    data = InferenceTopologyData(db_session)

    def gpu_payload():
        value = _payload().model_dump(mode="json")
        value["manifest"]["resource"] = {
            "device": "cuda",
            "memory_mb": 1024,
            "gpu_memory_mb": 512,
        }
        value["resource"] = {
            "gpu_resource_id": "node/GPU-123",
            "physical_gpu_uuid": "GPU-123",
            "logical_device": "cuda:0",
        }
        return PreparePluginInstallation.model_validate(value)

    untouched = gpu_payload()
    await data.prepare_plugin_installation(untouched)
    retired = await data.command_plugin_installation(
        untouched.installation_id, 1, "cancel_prepared"
    )
    assert retired.installation.status == "retired"
    claim = (
        await db_session.execute(
            text(
                "SELECT r.gpu_resource_id, r.vram_budget_mb, m.state, m.membership_epoch "
                "FROM public.ml_backend_registry r JOIN public.gpu_backend_memberships m "
                "ON m.backend_registry_id=r.id WHERE r.id=:id"
            ),
            {"id": untouched.installation_id},
        )
    ).one()
    assert (claim.gpu_resource_id, claim.vram_budget_mb, claim.state) == (
        None,
        None,
        "retiring",
    )
    collection = await db_session.scalar(
        text(
            "SELECT aap_inference_gpu_collector_v1.gpu_collector_step('collect_tombstone',:arguments)"
        ),
        {
            "arguments": json.dumps(
                {
                    "phase": "prepare",
                    "backend_id": str(untouched.installation_id),
                    "resource_id": "node/GPU-123",
                    "membership_epoch": claim.membership_epoch,
                    "proof": None,
                }
            )
        },
    )
    assert collection["result"]["status"] == "closure_valid"

    used = gpu_payload()
    await data.prepare_plugin_installation(used)
    await db_session.execute(
        text(
            "UPDATE public.gpu_backend_fences SET generation_high_water=1 WHERE backend_registry_id=:id"
        ),
        {"id": used.installation_id},
    )
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(
                used.installation_id, 1, "cancel_prepared"
            )
    assert (
        await data.get_plugin_installation(used.installation_id)
    ).installation.status == "prepared"


async def test_prepare_replay_is_identity_bound_and_secret_sensitive(
    db_session, test_engine
):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    first = await data.prepare_plugin_installation(payload)
    assert first.registry_id == payload.installation_id
    assert first.installation.installation_id == payload.installation_id
    assert first.installation.status == "prepared"
    assert first.installation.revision == 1
    pool = await data.singleton_pool(payload.installation_id)
    assert pool is not None
    members = await data.pool_members(pool.id)
    assert [member.registry_id for member in members] == [payload.installation_id]
    assert await data.prepare_plugin_installation(payload) == first
    assert await data.singleton_pool(payload.installation_id) == pool
    assert await data.pool_members(pool.id) == members
    assert await data.get_plugin_installation(payload.installation_id) == first
    assert first in await data.list_plugin_installations()
    assert payload.auth_token not in first.model_dump_json()

    for changed in (
        payload.model_copy(
            update={"auth_token": "different-" + uuid4().hex + uuid4().hex}
        ),
        payload.model_copy(update={"url": payload.url + "/other"}),
        payload.model_copy(update={"name": payload.name + " changed"}),
    ):
        with pytest.raises(ValueError):
            async with db_session.begin_nested():
                await data.prepare_plugin_installation(changed)
    assert await data.get_plugin_installation(payload.installation_id) == first


async def test_generic_registry_mutation_and_delete_cannot_bypass_owner(
    db_session, test_engine
):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    first = await data.prepare_plugin_installation(payload)
    for patch in (
        {"url": "http://replacement.invalid"},
        {"extra_params": {}},
        {"auth_token": "replacement-" + uuid4().hex},
    ):
        with pytest.raises(DBAPIError, match="plugin Registry"):
            async with db_session.begin_nested():
                await data.patch_registry(payload.installation_id, patch)
    with pytest.raises(DBAPIError, match="plugin Registry"):
        async with db_session.begin_nested():
            await data.delete_registry(payload.installation_id, None)
    assert await data.get_plugin_installation(payload.installation_id) == first


async def test_activation_evidence_is_bound_to_installation_and_artifact(
    db_session, test_engine
):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    await data.prepare_plugin_installation(payload)
    evidence = _evidence(payload)
    prepared = await data.command_plugin_installation(
        payload.installation_id, 1, "record_launch", _receipt(payload)
    )
    for field, value in (
        ("installation_id", str(uuid4())),
        ("registry_id", str(uuid4())),
        ("artifact", "registry.example/other@sha256:" + "f" * 64),
        ("endpoint", "http://other.invalid"),
        ("resource", {"logical_device": "cuda:0"}),
    ):
        with pytest.raises(ValueError):
            async with db_session.begin_nested():
                await data.command_plugin_installation(
                    payload.installation_id, 2, "activate", {**evidence, field: value}
                )
        assert await data.get_plugin_installation(payload.installation_id) == prepared


async def test_revision_quiescence_retirement_and_launch_fence(db_session, test_engine):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    prepared = await data.prepare_plugin_installation(payload)
    evidence = _evidence(payload)
    await data.command_plugin_installation(
        payload.installation_id, 1, "record_launch", _receipt(payload)
    )
    active = await data.command_plugin_installation(
        payload.installation_id, 2, "activate", evidence
    )
    assert active.installation.status == "active"
    assert active.installation.revision == 3
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(
                payload.installation_id, prepared.installation.revision, "disable"
            )
    assert await data.get_plugin_installation(payload.installation_id) == active

    disabled = await data.command_plugin_installation(
        payload.installation_id, 3, "disable"
    )
    assert disabled.installation.status == "disabled"
    assert disabled.installation.revision == 4
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(payload.installation_id, 4, "retire")
    proof = {
        **evidence,
        "generation": None,
        "operation_id": str(uuid4()),
        "pool_ids": [str(uuid4())],
    }
    for invalid in (
        {key: value for key, value in proof.items() if key != "operation_id"},
        {**proof, "generation": 1},
    ):
        with pytest.raises(ValueError):
            async with db_session.begin_nested():
                await data.command_plugin_installation(
                    payload.installation_id, 4, "quiesce", invalid
                )
        assert await data.get_plugin_installation(payload.installation_id) == disabled
    stale = {**proof, "boot_id": str(uuid4())}
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(
                payload.installation_id, 4, "quiesce", stale
            )
    assert await data.get_plugin_installation(payload.installation_id) == disabled
    quiesced = await data.command_plugin_installation(
        payload.installation_id, 4, "quiesce", proof
    )
    assert quiesced.installation.status == "disabled"
    assert quiesced.installation.revision == 5
    for launch in (
        evidence,
        {**evidence, "boot_id": str(uuid4())},
        {**evidence, "container_id": "e" * 64},
    ):
        with pytest.raises(ValueError):
            async with db_session.begin_nested():
                await data.command_plugin_installation(
                    payload.installation_id, 5, "activate", launch
                )
    retired = await data.command_plugin_installation(
        payload.installation_id, 5, "retire"
    )
    assert retired.installation.status == "retired"
    assert retired.installation.revision == 6
    assert await data.get_plugin_installation(payload.installation_id) == retired
    assert retired in await data.list_plugin_installations()
    assert await data.prepare_plugin_installation(payload) == retired
    mismatch = payload.model_copy(
        update={"auth_token": "different-" + uuid4().hex + uuid4().hex}
    )
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.prepare_plugin_installation(mismatch)
    assert await data.get_plugin_installation(payload.installation_id) == retired
    row = (
        await db_session.execute(
            text(
                "SELECT auth_token, extra_params FROM public.ml_backend_registry WHERE id=:id"
            ),
            {"id": payload.installation_id},
        )
    ).one()
    digest = hashlib.sha256(payload.auth_token.encode()).hexdigest()
    assert row.auth_token == "retired-sha256:" + digest
    assert payload.auth_token not in json.dumps(row.extra_params)
    assert digest not in json.dumps(row.extra_params["plugin_lifecycle"])
    assert row.extra_params["plugin"]["status"] == "retired"
    assert row.extra_params["plugin_lifecycle"]["stopped"]


async def test_aborted_prepared_launch_can_be_quiesced_and_retired(
    db_session, test_engine
):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    await data.prepare_plugin_installation(payload)
    await data.command_plugin_installation(
        payload.installation_id, 1, "record_launch", _receipt(payload)
    )
    proof = {
        **_evidence(payload),
        "generation": None,
        "operation_id": str(uuid4()),
        "pool_ids": [str(uuid4())],
    }
    quiesced = await data.command_plugin_installation(
        payload.installation_id, 2, "quiesce", proof
    )
    assert quiesced.installation.status == "disabled"
    assert quiesced.installation.revision == 3
    retired = await data.command_plugin_installation(
        payload.installation_id, 3, "retire"
    )
    assert retired.installation.status == "retired"
    assert await data.get_plugin_installation(payload.installation_id) == retired


async def test_stopped_boot_cannot_activate_a_new_recorded_launch(
    db_session, test_engine
):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    old = _evidence(payload)
    await data.prepare_plugin_installation(payload)
    await data.command_plugin_installation(
        payload.installation_id, 1, "record_launch", _receipt(payload)
    )
    await data.command_plugin_installation(
        payload.installation_id,
        2,
        "quiesce",
        {
            **old,
            "generation": None,
            "operation_id": str(uuid4()),
            "pool_ids": [str(uuid4())],
        },
    )
    new_receipt = {**_receipt(payload), "container_id": "e" * 64}
    await data.command_plugin_installation(
        payload.installation_id, 3, "record_launch", new_receipt
    )
    new_evidence = {**old, **new_receipt}
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(
                payload.installation_id, 4, "activate", new_evidence
            )
    active = await data.command_plugin_installation(
        payload.installation_id,
        4,
        "activate",
        {**new_evidence, "boot_id": str(uuid4())},
    )
    assert active.installation.status == "active"


async def test_concurrent_commands_with_same_revision_have_one_winner(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    payload = _payload()
    evidence = _receipt(payload)
    try:
        async with maker.begin() as db:
            await InferenceTopologyData(db).prepare_plugin_installation(payload)
        async with maker() as winner, maker() as loser:
            result = await InferenceTopologyData(winner).command_plugin_installation(
                payload.installation_id, 1, "record_launch", evidence
            )
            started = asyncio.Event()

            async def competing():
                started.set()
                return await InferenceTopologyData(loser).command_plugin_installation(
                    payload.installation_id, 1, "record_launch", evidence
                )

            task = asyncio.create_task(competing())
            await started.wait()
            await winner.commit()
            with pytest.raises(ValueError):
                await task
            await loser.rollback()
            assert result.installation.revision == 2
            assert (
                await InferenceTopologyData(loser).get_plugin_installation(
                    payload.installation_id
                )
                == result
            )
    finally:
        async with maker.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM public.ml_backend_pool_members WHERE registry_id=:id"
                ),
                {"id": payload.installation_id},
            )
            await db.execute(
                text(
                    "DELETE FROM public.ml_backend_service_pools WHERE legacy_instance_id=:id"
                ),
                {"id": payload.installation_id},
            )
            await db.execute(
                text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                {"id": payload.installation_id},
            )


async def test_plugin_registry_fence_allows_routing_but_blocks_disable(test_engine):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    payload = _payload()
    registry_id = payload.installation_id
    world = {"registry_id": registry_id, "pool_id": None}
    owner = await _connect(test_engine)
    client = None
    try:
        async with maker.begin() as db:
            data = InferenceTopologyData(db)
            await data.prepare_plugin_installation(payload)
            await data.command_plugin_installation(
                registry_id, 1, "record_launch", _receipt(payload)
            )
            active = await data.command_plugin_installation(
                registry_id, 2, "activate", _evidence(payload)
            )
            assert active.installation.status == "active"
            pool = await data.pool_for_registry(registry_id)
            assert pool is not None
            pool_id = pool.id
            world["pool_id"] = pool_id
            await db.execute(
                text(
                    "UPDATE public.ml_backend_service_pools SET enabled=true WHERE id=:id"
                ),
                {"id": pool_id},
            )

        identity_hash = await _identity_hash(owner, registry_id)
        client = await _connect(test_engine, role="inference_routing_client")
        async with maker() as holder, maker() as writer:
            facts = await InferenceTopologyData(holder).lock_plugin_registry(
                registry_id
            )
            assert facts is not None
            assert facts.id == registry_id
            database_id = await holder.scalar(
                text("SELECT CAST(:id AS uuid)"), {"id": str(registry_id)}
            )
            request_id = uuid4()
            subject = await capture_backend_endpoint_subject(
                holder, database_id, request_id=request_id, purpose="backend_read"
            )
            assert subject.registry_id == registry_id
            assert subject.request_id == request_id

            # Roll back the real reservation after proving admission so the
            # separate route lifecycle cannot itself fence the later disable.
            route_tx = client.transaction()
            await route_tx.start()
            try:
                snapshot = await _step(
                    client,
                    "admission_snapshot",
                    {
                        "pool_id": str(pool_id),
                        "registry_id": None,
                        "project_id": None,
                        "mode": "off",
                        "required_instance_id": None,
                        "health_max_age_seconds": 60,
                    },
                )
                assert snapshot["status"] == "ok", snapshot
                scope_id = uuid4()
                reserved = await _step(
                    client,
                    "reserve_scope",
                    {
                        "snapshot": snapshot["snapshot"],
                        "candidate_ids": [str(registry_id)],
                        "scope_id": str(scope_id),
                        "command_sha256": "a" * 64,
                        "owner": "api:plugin-fence-test",
                        "operation": "predict",
                        "ttl_ms": 30000,
                    },
                )
                assert reserved["status"] == "ok", reserved
                attempt_id = uuid4()
                began = await _step(
                    client,
                    "begin_attempt",
                    {
                        "scope_id": str(scope_id),
                        "capability": reserved["scope"]["capability"],
                        "attempt_id": str(attempt_id),
                        "method": "POST",
                        "path": "/predict",
                        "transport_identity": {"identity_sha256": identity_hash},
                    },
                )
                assert began["status"] == "ok", began
                assert began["attempt_id"] == str(attempt_id)
            finally:
                await route_tx.rollback()

            await writer.execute(text("SET LOCAL lock_timeout = '250ms'"))
            with pytest.raises(HTTPException) as blocked:
                await InferenceTopologyData(writer).command_plugin_installation(
                    registry_id, 3, "disable"
                )
            assert blocked.value.status_code == 409
            assert blocked.value.detail == {"reason": "project_busy"}
            await writer.rollback()
            await holder.commit()

            disabled = await InferenceTopologyData(writer).command_plugin_installation(
                registry_id, 3, "disable"
            )
            assert disabled.installation.status == "disabled"
            assert disabled.installation.revision == 4
            await writer.commit()
    finally:
        if client is not None:
            await client.close()
        try:
            await _cleanup(owner, world)
        finally:
            await owner.close()


async def test_cuda_qualification_is_required_for_activation(
    db_session, test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    monkeypatch.setattr(
        settings,
        "gpu_arbiter_resources_json",
        json.dumps(
            {
                "node/GPU-123": {
                    "node_id": "node",
                    "physical_device_token": "GPU-123",
                    "allocatable_mb": 8192,
                    "mode": "observe",
                }
            }
        ),
    )
    data = InferenceTopologyData(db_session)
    value = _payload().model_dump(mode="json")
    value["manifest"]["resource"] = {
        "device": "cuda",
        "memory_mb": 1024,
        "gpu_memory_mb": 2048,
    }
    value["resource"] = {
        "gpu_resource_id": "node/GPU-123",
        "physical_gpu_uuid": "GPU-123",
        "logical_device": "cuda:0",
    }
    payload = PreparePluginInstallation.model_validate(value)
    evidence = _evidence(payload, control_epoch=1)
    await data.prepare_plugin_installation(payload)
    await data.command_plugin_installation(
        payload.installation_id, 1, "record_launch", _receipt(payload)
    )
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(
                payload.installation_id, 2, "activate", evidence
            )
    report = {"device": "cuda", "qualified": True}
    digest = hashlib.sha256(
        json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    qualified = await data.command_plugin_installation(
        payload.installation_id,
        2,
        "approve_qualification",
        {"report": report, "sha256": digest},
    )
    assert qualified.installation.revision == 3
    active = await data.command_plugin_installation(
        payload.installation_id, 3, "activate", evidence
    )
    assert active.installation.status == "active"


async def test_gpu_prepare_contention_is_recoverable_without_a_partial_installation(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    gpu_uuid = f"GPU-{uuid4()}"
    resource_id = f"node/{gpu_uuid}"
    monkeypatch.setattr(
        settings,
        "gpu_arbiter_resources_json",
        json.dumps(
            {
                resource_id: {
                    "node_id": "node",
                    "physical_device_token": gpu_uuid,
                    "allocatable_mb": 8192,
                    "mode": "observe",
                }
            }
        ),
    )
    value = _payload().model_dump(mode="json")
    value["manifest"]["resource"] = {
        "device": "cuda",
        "memory_mb": 1024,
        "gpu_memory_mb": 2048,
    }
    value["resource"] = {
        "gpu_resource_id": resource_id,
        "physical_gpu_uuid": gpu_uuid,
        "logical_device": "cuda:0",
    }
    payload = PreparePluginInstallation.model_validate(value)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as holder, maker() as contender:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"aap:gpu-resource:{resource_id}"},
        )
        with pytest.raises(ValueError) as error:
            await InferenceTopologyData(contender).prepare_plugin_installation(payload)
        assert payload.auth_token not in str(error.value)
        await contender.rollback()
        assert (
            await InferenceTopologyData(contender).get_plugin_installation(
                payload.installation_id
            )
            is None
        )
