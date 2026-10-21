"""Owned PostgreSQL acceptance for Native's scope-retirement boundary."""

from __future__ import annotations

import base64
import json
import os
import uuid

import asyncpg
import pytest

from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import (
    _cleanup,
    _connect,
    _identity_hash,
    _seed,
    _step,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_SCHEMA = "aap_inference_routing_v1"
_ROLE = "inference_routing_client"
_RETIREMENT_STEP = f"{_SCHEMA}.scope_retirement_step"


async def _retirement_step(connection, action: str, args: dict) -> dict:
    raw = await connection.fetchval(
        f"SELECT {_RETIREMENT_STEP}($1::text, $2::text)",
        action,
        json.dumps(args, ensure_ascii=False, separators=(",", ":")),
    )
    return json.loads(raw)


async def _reserve_scope(client, owner, world, label: str) -> tuple[uuid.UUID, str]:
    assert client.is_in_transaction()
    registry_id = world["registry_id"]
    snapshot = await _step(
        client,
        "admission_snapshot",
        {
            "pool_id": None,
            "registry_id": str(registry_id),
            "project_id": None,
            "mode": "direct",
            "required_instance_id": str(registry_id),
            "health_max_age_seconds": 60,
        },
    )
    assert snapshot["status"] == "ok"
    scope_id = uuid.uuid4()
    reserved = await _step(
        client,
        "reserve_scope",
        {
            "snapshot": snapshot["snapshot"],
            "candidate_ids": [str(registry_id)],
            "scope_id": str(scope_id),
            "command_sha256": uuid.uuid4().hex * 2,
            "owner": f"scope-retirement-{label}",
            "operation": "predict",
            "ttl_ms": 60_000,
        },
    )
    assert reserved["status"] == "ok"
    return scope_id, reserved["scope"]["capability"]


async def _begin_attempt(client, owner, world, scope_id, capability):
    attempt_id = uuid.uuid4()
    result = await _step(
        client,
        "begin_attempt",
        {
            "scope_id": str(scope_id),
            "capability": capability,
            "attempt_id": str(attempt_id),
            "method": "POST",
            "path": "/predict",
            "transport_identity": {
                "identity_sha256": await _identity_hash(owner, world["registry_id"])
            },
        },
    )
    assert result["status"] == "ok", result.get("reason")
    return attempt_id, result


def _admission_exp(token: str) -> int:
    payload = token.split(".")[1]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    return int(json.loads(decoded)["exp"])


async def test_native_scope_retirement_is_exact_and_preserves_other_work(test_engine):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    world = await _seed(owner)
    client = await _connect(test_engine, role=_ROLE)
    registry_id = world["registry_id"]
    retirement_id = uuid.uuid4()
    try:
        epoch = uuid.uuid4()
        incarnation = uuid.uuid4()
        await owner.execute(
            """UPDATE aap_inference_routing_v1.registry_state
               SET authority_epoch=$2, backend_incarnation=$3,
                   continuity='ready', epoch_key=public.gen_random_bytes(32)
               WHERE registry_id=$1""",
            registry_id,
            epoch,
            incarnation,
        )

        async with client.transaction():
            policy = await _retirement_step(client, "policy", {})
            assert policy == {
                "schema": "aap-inference-scope-retirement-data/v1",
                "status": "ok",
                "ready": True,
                "functions": [
                    f"{_SCHEMA}.routing_policy()",
                    f"{_SCHEMA}.routing_step(text,text)",
                    f"{_SCHEMA}.scope_retirement_step(text,text)",
                    f"{_SCHEMA}.video_execution_scope_step(text,text)",
                ],
            }
            await client.execute("SAVEPOINT retirement_table_denial")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await client.fetchval(
                    f"SELECT retirement_id FROM {_SCHEMA}.scope_retirements LIMIT 1"
                )
            await client.execute("ROLLBACK TO SAVEPOINT retirement_table_denial")
            await client.execute("RELEASE SAVEPOINT retirement_table_denial")

        async with client.transaction():
            scope_id, capability = await _reserve_scope(client, owner, world, "target")
            active_id, active = await _begin_attempt(
                client, owner, world, scope_id, capability
            )
            uncertain_id, _ = await _begin_attempt(
                client, owner, world, scope_id, capability
            )
            response_id, _ = await _begin_attempt(
                client, owner, world, scope_id, capability
            )
            response = await _step(
                client,
                "settle_attempt",
                {
                    "scope_id": str(scope_id),
                    "capability": capability,
                    "attempt_id": str(response_id),
                    "kind": "response",
                    "status_code": 200,
                    "reason": None,
                },
            )
            assert response["status"] == "ok"
            uncertain = await _step(
                client,
                "settle_attempt",
                {
                    "scope_id": str(scope_id),
                    "capability": capability,
                    "attempt_id": str(uncertain_id),
                    "kind": "uncertain",
                    "status_code": None,
                    "reason": "response_lost",
                },
            )
            assert uncertain["status"] == "ok"

            unrelated_scope_id, unrelated_capability = await _reserve_scope(
                client, owner, world, "unrelated"
            )
            unrelated_id, _ = await _begin_attempt(
                client, owner, world, unrelated_scope_id, unrelated_capability
            )

        exp = _admission_exp(active["headers"]["X-AAP-Route-Admission"])
        recorded_highwater = await owner.fetchval(
            """SELECT admission_expires_at FROM aap_inference_routing_v1.scopes
               WHERE id=$1""",
            scope_id,
        )
        assert int(recorded_highwater.timestamp()) >= exp

        async with client.transaction():
            prepared = await _retirement_step(
                client,
                "prepare",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(scope_id),
                    "capability": capability,
                },
            )
        assert set(prepared) == {"schema", "status", "retirement", "context"}
        assert prepared["schema"] == "aap-inference-scope-retirement-data/v1"
        assert prepared["status"] == "ok"
        assert set(prepared["retirement"]) == {
            "retirement_id",
            "scope_id",
            "registry_id",
            "authority_epoch",
            "backend_incarnation",
            "phase",
            "closed",
        }
        assert prepared["retirement"] == {
            "retirement_id": str(retirement_id),
            "scope_id": str(scope_id),
            "registry_id": str(registry_id),
            "authority_epoch": str(epoch),
            "backend_incarnation": str(incarnation),
            "phase": "prepared",
            "closed": True,
        }
        context = prepared["context"]
        assert set(context) == {
            "retirement_id",
            "scope_id",
            "registry_id",
            "authority_epoch",
            "backend_incarnation",
            "challenge",
            "fence_until",
            "endpoint",
        }
        assert context["endpoint"]["auth_token"].startswith("routing-secret-")
        assert set(context["endpoint"]) == {"url", "auth_method", "auth_token"}
        assert context["fence_until"] >= exp

        async with client.transaction():
            late = await _step(
                client,
                "begin_attempt",
                {
                    "scope_id": str(scope_id),
                    "capability": capability,
                    "attempt_id": str(uuid.uuid4()),
                    "method": "POST",
                    "path": "/predict",
                    "transport_identity": {
                        "identity_sha256": await _identity_hash(owner, registry_id)
                    },
                },
            )
        assert late["status"] == "rejected"
        assert late["reason"] == "scope_closed"

        async with client.transaction():
            status = await _retirement_step(
                client,
                "status",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(scope_id),
                    "capability": capability,
                },
            )
        assert status["status"] == "ok"
        assert status["retirement"]["phase"] == "prepared"
        assert status["context"] is None

        other_capability = await owner.fetchval(
            "SELECT aap_inference_routing_v1._routing_scope_capability($1)",
            unrelated_scope_id,
        )
        async with client.transaction():
            conflict = await _retirement_step(
                client,
                "prepare",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(unrelated_scope_id),
                    "capability": other_capability,
                },
            )
        assert conflict == {
            "schema": "aap-inference-scope-retirement-data/v1",
            "status": "rejected",
            "reason": "retirement_conflict",
        }

        async with client.transaction():
            replay = await _retirement_step(
                client,
                "prepare",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(scope_id),
                    "capability": capability,
                },
            )
        assert replay["status"] == "ok"
        assert replay["context"] == context

        changed_incarnation = {
            "schema": "aap-backend-scope-retirement-reply/v1",
            "retirement_id": str(retirement_id),
            "registry_id": str(registry_id),
            "scope_id": str(scope_id),
            "authority_epoch": str(epoch),
            "backend_incarnation": str(uuid.uuid4()),
            "challenge": context["challenge"],
            "phase": "retired",
            "active_requests": 0,
        }
        async with client.transaction():
            stale = await _retirement_step(
                client,
                "finish",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(scope_id),
                    "capability": capability,
                    "reply": changed_incarnation,
                },
            )
        assert stale["status"] == "rejected"
        assert stale["reason"] == "invalid_command"

        nonzero_retired = {
            **changed_incarnation,
            "backend_incarnation": str(incarnation),
            "active_requests": 1,
        }
        async with client.transaction():
            nonzero = await _retirement_step(
                client,
                "finish",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(scope_id),
                    "capability": capability,
                    "reply": nonzero_retired,
                },
            )
        assert nonzero["status"] == "rejected"
        assert nonzero["reason"] == "invalid_command"

        async with client.transaction():
            draining = await _retirement_step(
                client,
                "finish",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(scope_id),
                    "capability": capability,
                    "reply": {
                        **changed_incarnation,
                        "backend_incarnation": str(incarnation),
                        "phase": "draining",
                    },
                },
            )
        assert draining["status"] == "ok"
        assert draining["retirement"]["phase"] == "draining"
        assert draining["context"] == context
        assert (
            await owner.fetchval(
                """SELECT count(*) FROM aap_inference_routing_v1.attempts
               WHERE scope_id=$1 AND state IN ('active','uncertain')""",
                scope_id,
            )
            == 2
        )

        late_response = await _step(
            client,
            "settle_attempt",
            {
                "scope_id": str(scope_id),
                "capability": capability,
                "attempt_id": str(active_id),
                "kind": "response",
                "status_code": 201,
                "reason": None,
            },
        )
        assert late_response["status"] == "ok"
        final_reply = {
            "schema": "aap-backend-scope-retirement-reply/v1",
            "retirement_id": str(retirement_id),
            "registry_id": str(registry_id),
            "scope_id": str(scope_id),
            "authority_epoch": str(epoch),
            "backend_incarnation": str(incarnation),
            "challenge": context["challenge"],
            "phase": "retired",
            "active_requests": 0,
        }
        async with client.transaction():
            completed = await _retirement_step(
                client,
                "finish",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(scope_id),
                    "capability": capability,
                    "reply": final_reply,
                },
            )
        assert completed["status"] == "ok"
        assert completed["retirement"]["phase"] == "completed"
        assert completed["context"] is None
        assert (
            await owner.fetchval(
                """SELECT state FROM aap_inference_routing_v1.scopes WHERE id=$1""",
                scope_id,
            )
            == "reconciled"
        )
        states = await owner.fetch(
            """SELECT id, state, status_code FROM aap_inference_routing_v1.attempts
               WHERE scope_id=$1 ORDER BY created_at, id""",
            scope_id,
        )
        assert {row["state"] for row in states} == {"response", "reconciled"}
        assert sorted(
            row["status_code"] for row in states if row["state"] == "response"
        ) == [
            200,
            201,
        ]
        assert (
            await owner.fetchval(
                """SELECT state FROM aap_inference_routing_v1.attempts WHERE id=$1""",
                unrelated_id,
            )
            == "active"
        )
        assert (
            await owner.fetchval(
                """SELECT state FROM aap_inference_routing_v1.scopes WHERE id=$1""",
                unrelated_scope_id,
            )
            == "active"
        )

        await _step(
            client,
            "settle_attempt",
            {
                "scope_id": str(unrelated_scope_id),
                "capability": unrelated_capability,
                "attempt_id": str(unrelated_id),
                "kind": "response",
                "status_code": 200,
                "reason": None,
            },
        )
        await _step(
            client,
            "close_scope",
            {
                "scope_id": str(unrelated_scope_id),
                "capability": unrelated_capability,
                "outcome": "success",
            },
        )
        assert (
            await owner.fetchval(
                """SELECT state FROM aap_inference_routing_v1.scopes WHERE id=$1""",
                unrelated_scope_id,
            )
            == "finished"
        )

        async with client.transaction():
            fence_scope_id, fence_capability = await _reserve_scope(
                client, owner, world, "configuration-fence"
            )
        await _step(
            client,
            "close_scope",
            {
                "scope_id": str(fence_scope_id),
                "capability": fence_capability,
                "outcome": "cancel",
            },
        )
        fence_retirement_id = uuid.uuid4()
        fence_prepared = await _retirement_step(
            client,
            "prepare",
            {
                "retirement_id": str(fence_retirement_id),
                "scope_id": str(fence_scope_id),
                "capability": fence_capability,
            },
        )
        fence_context = fence_prepared["context"]
        with pytest.raises(asyncpg.PostgresError) as config_fence:
            await owner.execute(
                "UPDATE public.ml_backend_registry SET auth_token='changed-secret' WHERE id=$1",
                registry_id,
            )
        assert config_fence.value.sqlstate == "55P03"

        async with client.transaction():
            finished_fence = await _retirement_step(
                client,
                "finish",
                {
                    "retirement_id": str(fence_retirement_id),
                    "scope_id": str(fence_scope_id),
                    "capability": fence_capability,
                    "reply": {
                        "schema": "aap-backend-scope-retirement-reply/v1",
                        "retirement_id": str(fence_retirement_id),
                        "registry_id": str(registry_id),
                        "scope_id": str(fence_scope_id),
                        "authority_epoch": str(epoch),
                        "backend_incarnation": str(incarnation),
                        "challenge": fence_context["challenge"],
                        "phase": "retired",
                        "active_requests": 0,
                    },
                },
            )
            assert finished_fence["retirement"]["phase"] == "completed"
        await owner.execute(
            "UPDATE public.ml_backend_registry SET auth_token='changed-secret' WHERE id=$1",
            registry_id,
        )

        async with client.transaction():
            completed_prepare = await _retirement_step(
                client,
                "prepare",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(scope_id),
                    "capability": capability,
                },
            )
        assert completed_prepare["retirement"]["phase"] == "completed"
        assert completed_prepare["context"] is None

        async with client.transaction():
            completed_status = await _retirement_step(
                client,
                "status",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(scope_id),
                    "capability": capability,
                },
            )
        assert completed_status["retirement"]["phase"] == "completed"
        assert completed_status["context"] is None
        async with client.transaction():
            replayed_finish = await _retirement_step(
                client,
                "finish",
                {
                    "retirement_id": str(retirement_id),
                    "scope_id": str(scope_id),
                    "capability": capability,
                    "reply": final_reply,
                },
            )
        assert replayed_finish == completed
    finally:
        await client.close()
        try:
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.scope_retirements WHERE scope_id IN "
                "(SELECT id FROM aap_inference_routing_v1.scopes WHERE registry_id=$1)",
                registry_id,
            )
            await _cleanup(owner, world)
        finally:
            await owner.close()
