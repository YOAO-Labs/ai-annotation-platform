"""Real owner failures must not commit partial writes or misclassify DML contention."""

from __future__ import annotations

from contextlib import AsyncExitStack
import os
import secrets
from types import SimpleNamespace
import uuid

import asyncpg
import httpx
from fastapi import HTTPException
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.application.project_workspace import transfer_project_owner
from app.services import workspace_owner_transfer as commands
from app.services.identity_contracts import IdentityActorFacts
from tests.test_workspace_owner_transfer_live import (
    _cleanup_world,
    _peer,
    _public_transfer,
    _request,
    _seed_world,
    _snapshot,
)
from tests.workspace_owner_transfer_helpers import (
    api_transfer_binding,
    node_provider_url,
    python_provider_url,
    transfer_dsn,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_OWNER_TRANSFER_LIVE") != "1",
        reason="requires the disposable owner-transfer verifier",
    ),
]


async def _binding(stack, test_engine, binding, token):
    dsn = transfer_dsn(test_engine)
    if binding.startswith("remote_"):
        factory = node_provider_url if binding == "remote_node" else python_provider_url
        origin = await stack.enter_async_context(factory(dsn, token))
        await stack.enter_async_context(api_transfer_binding(url=origin, token=token))
    else:
        await stack.enter_async_context(
            api_transfer_binding(
                dsn=dsn if binding == "dedicated" else None, token=token
            )
        )


@pytest.mark.parametrize(
    "binding", ["caller", "dedicated", "remote_python", "remote_node"]
)
@pytest.mark.parametrize(
    "fault", ["dml_busy", "deferred_write", "malformed_reply", "changed_policy"]
)
async def test_owner_failure_fences_rollback_partial_effects(
    test_engine, binding, fault
):
    maker, actor, first, second, employee, project = await _seed_world(
        test_engine, fault
    )
    users = [actor.id, first.id, second.id, employee.id]
    initial = await _snapshot(maker, project.id, actor.id)
    token = secrets.token_urlsafe(32)
    trigger = "owner_transfer_probe_" + uuid.uuid4().hex
    original, signature = None, None
    try:
        async with AsyncExitStack() as stack:
            await _binding(stack, test_engine, binding, token)
            async with test_engine.begin() as db:
                if fault in {"dml_busy", "deferred_write"}:
                    code = "55P03" if fault == "dml_busy" else "23514"
                    await db.execute(
                        text(
                            f"CREATE FUNCTION public.{trigger}() RETURNS trigger LANGUAGE plpgsql "
                            f"AS $$ BEGIN RAISE EXCEPTION USING ERRCODE='{code}', MESSAGE='owned DML probe'; END $$"
                        )
                    )
                    timing = (
                        "CONSTRAINT TRIGGER" if fault == "deferred_write" else "TRIGGER"
                    )
                    event = (
                        "AFTER UPDATE ON public.projects DEFERRABLE INITIALLY DEFERRED"
                        if fault == "deferred_write"
                        else "BEFORE UPDATE ON public.projects"
                    )
                    await db.execute(
                        text(
                            f"CREATE {timing} {trigger} {event} "
                            f"FOR EACH ROW WHEN (OLD.id='{project.id}'::uuid) EXECUTE FUNCTION public.{trigger}()"
                        )
                    )
                else:
                    signature = "aap_workspace_owner_transfer_v1." + (
                        "owner_transfer_policy()"
                        if fault == "changed_policy"
                        else "transfer_project_owner(jsonb)"
                    )
                    original = await db.scalar(
                        text(
                            "SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"
                        ),
                        {"signature": signature},
                    )
                    if fault == "changed_policy":
                        replacement = original.replace(
                            "aap-workspace-owner-transfer/v1", "wrong/v1"
                        )
                    else:
                        # Corrupt only the returned binding after Native has performed its write.
                        replacement = original.replace(
                            "RETURN v_result;",
                            "RETURN pg_catalog.jsonb_set(v_result, '{actor_id}', pg_catalog.to_jsonb('00000000-0000-0000-0000-000000000000'::text));",
                        )
                    assert replacement != original
                    await db.execute(text(replacement))
            async with maker() as caller:
                with pytest.raises(Exception) as rejected:
                    await transfer_project_owner(
                        caller,
                        actor=IdentityActorFacts(
                            id=actor.id,
                            name=actor.name,
                            email=actor.email,
                            role=actor.role,
                            is_active=actor.is_active,
                        ),
                        project_id=project.id,
                        new_owner_id=first.id,
                    )
                    if fault == "deferred_write" and binding == "caller":
                        await caller.commit()
                if fault in {"dml_busy", "deferred_write"} and binding in {
                    "caller",
                    "dedicated",
                }:
                    expected = (
                        DBAPIError if binding == "caller" else asyncpg.PostgresError
                    )
                    assert isinstance(rejected.value, expected)
                    error = (
                        rejected.value.orig if binding == "caller" else rejected.value
                    )
                    assert error.sqlstate == (
                        "55P03" if fault == "dml_busy" else "23514"
                    )
                    if binding == "caller" and fault == "dml_busy":
                        with pytest.raises(DBAPIError) as aborted:
                            await caller.scalar(text("SELECT 1"))
                        assert aborted.value.orig.sqlstate == "25P02"
                else:
                    assert isinstance(rejected.value, HTTPException)
                    assert rejected.value.status_code == (
                        500 if fault in {"dml_busy", "deferred_write"} else 503
                    )
                    if binding == "caller":
                        assert not caller.in_transaction()
                await caller.rollback()
            assert await _snapshot(maker, project.id, actor.id) == initial
            async with maker() as observer:
                assert (
                    await observer.scalar(
                        text(
                            "SELECT count(*) FROM public.workspace_owner_transfer_operation_receipts WHERE project_id=:project"
                        ),
                        {"project": project.id},
                    )
                    == 0
                )
    finally:
        async with test_engine.begin() as db:
            if fault in {"dml_busy", "deferred_write"}:
                await db.execute(
                    text(f"DROP TRIGGER IF EXISTS {trigger} ON public.projects")
                )
                await db.execute(text(f"DROP FUNCTION IF EXISTS public.{trigger}()"))
            elif original is not None:
                await db.execute(text(original))
        await _cleanup_world(maker, actor.id, project.id, users)


