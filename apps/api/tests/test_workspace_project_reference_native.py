"""Preserve scalar Project reference fences before consumer policy checks."""

import os
import asyncio
import json
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from fastapi import HTTPException

from app.services.project_write_guard import lock_actor_project_authority
from tests.test_workspace_project_authority_locking import (
    _assert_private_denied,
    _world,
)
from tests.test_workspace_project_access_step_native import _assert_nowait_conflict

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


@pytest.mark.parametrize("nowait", [False, True])
@pytest.mark.parametrize(
    "actor_kind,project_kind",
    [
        ("valid", "exists"),
        ("valid", "bad_date"),
        ("valid", "missing"),
        ("valid", "null"),
        ("missing", "exists"),
        ("none", "exists"),
        ("inactive", "exists"),
        ("none", "missing"),
    ],
)
async def test_reference_guard_keeps_account_and_project_missing_semantics(
    test_engine, nowait, actor_kind, project_kind
):
    async with _world(test_engine) as (owner, _api, world, _url):
        if actor_kind == "inactive" or project_kind == "bad_date":
            async with owner.begin() as seed:
                if actor_kind == "inactive":
                    await seed.execute(
                        text("UPDATE public.users SET is_active=false WHERE id=:id"),
                        {"id": world["member_id"]},
                    )
                if project_kind == "bad_date":
                    await seed.execute(
                        text(
                            "UPDATE public.projects SET due_date=DATE '12000-01-01' WHERE id=:id"
                        ),
                        {"id": world["project_id"]},
                    )
        actor_id = (
            None
            if actor_kind == "none"
            else uuid.uuid4()
            if actor_kind == "missing"
            else world["member_id"]
        )
        project_id = (
            None
            if project_kind == "null"
            else uuid.uuid4()
            if project_kind == "missing"
            else world["project_id"]
        )
        async with owner() as db:
            facts = await lock_actor_project_authority(
                db, actor_id, project_id, nowait=nowait
            )
            if actor_kind in {"none", "missing"}:
                assert facts is None
            else:
                assert facts.id == actor_id and facts.is_active is (
                    actor_kind != "inactive"
                )
            assert await db.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize("nowait", [False, True])
async def test_api_reference_fence_uses_only_owner_functions_and_keeps_key_share(
    test_engine, nowait
):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as seed:
            await seed.execute(
                text(
                    "UPDATE public.projects SET due_date=DATE '12000-01-01' WHERE id=:id"
                ),
                {"id": world["project_id"]},
            )
        async with api() as db:
            await _assert_private_denied(db)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            facts = await lock_actor_project_authority(
                db, world["member_id"], world["project_id"], nowait=nowait
            )
            assert facts.id == world["member_id"] and facts.is_active
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            for table, identity in (
                ("users", world["member_id"]),
                ("projects", world["project_id"]),
            ):
                await _assert_nowait_conflict(
                    test_engine,
                    f"SELECT id FROM public.{table} WHERE id=$1 FOR UPDATE NOWAIT",
                    identity,
                )
            async with owner() as counter:
                await counter.execute(text("SET LOCAL lock_timeout='1s'"))
                await counter.execute(
                    text(
                        "UPDATE public.projects SET total_tasks=total_tasks+1 WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
                await counter.rollback()


async def test_node_reference_and_snapshot_capabilities_need_no_python_proxy(
    test_engine,
):
    async with _world(test_engine) as (_owner, _api, world, url):
        process = await asyncio.create_subprocess_exec(
            "node",
            str(
                Path(__file__).parent / "fixtures/workspace_project_reference_peer.mjs"
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                process.communicate(
                    json.dumps(
                        {
                            "database_url": url.set(
                                drivername="postgresql"
                            ).render_as_string(hide_password=False),
                            "project_id": str(world["project_id"]),
                            "actor_id": str(world["member_id"]),
                        }
                    ).encode()
                ),
                timeout=20,
            )
            assert process.returncode == 0, err.decode()
            assert json.loads(out) == {"ok": True, "transaction_unchanged": True}
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_nowait_reference_function_has_exact_owner_and_application_grants(
    test_engine,
):
    async with _world(test_engine) as (owner, _api, _world_data, _url):
        async with owner() as db:
            signature = "aap_workspace_v1.lock_project_reference_nowait(uuid)"
            row = (
                await db.execute(
                    text(
                        "SELECT pg_get_userbyid(proowner),prosecdef,proisstrict,format_type(prorettype,NULL) FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)"
                    ),
                    {"signature": signature},
                )
            ).one()
            assert row == ("workspace_code", True, True, "uuid")
            for role, expected in (
                ("identity_api_consumer", True),
                ("legacy_runtime", True),
                ("append_client", False),
                ("assets_code", False),
                ("inference_code", False),
                ("workspace_project_deletion_client", False),
            ):
                assert (
                    await db.scalar(
                        text(
                            "SELECT has_function_privilege(:role,:signature,'EXECUTE')"
                        ),
                        {"role": role, "signature": signature},
                    )
                    is expected
                )
            assert not await db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE p.oid=CAST(:signature AS regprocedure) AND a.grantee=0 AND a.privilege_type='EXECUTE')"
                ),
                {"signature": signature},
            )


async def test_wrong_reference_scope_rolls_back_callers_prior_write(
    test_engine, monkeypatch
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            before = await db.scalar(
                text("SELECT name FROM public.projects WHERE id=:id"),
                {"id": world["project_id"]},
            )
        with pytest.raises(HTTPException) as caught:
            async with owner.begin() as db:
                scalar = db.scalar

                async def wrong_scope(statement, *args, **kwargs):
                    result = await scalar(statement, *args, **kwargs)
                    return (
                        uuid.uuid4()
                        if "aap_workspace_v1.lock_project_reference_nowait"
                        in str(statement)
                        else result
                    )

                monkeypatch.setattr(db, "scalar", wrong_scope)
                await db.execute(
                    text(
                        "UPDATE public.projects SET name='uncommitted reference probe' WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
                await lock_actor_project_authority(
                    db, world["member_id"], world["project_id"]
                )
        assert caught.value.status_code == 503
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM public.projects WHERE id=:id"),
                    {"id": world["project_id"]},
                )
                == before
            )
