"""Native ownership, stale claims and fair progress under unresolved byte work."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc_runtime.database import AsyncpgMaskGcData
from tests.test_mask_gc_native import _native_call, _stage_one_old_candidate, _key
from app.services import mask_gc


pytestmark = pytest.mark.asyncio


def _receipt(claim, *, state="completed", outcome="deleted"):
    return {
        "operation_id": claim["operation_id"],
        "request": {
            field: claim[field]
            for field in ("operation_id", "object_key", "etag", "last_modified")
        },
        "state": state,
        "outcome": outcome,
    }


def _finish(claim, *, receipt=None, error_code=None):
    return {
        **{
            field: claim[field]
            for field in ("operation_id", "generation", "lease_token")
        },
        "receipt": receipt,
        "error_code": error_code,
    }


async def _claim(db, worker, limit=1):
    result = await _native_call(
        db,
        "claim_mask_gc_deletes",
        {"worker_id": str(worker), "limit": limit, "lease_seconds": 30},
    )
    assert result["ok"] is True, result
    return result["data"]["items"]


@pytest.mark.parametrize("state", ["pending", "indeterminate", "unavailable"])
async def test_released_and_superseded_claims_cannot_clear_an_uncertain_fence(
    db_session, state
):
    key = _key()
    worker, _ = await _stage_one_old_candidate(db_session, key)
    claim = (await _claim(db_session, worker))[0]
    uncertain = await _native_call(
        db_session,
        "finish_mask_gc_delete",
        _finish(
            claim,
            receipt=None
            if state == "unavailable"
            else _receipt(claim, state=state, outcome=None),
            error_code="peer_unavailable" if state == "unavailable" else None,
        ),
    )
    assert uncertain["ok"] is True, uncertain
    assert uncertain["data"]["completed"] is False
    terminal = _finish(claim, receipt=_receipt(claim))
    stale = await _native_call(db_session, "finish_mask_gc_delete", terminal)
    assert stale["error"]["code"] == "lease_lost"
    blocked = await _native_call(
        db_session, "read_mask_write_epochs", {"object_keys": [key]}
    )
    assert blocked["error"]["code"] == "content_deleting"

    await db_session.execute(
        text(
            "UPDATE mask_gc_intents SET next_check_at=clock_timestamp() WHERE operation_id=CAST(:id AS uuid)"
        ),
        {"id": claim["operation_id"]},
    )
    takeover = (await _claim(db_session, uuid4()))[0]
    assert takeover["operation_id"] == claim["operation_id"]
    assert takeover["generation"] == claim["generation"] + 1
    stale = await _native_call(db_session, "finish_mask_gc_delete", terminal)
    assert stale["error"]["code"] == "lease_lost"
    wrong_receipt = _receipt(takeover)
    wrong_receipt["request"]["etag"] = "unrelated-content"
    wrong = await _native_call(
        db_session, "finish_mask_gc_delete", _finish(takeover, receipt=wrong_receipt)
    )
    assert wrong["error"]["code"] == "invalid_response"
    finished = await _native_call(
        db_session,
        "finish_mask_gc_delete",
        _finish(takeover, receipt=_receipt(takeover)),
    )
    assert finished["data"]["completed"] is True
    stale_preflight = await _native_call(
        db_session,
        "lock_mask_write_epochs",
        {"items": [{"object_key": key, "epoch": 0}]},
    )
    assert stale_preflight["error"]["code"] == "content_epoch_changed"
    assert (
        await _native_call(db_session, "read_mask_write_epochs", {"object_keys": [key]})
    )["data"]["items"] == [{"object_key": key, "epoch": 1}]


async def test_failed_first_thousand_keys_do_not_starve_next_inventory_page(db_session):
    worker = uuid4()
    prefix = uuid4().bytes
    digests = sorted(
        hashlib.sha256(prefix + index.to_bytes(4)).hexdigest() for index in range(1001)
    )
    keys = [
        f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json"
        for digest in digests
    ]
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    claimed = await _native_call(
        db_session, "claim_mask_gc_sweep", {"worker_id": str(worker)}
    )
    sweep = claimed["data"]["sweep"]
    after = None
    for page_keys, complete in ((keys[:1000], False), (keys[1000:], True)):
        staged = await _native_call(
            db_session,
            "stage_mask_gc_page",
            {
                **{
                    field: sweep[field]
                    for field in ("sweep_id", "generation", "lease_token")
                },
                "after_key": after,
                "page": {
                    "items": [
                        {
                            "object_key": key,
                            "etag": "old",
                            "last_modified": old,
                            "byte_size": 1,
                        }
                        for key in page_keys
                    ],
                    "after_key": page_keys[-1],
                    "complete": complete,
                },
            },
        )
        assert staged["ok"] is True, staged
        assert staged["data"]["staged"] == len(page_keys)
        after = page_keys[-1]
    assert staged["data"]["sweep"]["complete"] is True
    assert staged["data"]["sweep"]["after_key"] == keys[-1]
    attempted = []
    for _ in range(10):
        claims = await _claim(db_session, worker, 100)
        assert len(claims) == 100
        for claim in claims:
            attempted.append(claim["object_key"])
            unresolved = await _native_call(
                db_session,
                "finish_mask_gc_delete",
                _finish(claim, error_code="peer_unavailable"),
            )
            assert unresolved["data"]["state"] == "indeterminate"
    assert attempted == keys[:1000]
    assert [claim["object_key"] for claim in await _claim(db_session, worker)] == keys[
        1000:
    ]


async def test_gc_login_has_only_cleanup_functions_and_no_write_permits():
    url = os.getenv("AAP_MODULE_DATA_LABELING_MASK_GC_URL")
    if not url:
        pytest.skip("requires the owned module-data role verifier")
    dsn = (
        make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        async with pool.acquire() as connection:
            assert (
                await connection.fetchval("SELECT current_user")
                == "labeling_mask_gc_client"
            )
            functions = await connection.fetch(
                "SELECT proc.proname FROM pg_proc AS proc JOIN pg_namespace AS ns ON ns.oid=proc.pronamespace "
                "WHERE ns.nspname LIKE 'aap_%' AND has_function_privilege(current_user,proc.oid,'execute')"
            )
            assert {row[0] for row in functions} == {
                "claim_mask_gc_sweep",
                "stage_mask_gc_page",
                "claim_mask_gc_deletes",
                "finish_mask_gc_delete",
                "read_mask_gc_progress",
            }
            assert (
                await connection.fetchval(
                    "SELECT has_schema_privilege(current_user,'public','create')"
                )
                is False
            )
            assert (
                await connection.fetchval(
                    "SELECT has_database_privilege(current_user,current_database(),'TEMP')"
                )
                is False
            )
            for query in (
                "SELECT * FROM public.mask_gc_intents",
                "SELECT * FROM public.tasks",
                "SELECT aap_labeling_v1.read_mask_write_epochs('{\"object_keys\": []}'::jsonb)",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute(query)
        adapter = AsyncpgMaskGcData(pool)
        result = await adapter.call(
            "read_mask_gc_progress", c.ReadMaskGcProgressRequest()
        )
        assert isinstance(result, c.ReadMaskGcProgressResult)
    finally:
        await pool.close()


async def test_legacy_runtime_can_use_write_gates_in_its_own_transaction(test_engine):
    url = os.getenv("AAP_MODULE_DATA_LEGACY_RUNTIME_URL")
    if not url:
        pytest.skip("requires the owned module-data role verifier")
    target = make_url(url)
    assert (target.host, target.port, target.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    engine = create_async_engine(url)
    key = _key()
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(bind=connection) as db:
                    assert await mask_gc.read_mask_write_epochs(db, [key]) == {key: 0}
                    assert await mask_gc.begin_mask_content_write(db, [key]) == {key: 0}
                    assert await mask_gc.lock_mask_write_epochs(db, {key: 0}) == {
                        key: 0
                    }
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