async def test_python_owner_accepts_the_independent_node_consumer(test_engine):
    maker, actor, first, second, employee, project = await _seed_world(
        test_engine, "python-peer"
    )
    token = secrets.token_urlsafe(32)
    try:
        async with python_provider_url(transfer_dsn(test_engine), token) as origin:
            result = _peer(origin, token, _request(actor.id, project.id, first.id))
            assert result["ok"] is True and result["reply"]["ok"] is True
            assert (await _snapshot(maker, project.id, actor.id))[0] == first.id
    finally:
        await _cleanup_world(
            maker, actor.id, project.id, [actor.id, first.id, second.id, employee.id]
        )


async def test_public_api_recovers_a_reply_lost_after_node_commit(
    test_engine, httpx_client, monkeypatch
):
    maker, actor, first, second, employee, project = await _seed_world(
        test_engine, "api-loss"
    )
    token, operation_id = secrets.token_urlsafe(32), uuid.uuid4()
    monkeypatch.setattr(commands, "uuid", SimpleNamespace(uuid4=lambda: operation_id))
    try:
        async with node_provider_url(
            transfer_dsn(test_engine), token, drop_operation_id=str(operation_id)
        ) as origin:
            async with api_transfer_binding(url=origin, token=token):
                response = await _public_transfer(
                    httpx_client, project.id, first.id, actor
                )
                assert response.status_code == 200, response.text
        assert (await _snapshot(maker, project.id, actor.id))[0] == first.id
        async with maker() as observer:
            assert (
                await observer.scalar(
                    text(
                        "SELECT count(*) FROM public.workspace_owner_transfer_operation_receipts WHERE operation_id=:operation"
                    ),
                    {"operation": operation_id},
                )
                == 1
            )
    finally:
        await _cleanup_world(
            maker, actor.id, project.id, [actor.id, first.id, second.id, employee.id]
        )


async def test_installed_runtime_combines_both_restricted_capabilities(test_engine):
    from tests.test_workspace_membership_runtime import _workspace_dsn

    maker, actor, first, second, employee, project = await _seed_world(
        test_engine, "both-capabilities"
    )
    token = secrets.token_urlsafe(32)
    try:
        async with python_provider_url(
            transfer_dsn(test_engine), token, membership_dsn=_workspace_dsn(test_engine)
        ) as origin:
            result = _peer(origin, token, _request(actor.id, project.id, first.id))
            assert result["reply"]["ok"] is True
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                assert (await client.get(origin + "/ready")).status_code == 200
                response = await client.post(
                    origin + "/v1/workspace/membership/execute",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "schema": "aap-workspace-membership/v1",
                        "operation_id": str(uuid.uuid4()),
                        "operation": "remove_member",
                        "actor_id": str(actor.id),
                        "project_id": str(project.id),
                        "payload": {"member_id": str(uuid.uuid4())},
                        "audit_context": {
                            "method": None,
                            "path": None,
                            "ip": None,
                            "request_id": None,
                        },
                    },
                )
                assert response.status_code == 200 and response.json()["ok"] is False
                assert response.json()["error"]["status_code"] == 404
    finally:
        await _cleanup_world(
            maker, actor.id, project.id, [actor.id, first.id, second.id, employee.id]
        )
