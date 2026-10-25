"""Caller-transaction qualification for Identity-owned account write facts."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
import subprocess
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user
from tests.test_identity_admission_live import ROOT

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_WRITE_FENCES_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled account write-fence verifier",
    ),
]


def _identity(tag: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aap-identity-write-fences/{tag}")


def _account_facts(actor) -> dict:
    return {
        "id": str(actor.id),
        "name": actor.name,
        "email": actor.email,
        "role": actor.role,
        "is_active": actor.is_active,
    }


def _failure(error: HTTPException) -> dict:
    return {
        "status": error.status_code,
        "detail": error.detail,
        "headers": error.headers,
    }


def _legacy_samples() -> dict:
    path = ROOT / "apps/api/tests/fixtures/identity_write_fences_legacy.json"
    if not path.is_file():
        pytest.fail("Root did not provide the frozen legacy write-fence samples")
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result.get("kind") == "actual-legacy-account-write-fences"
    return result["records"]


async def _seed_legacy_world(test_engine) -> tuple[async_sessionmaker, dict]:
    from sqlalchemy.ext.asyncio import AsyncSession

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    fixture = {
        "users": {},
        "user_ids": [],
        "project_id": _identity("project"),
        "project_ids": [_identity("project")],
    }
    specs = (
        ("owner", "project_admin", True, None),
        ("employee", "employee", True, "annotator"),
        ("viewer", "viewer", True, "viewer"),
        ("incompatible", "viewer", True, "annotator"),
        ("bad_member", "employee", True, "historic_role"),
        ("legacy", "reviewer", True, None),
        ("inactive", "employee", False, "annotator"),
        ("absent_member", "employee", True, None),
    )
    async with maker.begin() as db:
        for tag, role, active, _member_role in specs:
            user = await create_user(
                db,
                role,
                f"{tag}@write-fences.test",
                "  Owner ß  " if tag == "owner" else f"  {tag} 😀 ",
            )
            user.id = _identity(tag)
            user.is_active = active
            fixture["users"][tag] = user.id
            fixture["user_ids"].append(user.id)
            await db.flush()
        owner_id = fixture["users"]["owner"]
        project = await create_project(
            db, owner_id=owner_id, name="Write fence baseline"
        )
        project.id = fixture["project_id"]
        await db.flush()
        for tag, _role, _active, member_role in specs:
            if member_role is None:
                continue
            member = await create_membership(
                db,
                project_id=project.id,
                user_id=fixture["users"][tag],
                role=member_role,
                assigned_by=owner_id,
            )
            member.id = _identity(f"membership-{tag}")
            await db.flush()
    return maker, fixture


async def _cleanup_legacy_world(maker, fixture):
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(
            delete(ProjectMember).where(
                ProjectMember.project_id.in_(fixture["project_ids"])
            )
        )
        await db.execute(delete(Project).where(Project.id.in_(fixture["project_ids"])))
        await db.execute(delete(User).where(User.id.in_(fixture["user_ids"])))


def _append_client_target(test_engine) -> tuple[str, str]:
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres
    from tests.test_module_data_roles import _role_url

    append_url = make_url(_role_url("AAP_MODULE_DATA_APPEND_URL", "append_client"))
    assert append_url.username == "append_client"
    assert append_url.host == "127.0.0.1"
    assert append_url.database == test_engine.url.database
    assert append_url.port == test_engine.url.port
    assert append_url.database.endswith("_test")
    _verify_postgres(test_engine.url)
    container_id = os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"]
    return container_id, append_url.database


def _node_account_lock_peer(*, container_id, database, account_ids, mode, nowait):
    peer = ROOT / "apps/api/tests/fixtures/identity_account_locks_peer.mjs"
    result = subprocess.run(
        ["node", str(peer)],
        input=json.dumps(
            {
                "container_id": container_id,
                "database": database,
                "request": {
                    "schema": "aap-identity-account-locks/v1",
                    "account_ids": [str(value) for value in account_ids],
                    "mode": mode,
                    "nowait": nowait,
                },
            },
            ensure_ascii=False,
            allow_nan=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "Independent append-client account-lock peer failed"
    return json.loads(result.stdout)


async def _legacy_access_records(maker, fixture) -> list[dict]:
    from app.services.project_access import lock_current_project_access

    tags = (
        "owner",
        "employee",
        "viewer",
        "incompatible",
        "bad_member",
        "legacy",
        "inactive",
        "absent_member",
        "missing",
    )
    rows = []
    for tag in tags:
        actor_id = _identity(tag)
        async with maker() as caller:
            try:
                actor, project, access = await lock_current_project_access(
                    caller, actor_id=actor_id, project_id=fixture["project_id"]
                )
                value = asdict(access)
                for key, item in list(value.items()):
                    if isinstance(item, uuid.UUID):
                        value[key] = str(item)
                    elif isinstance(item, frozenset):
                        value[key] = sorted(item)
                rows.append(
                    {
                        "case": tag,
                        "actor": _account_facts(actor),
                        "project_id": str(project.id),
                        "access": value,
                    }
                )
            except HTTPException as error:
                rows.append({"case": tag, "failure": _failure(error)})
            finally:
                await caller.rollback()
    return rows


async def _legacy_contention_records(maker, fixture) -> list[dict]:
    from app.services.project_access import lock_current_project_access
    from app.services.project_write_guard import (
        lock_actor_account,
        lock_actor_project_authority,
    )
    from app.services.user_lifecycle import UserLifecycleService

    owner_id = fixture["users"]["owner"]
    project_id = fixture["project_id"]
    calls = (
        ("account", lambda db: lock_actor_account(db, owner_id)),
        (
            "project_authority_account",
            lambda db: lock_actor_project_authority(db, owner_id, project_id),
        ),
        (
            "access_account",
            lambda db: lock_current_project_access(
                db, actor_id=owner_id, project_id=project_id
            ),
        ),
        (
            "lifecycle",
            lambda db: UserLifecycleService.lock_accounts(db, [owner_id]),
        ),
    )
    records = []
    for label, call in calls:
        async with maker() as holder, maker() as caller:
            await holder.execute(
                select(User.id).where(User.id == owner_id).with_for_update()
            )
            try:
                await call(caller)
                pytest.fail(f"Expected held-account contention for {label}")
            except HTTPException as error:
                record = {
                    "case": label,
                    "failure": _failure(error),
                    "in_transaction": caller.in_transaction(),
                }
                try:
                    await caller.scalar(text("SELECT 1"))
                    record["subsequent_sqlstate"] = None
                except DBAPIError as aborted:
                    record["subsequent_sqlstate"] = getattr(
                        aborted.orig, "sqlstate", None
                    )
                records.append(record)
            finally:
                await caller.rollback()
                await holder.rollback()
    return records


async def _legacy_wait_record(maker, fixture) -> dict:
    from app.services.user_lifecycle import UserLifecycleService

    owner_id = fixture["users"]["owner"]
    async with maker() as holder, maker() as caller, maker() as observer:
        await holder.execute(
            select(User.id).where(User.id == owner_id).with_for_update()
        )
        await holder.execute(
            text("UPDATE public.users SET name='Waited current owner' WHERE id=:id"),
            {"id": owner_id},
        )
        pid = await caller.scalar(text("SELECT pg_backend_pid()"))
        waiting = asyncio.create_task(
            UserLifecycleService.lock_accounts(caller, [owner_id], nowait=False)
        )
        observed = False
        for _ in range(200):
            state = await observer.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                {"pid": pid},
            )
            await observer.rollback()
            if state == "Lock":
                observed = True
                break
            await asyncio.sleep(0.01)
        assert observed and not waiting.done()
        await holder.commit()
        accounts = await asyncio.wait_for(waiting, timeout=5)
        record = {
            "actually_waited": observed,
            "actor": _account_facts(accounts[owner_id]),
        }
        await caller.rollback()
        return record


async def test_account_lock_outputs_match_capture_and_execute_only_peer(
    test_engine,
):
    from app.services.identity_account_lock_data import IdentityAccountLockData
    from app.services.identity_contracts import IdentityActorStatus
    from app.services.user_lifecycle import UserLifecycleService

    maker, fixture = await _seed_legacy_world(test_engine)
    try:
        expected = _legacy_samples()
        records = {
            "access": await _legacy_access_records(maker, fixture),
            "contention": await _legacy_contention_records(maker, fixture),
        }
        async with maker() as caller:
            accounts = await UserLifecycleService.lock_accounts(
                caller,
                [
                    fixture["users"]["legacy"],
                    fixture["users"]["inactive"],
                    fixture["users"]["owner"],
                    fixture["users"]["owner"],
                    _identity("missing"),
                ],
            )
            records["batch"] = [_account_facts(actor) for actor in accounts.values()]
            await caller.rollback()
        records["wait"] = await _legacy_wait_record(maker, fixture)
        assert records == expected
        current_batch = [
            records["wait"]["actor"]
            if row["id"] == str(fixture["users"]["owner"])
            else row
            for row in expected["batch"]
        ]

        async with maker() as status_session:
            status_reader = IdentityAccountLockData(status_session)
            owner_status = await status_reader.read_actor_status(
                fixture["users"]["owner"]
            )
            assert owner_status == IdentityActorStatus(
                id=fixture["users"]["owner"], role="project_admin", is_active=True
            )
            inactive_status = await status_reader.read_actor_status(
                fixture["users"]["inactive"]
            )
            assert inactive_status == IdentityActorStatus(
                id=fixture["users"]["inactive"], role="employee", is_active=False
            )
            legacy_status = await status_reader.read_actor_status(
                fixture["users"]["legacy"]
            )
            assert legacy_status.role == "reviewer" and legacy_status.is_active is True
            assert await status_reader.read_actor_status(_identity("missing")) is None
            await status_session.rollback()

        container_id, database = _append_client_target(test_engine)
        account_ids = [
            fixture["users"]["legacy"],
            fixture["users"]["inactive"],
            fixture["users"]["owner"],
            fixture["users"]["owner"],
            _identity("missing"),
        ]
        for mode in ("share", "update", "key_share"):
            peer = await asyncio.to_thread(
                _node_account_lock_peer,
                container_id=container_id,
                database=database,
                account_ids=account_ids,
                mode=mode,
                nowait=True,
            )
            assert peer["current_user"] == peer["session_user"] == "append_client"
            assert peer["database"] == database
            assert peer["raw_user_privileges"] is False
            assert peer["request"]["mode"] == mode
            assert peer["accounts"] == current_batch

        for mode in ("share", "update", "key_share"):
            async with maker() as holder, maker() as contender:
                facts = await IdentityAccountLockData(holder).lock_accounts(
                    [fixture["users"]["owner"]], mode=mode, nowait=True
                )
                assert fixture["users"]["owner"] in facts
                statement = text(
                    "SELECT id FROM public.users WHERE id=:id FOR NO KEY UPDATE NOWAIT"
                )
                if mode == "key_share":
                    assert (
                        await contender.scalar(
                            statement, {"id": fixture["users"]["owner"]}
                        )
                        == fixture["users"]["owner"]
                    )
                else:
                    with pytest.raises(DBAPIError) as mode_busy:
                        await contender.scalar(
                            statement, {"id": fixture["users"]["owner"]}
                        )
                    assert getattr(mode_busy.value.orig, "sqlstate", None) == "55P03"
                await contender.rollback()
                await holder.rollback()

        async with maker() as holder, maker() as caller:
            await holder.execute(
                select(User.id)
                .where(User.id == fixture["users"]["owner"])
                .with_for_update()
            )
            with pytest.raises(DBAPIError) as busy:
                await IdentityAccountLockData(caller).lock_accounts(
                    [fixture["users"]["owner"]], mode="share", nowait=True
                )
            assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
            with pytest.raises(DBAPIError) as aborted:
                await caller.scalar(text("SELECT 1"))
            assert getattr(aborted.value.orig, "sqlstate", None) == "25P02"
            await caller.rollback()
            await holder.rollback()
    finally:
        await _cleanup_legacy_world(maker, fixture)


async def test_workspace_transfer_holds_identity_account_locks_through_commit(
    test_engine,
):
    from app.application.project_workspace import transfer_project_owner
    from app.services.identity_contracts import IdentityActorFacts

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id = uuid.uuid4()
    receiver_id = uuid.uuid4()
    project_id = uuid.uuid4()
    try:
        async with maker.begin() as seed:
            actor = await create_user(
                seed, "super_admin", f"write-fence-owner-{actor_id}@test.local", "Owner"
            )
            actor.id = actor_id
            await seed.flush()
            receiver = await create_user(
                seed,
                "project_admin",
                f"write-fence-receiver-{receiver_id}@test.local",
                "Receiver",
            )
            receiver.id = receiver_id
            await seed.flush()
            project = await create_project(seed, owner_id=actor_id)
            project.id = project_id
            await seed.flush()

        async with maker() as caller:
            await caller.begin()
            transferred = await transfer_project_owner(
                caller,
                actor=IdentityActorFacts(
                    id=actor.id,
                    name=actor.name,
                    email=actor.email,
                    role=actor.role,
                    is_active=actor.is_active,
                ),
                project_id=project_id,
                new_owner_id=receiver_id,
            )
            assert transferred.source == "owner_transfer"
            assert transferred.new_owner_id == receiver_id
            async with maker() as observer:
                for table, key in (
                    (User, actor_id),
                    (User, receiver_id),
                    (Project, project_id),
                ):
                    statement = (
                        select(table.id)
                        .where(table.id == key)
                        .with_for_update(nowait=True)
                    )
                    with pytest.raises(DBAPIError) as locked:
                        await observer.execute(statement)
                    assert getattr(locked.value.orig, "sqlstate", None) == "55P03"
                    await observer.rollback()
            await caller.commit()
        async with maker() as observer:
            project = await observer.get(Project, project_id)
            assert project.owner_id == receiver_id
            for table, key in (
                (User, actor_id),
                (User, receiver_id),
                (Project, project_id),
            ):
                await observer.execute(
                    select(table.id).where(table.id == key).with_for_update(nowait=True)
                )
                await observer.rollback()
    finally:
        async with maker.begin() as cleanup:
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(
                delete(User).where(User.id.in_([actor_id, receiver_id]))
            )


async def test_account_lock_reads_preserve_caller_no_autoflush_work(test_engine):
    from app.services.identity_account_lock_data import IdentityAccountLockData

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    owner_id = uuid.uuid4()
    pending_id = uuid.uuid4()
    async with maker.begin() as seed:
        owner = await create_user(
            seed,
            "project_admin",
            f"no-autoflush-owner-{owner_id}@test.local",
            "No autoflush owner",
        )
        owner.id = owner_id
        await seed.flush()
    try:
        async with maker() as caller:
            autoflushed = User(
                id=pending_id,
                email=f"autoflush-write-fence-{pending_id}@test.local",
                name="Autoflush unrelated work",
                password_hash="test-only",
                role="employee",
                is_active=True,
            )
            caller.add(autoflushed)
            facts = await IdentityAccountLockData(caller).lock_accounts(
                [owner_id], mode="share", nowait=True
            )
            assert owner_id in facts
            assert autoflushed not in caller.new
            await caller.rollback()

        async with maker() as caller:
            pending = User(
                id=uuid.uuid4(),
                email=f"no-autoflush-write-fence-{uuid.uuid4()}@test.local",
                name="Pending unrelated work",
                password_hash="test-only",
                role="employee",
                is_active=True,
            )
            caller.add(pending)
            with caller.no_autoflush:
                facts = await IdentityAccountLockData(caller).lock_accounts(
                    [owner_id], mode="share", nowait=True
                )
                assert owner_id in facts
                assert pending in caller.new
                async with maker() as observer:
                    assert await observer.get(User, pending_id) is None
            await caller.rollback()
    finally:
        async with maker.begin() as cleanup:
            await cleanup.execute(delete(User).where(User.id == owner_id))
