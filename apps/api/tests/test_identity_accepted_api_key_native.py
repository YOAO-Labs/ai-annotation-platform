"""Accepted API-key authority stays locked in the business caller transaction."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from aap_identity.account_lock_contract import accepted_api_key_schema
from app.db.models.api_key import ApiKey
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.training_dataset_bridge import TrainingDatasetError
from app.services.training_datasets import bind_training_authority
from tests.test_training_authority_contract import (
    cleanup_authority_rows,
    seed_authority_rows,
)

_SCOPES = frozenset({"datasets:read", "annotations:read"})
_FUNCTION = "aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])"


def test_accepted_key_sql_descriptor_matches_the_portable_contract():
    path = (
        Path(__file__).parents[1]
        / "alembic/sql/0285_identity_accepted_api_key.schema.json"
    )
    expected = accepted_api_key_schema()
    assert json.loads(path.read_text()) == expected
    from importlib.resources import files

    assert (
        json.loads(
            files("aap_identity")
            .joinpath("accepted_api_key_protocol.schema.json")
            .read_text()
        )
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["actor", "key", "scopes", "null_member", "dimensions"]
)
async def test_accepted_key_invalid_arguments_keep_native_error_and_aborted_transaction(
    test_engine, invalid
):
    async with test_engine.connect() as caller:
        arguments = {
            "actor": None if invalid == "actor" else uuid.uuid4(),
            "key": None if invalid == "key" else uuid.uuid4(),
            "scopes": (
                None
                if invalid == "scopes"
                else [None]
                if invalid == "null_member"
                else [["one"], ["two"]]
                if invalid == "dimensions"
                else []
            ),
        }
        with pytest.raises(DBAPIError) as rejected:
            await caller.scalar(
                text(
                    "SELECT aap_identity_v1.check_accepted_api_key(CAST(:actor AS uuid), CAST(:key AS uuid), CAST(:scopes AS text[]))"
                ),
                arguments,
            )
        assert getattr(rejected.value.orig, "sqlstate", None) == "22023"
        with pytest.raises(DBAPIError) as aborted:
            await caller.scalar(text("SELECT 1"))
        assert getattr(aborted.value.orig, "sqlstate", None) == "25P02"
        await caller.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,expected",
    [
        ("valid", True),
        ("wildcard", True),
        ("missing_scope", False),
        ("revoked", False),
        ("expired", False),
        ("missing_key", False),
        ("wrong_owner", False),
        ("rotated_hash", True),
        ("empty_required", True),
    ],
)
async def test_accepted_key_checks_current_authority_without_possession_or_writes(
    test_engine, state, expected
):
    maker, ids = await seed_authority_rows(test_engine)
    actor_id, _project_id, key_id = ids
    required = _SCOPES
    try:
        async with maker() as setup:
            values = {}
            if state == "wildcard":
                values["scopes"] = ["*"]
            elif state == "missing_scope":
                values["scopes"] = ["datasets:read"]
            elif state == "revoked":
                values["revoked_at"] = datetime.now(timezone.utc)
            elif state == "expired":
                values["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            elif state == "rotated_hash":
                values["key_hash"] = "new-secret-hash-never-sent-to-the-consumer"
            elif state == "empty_required":
                values["scopes"] = []
                required = frozenset()
            if values:
                await setup.execute(
                    update(ApiKey).where(ApiKey.id == key_id).values(**values)
                )
            await setup.commit()
        async with maker() as caller:
            before = await caller.scalar(
                text("SELECT to_jsonb(k) FROM public.api_keys k WHERE id=:id"),
                {"id": key_id},
            )
            assert (
                await IdentityAccountLockData(caller).accepted_api_key_authorized(
                    uuid.uuid4() if state == "wrong_owner" else actor_id,
                    uuid.uuid4() if state == "missing_key" else key_id,
                    required,
                )
                is expected
            )
            after = await caller.scalar(
                text("SELECT to_jsonb(k) FROM public.api_keys k WHERE id=:id"),
                {"id": key_id},
            )
            assert after == before
            await caller.rollback()
    finally:
        await cleanup_authority_rows(maker, ids)


async def _wait_for_key_lock(observer, pid):
    async with asyncio.timeout(5):
        while True:
            waiting = await observer.scalar(
                text(
                    "SELECT wait_event_type='Lock' FROM pg_catalog.pg_stat_activity WHERE pid=:pid"
                ),
                {"pid": pid},
            )
            if waiting:
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "scope", "expiry_during_wait"])
async def test_final_training_authority_waits_then_rechecks_the_current_key(
    test_engine, change
):
    maker, ids = await seed_authority_rows(test_engine)
    actor_id, project_id, key_id = ids
    pending = None
    try:
        async with maker() as blocker, maker() as caller, maker() as observer:
            await blocker.scalar(
                select(ApiKey).where(ApiKey.id == key_id).with_for_update()
            )
            values = {
                "revoked": {"revoked_at": datetime.now(timezone.utc)},
                "scope": {"scopes": ["datasets:read"]},
                "expiry_during_wait": {
                    "expires_at": datetime.now(timezone.utc) + timedelta(seconds=1)
                },
            }[change]
            await blocker.execute(
                update(ApiKey).where(ApiKey.id == key_id).values(**values)
            )
            # Fix the caller transaction's timestamp before expiry; now() would
            # incorrectly authorize it after the key lock finally becomes free.
            await caller.scalar(text("SELECT CURRENT_TIMESTAMP"))
            pid = await caller.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(
                bind_training_authority(caller).assert_authority(
                    project_id=project_id, actor_id=actor_id, api_key_id=str(key_id)
                )
            )
            await _wait_for_key_lock(observer, pid)
            assert not pending.done()
            if change == "expiry_during_wait":
                async with asyncio.timeout(5):
                    while not await observer.scalar(
                        text("SELECT clock_timestamp() > :expiry"),
                        {"expiry": values["expires_at"]},
                    ):
                        await asyncio.sleep(0.01)
            await blocker.commit()
            with pytest.raises(TrainingDatasetError) as denied:
                await pending
            assert (denied.value.code, denied.value.status_code) == (
                "api_key_revoked",
                403,
            )
            await caller.rollback()
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await cleanup_authority_rows(maker, ids)


@pytest.mark.asyncio
async def test_accepted_key_lock_error_aborts_only_the_callers_transaction(test_engine):
    maker, ids = await seed_authority_rows(test_engine)
    actor_id, _project_id, key_id = ids
    try:
        async with maker() as blocker, maker() as caller:
            await blocker.scalar(
                select(ApiKey).where(ApiKey.id == key_id).with_for_update()
            )
            await caller.execute(text("SET LOCAL lock_timeout='50ms'"))
            with pytest.raises(DBAPIError) as busy:
                await IdentityAccountLockData(caller).accepted_api_key_authorized(
                    actor_id, key_id, _SCOPES
                )
            assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
            with pytest.raises(DBAPIError) as aborted:
                await caller.scalar(text("SELECT 1"))
            assert getattr(aborted.value.orig, "sqlstate", None) == "25P02"
            await caller.rollback()
            assert await caller.scalar(text("SELECT 1")) == 1
            await blocker.rollback()
    finally:
        await cleanup_authority_rows(maker, ids)


@pytest.mark.asyncio
async def test_learning_owner_calls_only_the_key_capability_without_identity_table_access(
    test_engine,
):
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("requires the verifier-owned code owners and grants")
    maker, ids = await seed_authority_rows(test_engine)
    actor_id, _project_id, key_id = ids
    try:
        async with maker() as caller:
            await caller.execute(text("SET LOCAL ROLE learning_code"))
            assert await IdentityAccountLockData(caller).accepted_api_key_authorized(
                actor_id, key_id, _SCOPES
            )
            assert await caller.scalar(
                text(
                    "SELECT pg_catalog.has_function_privilege('learning_code', :fn, 'EXECUTE')"
                ),
                {"fn": _FUNCTION},
            )
            for role in (
                "identity_auth_client",
                "identity_key_client",
                "append_client",
            ):
                assert not await caller.scalar(
                    text(
                        "SELECT pg_catalog.has_function_privilege(CAST(:role AS name), :fn, 'EXECUTE')"
                    ),
                    {"role": role, "fn": _FUNCTION},
                )
            with pytest.raises(DBAPIError) as denied:
                await caller.execute(text("SELECT user_id,scopes FROM public.api_keys"))
            assert getattr(denied.value.orig, "sqlstate", None) == "42501"
            await caller.rollback()
    finally:
        await cleanup_authority_rows(maker, ids)


@pytest.mark.asyncio
async def test_learning_owner_keeps_account_and_key_facts_inside_the_same_transaction(
    test_engine,
):
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("requires the verifier-owned code owners and grants")
    maker, ids = await seed_authority_rows(test_engine)
    actor_id, _project_id, key_id = ids
    try:
        async with maker() as caller:
            await caller.execute(text("SET LOCAL ROLE learning_code"))
            data = IdentityAccountLockData(caller)
            accounts = await data.lock_accounts([actor_id], mode="share", nowait=True)
            actor = await data.read_actor_status(actor_id)
            assert actor is not None
            assert (actor.id, actor.role, actor.is_active) == (
                accounts[actor_id].id,
                accounts[actor_id].role,
                accounts[actor_id].is_active,
            )
            assert await data.accepted_api_key_authorized(actor_id, key_id, _SCOPES)
            assert caller.in_transaction()
            with pytest.raises(DBAPIError) as denied:
                await caller.execute(text("SELECT id FROM public.users"))
            assert getattr(denied.value.orig, "sqlstate", None) == "42501"
            await caller.rollback()
    finally:
        await cleanup_authority_rows(maker, ids)
