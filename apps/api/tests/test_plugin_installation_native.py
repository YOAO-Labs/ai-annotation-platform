"""PostgreSQL acceptance for the Native plugin installation owner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference_contract.plugin import PreparePluginInstallation
from app.services.inference_topology_data import InferenceTopologyData
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _payload():
    installation_id = uuid4()
    return PreparePluginInstallation.model_validate({
        "installation_id": installation_id,
        "name": f"Native plugin {installation_id}",
        "url": f"http://plugin-{installation_id}.invalid",
        "auth_token": "secret-" + uuid4().hex + uuid4().hex,
        "manifest": {
            "plugin_id": "libreyolo", "version": "1.0", "publisher": "example",
            "license": "MIT", "artifact": "registry.example/plugin@sha256:" + "a" * 64,
            "sdk_compatibility": ">=1", "entrypoint": ["python", "-m", "plugin"],
            "capabilities": ["inference", "detection"],
            "resource": {"device": "cpu", "memory_mb": 1024, "gpu_memory_mb": 0},
            "asset_manifest_sha256": "b" * 64, "allowed_asset_sources": [],
        },
        "resource": {"logical_device": "cpu"},
    })


def _receipt(payload):
    return {
        "installation_id": str(payload.installation_id),
        "registry_id": str(payload.installation_id),
        "artifact": payload.manifest.artifact,
        "image_id": "sha256:" + "c" * 64,
        "container_id": "d" * 64,
        "resource": payload.resource.model_dump(mode="json"),
    }


def _evidence(payload, *, boot_id=None, control_epoch=1):
    return {
        **_receipt(payload),
        "endpoint": payload.url,
        "boot_id": boot_id or str(uuid4()),
        "control_epoch": control_epoch,
    }


async def test_prepare_replay_is_identity_bound_and_secret_sensitive(db_session, test_engine):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    first = await data.prepare_plugin_installation(payload)
    assert first.registry_id == payload.installation_id
    assert first.installation.installation_id == payload.installation_id
    assert first.installation.status == "prepared"
    assert first.installation.revision == 1
    assert await data.prepare_plugin_installation(payload) == first
    assert await data.get_plugin_installation(payload.installation_id) == first
    assert first in await data.list_plugin_installations()
    assert payload.auth_token not in first.model_dump_json()

    for changed in (
        payload.model_copy(update={"auth_token": "different-" + uuid4().hex + uuid4().hex}),
        payload.model_copy(update={"url": payload.url + "/other"}),
        payload.model_copy(update={"name": payload.name + " changed"}),
    ):
        with pytest.raises(ValueError):
            async with db_session.begin_nested():
                await data.prepare_plugin_installation(changed)
    assert await data.get_plugin_installation(payload.installation_id) == first


async def test_generic_registry_mutation_and_delete_cannot_bypass_owner(db_session, test_engine):
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


async def test_activation_evidence_is_bound_to_installation_and_artifact(db_session, test_engine):
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
    await data.command_plugin_installation(payload.installation_id, 1, "record_launch", _receipt(payload))
    active = await data.command_plugin_installation(payload.installation_id, 2, "activate", evidence)
    assert active.installation.status == "active"
    assert active.installation.revision == 3
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(payload.installation_id, prepared.installation.revision, "disable")
    assert await data.get_plugin_installation(payload.installation_id) == active

    disabled = await data.command_plugin_installation(payload.installation_id, 3, "disable")
    assert disabled.installation.status == "disabled"
    assert disabled.installation.revision == 4
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(payload.installation_id, 4, "retire")
    proof = {**evidence, "generation": 1, "pool_ids": [str(uuid4())]}
    stale = {**proof, "boot_id": str(uuid4())}
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(payload.installation_id, 4, "quiesce", stale)
    assert await data.get_plugin_installation(payload.installation_id) == disabled
    quiesced = await data.command_plugin_installation(payload.installation_id, 4, "quiesce", proof)
    assert quiesced.installation.status == "disabled"
    assert quiesced.installation.revision == 5
    for launch in (evidence, {**evidence, "boot_id": str(uuid4())},
                   {**evidence, "container_id": "e" * 64}):
        with pytest.raises(ValueError):
            async with db_session.begin_nested():
                await data.command_plugin_installation(payload.installation_id, 5, "activate", launch)
    retired = await data.command_plugin_installation(payload.installation_id, 5, "retire")
    assert retired.installation.status == "retired"
    assert retired.installation.revision == 6
    assert await data.get_plugin_installation(payload.installation_id) == retired
    assert retired in await data.list_plugin_installations()
    assert await data.prepare_plugin_installation(payload) == retired
    mismatch = payload.model_copy(update={"auth_token": "different-" + uuid4().hex + uuid4().hex})
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.prepare_plugin_installation(mismatch)
    assert await data.get_plugin_installation(payload.installation_id) == retired
    row = (await db_session.execute(text(
        "SELECT auth_token, extra_params FROM public.ml_backend_registry WHERE id=:id"
    ), {"id": payload.installation_id})).one()
    digest = hashlib.sha256(payload.auth_token.encode()).hexdigest()
    assert row.auth_token == "retired-sha256:" + digest
    assert payload.auth_token not in json.dumps(row.extra_params)
    assert digest not in json.dumps(row.extra_params["plugin_lifecycle"])
    assert row.extra_params["plugin"]["status"] == "retired"
    assert row.extra_params["plugin_lifecycle"]["stopped"]


async def test_aborted_prepared_launch_can_be_quiesced_and_retired(db_session, test_engine):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    await data.prepare_plugin_installation(payload)
    await data.command_plugin_installation(payload.installation_id, 1, "record_launch", _receipt(payload))
    proof = {**_evidence(payload), "generation": 1, "pool_ids": [str(uuid4())]}
    quiesced = await data.command_plugin_installation(payload.installation_id, 2, "quiesce", proof)
    assert quiesced.installation.status == "disabled"
    assert quiesced.installation.revision == 3
    retired = await data.command_plugin_installation(payload.installation_id, 3, "retire")
    assert retired.installation.status == "retired"
    assert await data.get_plugin_installation(payload.installation_id) == retired


async def test_stopped_boot_cannot_activate_a_new_recorded_launch(db_session, test_engine):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    payload = _payload()
    old = _evidence(payload)
    await data.prepare_plugin_installation(payload)
    await data.command_plugin_installation(payload.installation_id, 1, "record_launch", _receipt(payload))
    await data.command_plugin_installation(
        payload.installation_id, 2, "quiesce",
        {**old, "generation": 1, "pool_ids": [str(uuid4())]},
    )
    new_receipt = {**_receipt(payload), "container_id": "e" * 64}
    await data.command_plugin_installation(payload.installation_id, 3, "record_launch", new_receipt)
    new_evidence = {**old, **new_receipt}
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(payload.installation_id, 4, "activate", new_evidence)
    active = await data.command_plugin_installation(
        payload.installation_id, 4, "activate", {**new_evidence, "boot_id": str(uuid4())}
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
            assert await InferenceTopologyData(loser).get_plugin_installation(payload.installation_id) == result
    finally:
        async with maker.begin() as db:
            await db.execute(text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                             {"id": payload.installation_id})


async def test_cuda_qualification_is_required_and_bound_to_launch(db_session, test_engine):
    await assert_owned_test_database(test_engine)
    data = InferenceTopologyData(db_session)
    value = _payload().model_dump(mode="json")
    value["manifest"]["resource"] = {"device": "cuda", "memory_mb": 1024, "gpu_memory_mb": 2048}
    value["resource"] = {
        "gpu_resource_id": "node/gpu", "physical_gpu_uuid": "GPU-123", "logical_device": "cuda:0"
    }
    payload = PreparePluginInstallation.model_validate(value)
    evidence = _evidence(payload)
    await data.prepare_plugin_installation(payload)
    await data.command_plugin_installation(payload.installation_id, 1, "record_launch", _receipt(payload))
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(payload.installation_id, 2, "activate", evidence)
    report = {"device": "cuda", "qualified": True}
    digest = hashlib.sha256(json.dumps(
        report, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()).hexdigest()
    qualified = await data.command_plugin_installation(
        payload.installation_id, 2, "approve_qualification", {"report": report, "sha256": digest}
    )
    assert qualified.installation.revision == 3
    active = await data.command_plugin_installation(payload.installation_id, 3, "activate", evidence)
    assert active.installation.status == "active"
    await data.command_plugin_installation(payload.installation_id, 4, "disable")
    await data.command_plugin_installation(
        payload.installation_id, 5, "quiesce",
        {**evidence, "generation": 1, "pool_ids": [str(uuid4())]},
    )
    new_receipt = {**_receipt(payload), "container_id": "e" * 64}
    recorded = await data.command_plugin_installation(payload.installation_id, 6, "record_launch", new_receipt)
    assert recorded.installation.revision == 7
    with pytest.raises(ValueError):
        async with db_session.begin_nested():
            await data.command_plugin_installation(
                payload.installation_id, 7, "activate",
                {**evidence, **new_receipt, "boot_id": str(uuid4())},
            )
    assert await data.get_plugin_installation(payload.installation_id) == recorded
