"""PostgreSQL acceptance for the Native plugin installation owner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from uuid import uuid4, uuid5

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from yoao_inference_contract.plugin import PreparePluginInstallation
from yoao_inference_contract.routing import (
    make_routing_command,
    routing_command_bytes,
    routing_command_sha256,
)
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
from tests.test_video_execution_native import (
    _cleanup_claimed_scope,
    _proof,
    _routing_step as _routing_db_step,
    _step as _jobs_step,
)
from tests.test_video_tracker_input_native import _capture, _seed_tracker_job

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
                "models": [
                    {
                        "id": "detect",
                        "task": "detect",
                        "is_interactive": False,
                        "supported_prompts": ["none"],
                        "supported_inputs": ["full_image", "crop"],
                        "supported_geometric_outputs": ["bbox"],
                    }
                ],
                "pools": {"models": {"cap": 1}},
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


async def _prepare_tracker_route(db, registry_id):
    actor, project, item_id, task_id, job = await _seed_tracker_job(db)
    await _capture(db, actor, job.id)
    claim = (
        await _jobs_step(
            db,
            "claim",
            {"job_id": str(job.id), "worker_id": str(uuid4()), "lease_seconds": 120},
        )
    )["execution"]
    proof = _proof(job.id, claim)
    request_id = uuid4()
    scope_id = uuid5(request_id, "aap-video-execution-scope/v1")
    body = {
        "pool_id": None,
        "registry_id": str(registry_id),
        "project_id": str(project.id),
        "mode": "direct",
        "required_instance_id": str(registry_id),
        "owner": f"tracker:{job.id}",
        "operation": "tracker",
        "execution": proof,
    }
    command = make_routing_command("video_acquire", body, request_id=request_id)
    arguments = {
        **proof,
        "request_id": str(request_id),
        "command": json.loads(routing_command_bytes(command)),
        "command_sha256": routing_command_sha256(command),
        "scope_id": str(scope_id),
    }
    result = await db.scalar(
        text(
            "SELECT aap_inference_routing_v1.video_execution_scope_step('prepare',:args)"
        ),
        {"args": json.dumps(arguments, separators=(",", ":"))},
    )
    if isinstance(result, str):
        result = json.loads(result)
    assert result["status"] == "ok"
    dataset_id = await db.scalar(
        text("SELECT dataset_id FROM public.dataset_items WHERE id=:id"),
        {"id": item_id},
    )
    return {
        "scope_id": scope_id,
        "registry_id": registry_id,
        "proof": proof,
        "request_id": request_id,
        "digest": arguments["command_sha256"],
        "actor_id": actor,
        "project_id": project.id,
        "dataset_id": dataset_id,
        "item_id": item_id,
        "task_id": task_id,
        "job_id": job.id,
    }


async def _reserve_tracker_route(db, route):
    snapshot = await _routing_db_step(
        db,
        "admission_snapshot",
        {
            "pool_id": None,
            "registry_id": str(route["registry_id"]),
            "project_id": str(route["project_id"]),
            "mode": "direct",
            "required_instance_id": str(route["registry_id"]),
            "health_max_age_seconds": 60,
        },
    )
    if snapshot["status"] != "ok":
        return snapshot
    result = await db.scalar(
        text(
            "SELECT aap_inference_routing_v1.video_execution_scope_step('reserve',:args)"
        ),
        {
            "args": json.dumps(
                {
                    **route["proof"],
                    "request_id": str(route["request_id"]),
                    "command_sha256": route["digest"],
                    "scope_id": str(route["scope_id"]),
                    "snapshot": snapshot["snapshot"],
                    "candidate_ids": [str(route["registry_id"])],
                    "ttl_ms": 60000,
                },
                separators=(",", ":"),
            )
        },
    )
    return json.loads(result) if isinstance(result, str) else result


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
            "SELECT yoao_inference_gpu_collector_v1.gpu_collector_step('collect_tombstone',:arguments)"
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
    snapshot = await data.registry_auth_snapshot(payload.installation_id)
    for patch in (
        {"url": "http://replacement.invalid"},
        {"extra_params": {}},
        {"auth_token": "replacement-" + uuid4().hex},
    ):
        with pytest.raises(DBAPIError, match="plugin Registry"):
            async with db_session.begin_nested():
                await data.patch_registry_auth_cas(
                    payload.installation_id, patch, snapshot
                )
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


@pytest.mark.parametrize("scope_state", ["active", "finished", "cancelled"])
async def test_plugin_stop_requires_physical_tracker_retirement(
    db_session, test_engine, scope_state
):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    await data.prepare_plugin_installation(payload)
    evidence = _evidence(payload)
    await data.command_plugin_installation(
        payload.installation_id, 1, "record_launch", _receipt(payload)
    )
    await data.command_plugin_installation(
        payload.installation_id, 2, "activate", evidence
    )
    route = await _prepare_tracker_route(db_session, payload.installation_id)
    assert (await _reserve_tracker_route(db_session, route))["status"] == "ok"
    scope_id = route["scope_id"]
    await db_session.execute(
        text(
            "UPDATE aap_inference_routing_v1.scopes SET state=:state,closed=:closed "
            "WHERE id=:scope"
        ),
        {"scope": scope_id, "state": scope_state, "closed": scope_state != "active"},
    )
    later = await _prepare_tracker_route(db_session, payload.installation_id)
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(
                payload.installation_id, 3, "disable"
            )

    # A completed exact Native retirement, not logical scope closure, permits
    # disabling and subsequently authorizes the stop check.
    await db_session.execute(
        text(
            "UPDATE aap_inference_routing_v1.scopes SET state='reconciled',closed=true "
            "WHERE id=:scope"
        ),
        {"scope": scope_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO aap_inference_routing_v1.scope_retirements "
            "(retirement_id,scope_id,registry_id,authority_epoch,backend_incarnation,"
            "challenge,fence_until,admission_expires_at,transport_identity_sha256,"
            "config_revision,realm,phase) "
            "SELECT :retirement,id,registry_id,authority_epoch,:incarnation,:digest,0,"
            "admission_expires_at,:digest,config_revision,realm,'completed' "
            "FROM aap_inference_routing_v1.scopes WHERE id=:scope"
        ),
        {
            "retirement": uuid4(),
            "scope": scope_id,
            "incarnation": uuid4(),
            "digest": "b" * 64,
        },
    )
    disabled = await data.command_plugin_installation(
        payload.installation_id, 3, "disable"
    )
    await data.check_plugin_tracker_retirement(
        payload.installation_id, disabled.installation.revision
    )
    rejected = await _reserve_tracker_route(db_session, later)
    assert rejected["status"] == "rejected"
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_inference_routing_v1.scopes WHERE id=:scope"
            ),
            {"scope": later["scope_id"]},
        )
        == 0
    )


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


@pytest.mark.parametrize("first", ["disable", "scope"])
async def test_plugin_disable_and_tracker_scope_insertion_serialize(test_engine, first):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    payload = _payload()
    registry_id = payload.installation_id
    route = None
    owner = await _connect(test_engine)
    world = {"registry_id": registry_id, "pool_id": None}
    try:
        async with maker.begin() as db:
            data = InferenceTopologyData(db)
            await data.prepare_plugin_installation(payload)
            await data.command_plugin_installation(
                registry_id, 1, "record_launch", _receipt(payload)
            )
            await data.command_plugin_installation(
                registry_id, 2, "activate", _evidence(payload)
            )
            pool = await data.pool_for_registry(registry_id)
            assert pool is not None
            world["pool_id"] = pool.id
            route = await _prepare_tracker_route(db, registry_id)

        async with maker() as holder, maker() as contender, maker() as observer:
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            contender_pid = await contender.scalar(text("SELECT pg_backend_pid()"))

            async def disable(db):
                return await InferenceTopologyData(db).command_plugin_installation(
                    registry_id, 3, "disable"
                )

            if first == "disable":
                assert (await disable(holder)).installation.status == "disabled"
                result = await _reserve_tracker_route(contender, route)
                assert result["status"] == "rejected"
                assert result["reason"] == "management_busy"
                await contender.rollback()
                await holder.commit()
                rejected = await _reserve_tracker_route(contender, route)
                assert rejected["status"] == "rejected"
                assert (
                    await contender.scalar(
                        text(
                            "SELECT count(*) FROM aap_inference_routing_v1.scopes WHERE id=:scope"
                        ),
                        {"scope": route["scope_id"]},
                    )
                    == 0
                )
                await contender.rollback()
            else:
                assert (await _reserve_tracker_route(holder, route))["status"] == "ok"
                blocked = asyncio.create_task(disable(contender))
                try:
                    async with asyncio.timeout(10):
                        while not await observer.scalar(
                            text(
                                "SELECT :holder = ANY(pg_catalog.pg_blocking_pids(:waiter))"
                            ),
                            {"holder": holder_pid, "waiter": contender_pid},
                        ):
                            await asyncio.sleep(0.02)
                    await holder.commit()
                    with pytest.raises(ValueError):
                        await asyncio.wait_for(blocked, timeout=10)
                    await contender.rollback()
                finally:
                    await holder.rollback()
                    if not blocked.done():
                        blocked.cancel()
                    await asyncio.gather(blocked, return_exceptions=True)
                    await contender.rollback()
            current = await InferenceTopologyData(observer).get_plugin_installation(
                registry_id
            )
            assert current.installation.status == (
                "disabled" if first == "disable" else "active"
            )
    finally:
        try:
            if world["pool_id"] is not None:
                await _cleanup(owner, world)
            if route is not None:
                await _cleanup_claimed_scope(
                    test_engine,
                    actor_id=route["actor_id"],
                    project_id=route["project_id"],
                    dataset_id=route["dataset_id"],
                    item_id=route["item_id"],
                    task_id=route["task_id"],
                    job_id=route["job_id"],
                    scope_id=route["scope_id"],
                )
        finally:
            await owner.close()


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
