"""Freeze actual member lock consumers before replacing their private reads."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import event, inspect, text

from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.services.batch import BatchService
from app.services.project_write_guard import lock_actor_scope
from tests.factory import create_membership, create_user
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
_BASE = "025b801e64fe82e61ba2ded92663f4554e26e418"
_GOLDEN = Path(__file__).parent / "fixtures/workspace_member_locks_legacy.json"


def _compare(key, observed):
    if os.getenv("AAP_MEMBER_LOCKS_CAPTURE") == "1":
        root = Path(__file__).resolve().parents[3]
        assert (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            == _BASE
        )
        document = (
            json.loads(_GOLDEN.read_text())
            if _GOLDEN.exists()
            else {"source_commit": _BASE, "cases": {}}
        )
        assert key not in document["cases"], "never overwrite a captured legacy case"
        document["cases"][key] = observed
        _GOLDEN.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    else:
        assert observed == json.loads(_GOLDEN.read_text())["cases"][key]


def _state(member):
    if member is None:
        return None
    state = inspect(member)
    return {
        "values": {
            key: state.dict.get(key, "__unloaded__")
            for key in ("role", "version", "weekly_target")
        },
        "expired": state.expired,
        "unloaded": sorted(state.unloaded),
        "persistent": state.persistent,
        "modified": state.modified,
        "dirty_role": state.attrs.role.history.has_changes(),
    }


@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize(
    "cache", ["miss", "clean", "dirty", "partial", "expired", "expired_dirty"]
)
async def test_assignment_member_cache_matches_legacy(test_engine, cache, autoflush):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            cached = (
                None
                if cache == "miss"
                else await db.get(ProjectMember, world["membership_id"])
            )
            async with owner.begin() as change:
                await change.execute(
                    text(
                        "UPDATE public.project_members SET weekly_target=31,version=2 "
                        "WHERE id=:id"
                    ),
                    {"id": world["membership_id"]},
                )
            if cache == "dirty":
                cached.role = "viewer"
            elif cache == "partial":
                db.expire(cached, ["weekly_target"])
            elif cache in {"expired", "expired_dirty"}:
                db.expire(cached)
                if cache == "expired_dirty":
                    cached.role = "viewer"
            pending = Task(
                id=uuid.uuid4(),
                project_id=world["project_id"],
                display_id=f"T-MEMBER-{uuid.uuid4().hex[:12]}",
                file_name="pending.png",
                file_path=f"member/{uuid.uuid4().hex}.png",
                file_type="image",
                status="pending",
            )
            db.add(pending)
            trace = []
            private_reads = []
            owner_steps = []

            def capture(_conn, _cursor, statement, _parameters, _context, _many):
                lowered = statement.lower()
                if "select " in lowered and "from project_members" in lowered:
                    private_reads.append(statement)
                if "project_member_rows(" in lowered:
                    owner_steps.append(
                        "locked_members" if _parameters[-1] else "primer"
                    )
                elif "aap_identity_v1.lock_accounts_share" in lowered:
                    owner_steps.append("accounts")
                if "project_member_rows(" in lowered or (
                    "select " in lowered and "from project_members" in lowered
                ):
                    trace.append("member_read")
                elif "update project_members" in lowered:
                    trace.append("member_update")
                elif "insert into tasks" in lowered:
                    trace.append("task_insert")

            event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
            error = None
            try:
                with nullcontext() if autoflush else db.no_autoflush:
                    await BatchService(db).lock_and_validate_assignment_targets(
                        world["project_id"], [("annotator", world["member_id"])]
                    )
            except HTTPException as exc:
                error = [exc.status_code, exc.detail["reason"]]
            except Exception as exc:
                error = type(exc).__name__
            finally:
                event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
            member = db.sync_session.identity_map.get(
                ProjectMember.__mapper__.identity_key_from_primary_key(
                    (world["membership_id"],)
                )
            )
            # Only cached rows are strongly retained after the consumer returns.
            observed = {
                "error": error,
                "cached": _state(cached),
                "same_identity": cached is not None and member is cached,
                "pending_task": pending in db.new,
                "trace": trace,
            }
            _compare(f"assignment:{cache}:autoflush={autoflush}", observed)
            if os.getenv("AAP_MEMBER_LOCKS_CAPTURE") != "1":
                assert not private_reads
                assert owner_steps == (
                    ["primer", "accounts", "locked_members"]
                    if cache == "expired_dirty" and autoflush
                    else ["accounts", "locked_members"]
                )
            await db.rollback()


@pytest.mark.parametrize("nowait", [False, True])
@pytest.mark.parametrize(
    "kind", ["exists", "missing_member", "bad_date", "null_actor", "null_project"]
)
async def test_scalar_member_guard_keeps_legacy_semantics(test_engine, nowait, kind):
    async with _world(test_engine) as (owner, _api, world, _url):
        if kind in {"missing_member", "bad_date"}:
            async with owner.begin() as change:
                await change.execute(
                    text(
                        "DELETE FROM public.project_members WHERE id=:id"
                        if kind == "missing_member"
                        else "UPDATE public.project_members SET assigned_at="
                        "TIMESTAMPTZ '12000-01-01' WHERE id=:id"
                    ),
                    {"id": world["membership_id"]},
                )
        async with owner() as db:
            actor_id = None if kind == "null_actor" else world["member_id"]
            project_id = None if kind == "null_project" else world["project_id"]
            result = await lock_actor_scope(db, actor_id, project_id, nowait=nowait)
            assert result is None
            _compare(f"reference:{kind}:nowait={nowait}", {"result": None})


async def _second_member(owner, world):
    async with owner.begin() as db:
        user = await create_user(
            db, "employee", f"member-lock-{uuid.uuid4().hex}@example.test", "Second"
        )
        world["actor_ids"].append(user.id)
        member = await create_membership(
            db,
            project_id=world["project_id"],
            user_id=user.id,
            role="annotator",
            assigned_by=world["owner_id"],
        )
        return user.id, member.id


@pytest.mark.parametrize("nowait", [False, True])
async def test_restricted_scalar_guard_keeps_all_three_fences(test_engine, nowait):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as seed:
            await seed.execute(
                text(
                    "UPDATE public.project_members SET assigned_at="
                    "TIMESTAMPTZ '12000-01-01' WHERE id=:id"
                ),
                {"id": world["membership_id"]},
            )
        async with api() as db:
            await _assert_private_denied(db)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            await lock_actor_scope(
                db, world["member_id"], world["project_id"], nowait=nowait
            )
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            for table, identity in (
                ("users", world["member_id"]),
                ("projects", world["project_id"]),
                ("project_members", world["membership_id"]),
            ):
                await _assert_nowait_conflict(
                    test_engine,
                    f"SELECT id FROM public.{table} WHERE id=$1 FOR UPDATE NOWAIT",
                    identity,
                )


async def test_restricted_batch_assignment_locks_whole_sorted_member_set(test_engine):
    from app.services.workspace_member_locks import lock_project_members

    async with _world(test_engine) as (owner, api, world, _url):
        second_id, second_member_id = await _second_member(owner, world)
        async with api() as db:
            await _assert_private_denied(db)
            before = (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one()
            await BatchService(db).lock_and_validate_assignment_targets(
                world["project_id"],
                [("annotator", second_id), ("annotator", world["member_id"])],
            )
            assert (
                await db.execute(text("SELECT pg_backend_pid(),txid_current()"))
            ).one() == before
            for identity in (world["membership_id"], second_member_id):
                await _assert_nowait_conflict(
                    test_engine,
                    "SELECT id FROM public.project_members WHERE id=$1 FOR UPDATE NOWAIT",
                    identity,
                )
            rows = await lock_project_members(
                db, world["project_id"], [second_id, world["member_id"], second_id]
            )
            assert [row.user_id for row in rows] == sorted(
                [second_id, world["member_id"]]
            )
            assert all(not inspect(row).unloaded for row in rows)
            assert await lock_project_members(db, world["project_id"], []) == []
            await db.rollback()


async def test_restricted_assignment_refreshes_expired_dirty_cache_without_autoflush(
    test_engine,
):
    from app.services.workspace_member_locks import lock_project_members

    async with _world(test_engine) as (_owner, api, world, _url):
        async with api() as db:
            await _assert_private_denied(db)
            cached = (
                await lock_project_members(
                    db, world["project_id"], [world["member_id"]]
                )
            )[0]
            db.expire(cached)
            cached.role = "viewer"
            with db.no_autoflush:
                await BatchService(db).lock_and_validate_assignment_targets(
                    world["project_id"], [("annotator", world["member_id"])]
                )
            assert cached.role == "annotator" and not inspect(cached).modified
            assert not inspect(cached).unloaded


async def test_scalar_nowait_contention_remains_membership_busy(test_engine):
    async with _world(test_engine) as (owner, api, world, _url):
        async with owner() as blocker, api() as db:
            await blocker.execute(
                text("SELECT id FROM public.project_members WHERE id=:id FOR UPDATE"),
                {"id": world["membership_id"]},
            )
            with pytest.raises(HTTPException) as caught:
                await lock_actor_scope(db, world["member_id"], world["project_id"])
            assert (caught.value.status_code, caught.value.detail) == (
                409,
                {"reason": "membership_busy"},
            )
            await db.rollback()
            assert await db.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize("wrong_type", [False, True])
async def test_bad_scalar_reply_rolls_back_prior_caller_write(
    test_engine, monkeypatch, wrong_type
):
    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            original = await db.scalar(
                text("SELECT name FROM public.projects WHERE id=:id"),
                {"id": world["project_id"]},
            )
        with pytest.raises(HTTPException) as caught:
            async with owner.begin() as db:
                scalar = db.scalar

                async def corrupt(statement, *args, **kwargs):
                    result = await scalar(statement, *args, **kwargs)
                    if "aap_workspace_v1.lock_project_member_reference" in str(
                        statement
                    ):
                        return str(result) if wrong_type else uuid.uuid4()
                    return result

                monkeypatch.setattr(db, "scalar", corrupt)
                await db.execute(
                    text(
                        "UPDATE public.projects SET name='pending scalar probe' WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
                await lock_actor_scope(db, world["member_id"], world["project_id"])
        assert caught.value.status_code == 503
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM public.projects WHERE id=:id"),
                    {"id": world["project_id"]},
                )
                == original
            )


async def test_waiting_bulk_set_refreshes_after_contender_commits(test_engine):
    from app.services.workspace_member_locks import lock_project_members

    async with _world(test_engine) as (owner, api, world, _url):
        second_id, second_member_id = await _second_member(owner, world)
        targets = sorted(
            [
                (world["member_id"], world["membership_id"]),
                (second_id, second_member_id),
            ]
        )
        async with owner() as blocker, api() as db:
            await blocker.execute(
                text("SELECT id FROM public.project_members WHERE id=:id FOR UPDATE"),
                {"id": targets[1][1]},
            )
            pid = await db.scalar(text("SELECT pg_backend_pid()"))
            pending = asyncio.create_task(
                lock_project_members(
                    db, world["project_id"], [item[0] for item in targets]
                )
            )
            try:
                for _ in range(100):
                    async with owner() as observer:
                        waiting = await observer.scalar(
                            text("SELECT cardinality(pg_blocking_pids(:pid))>0"),
                            {"pid": pid},
                        )
                    if waiting:
                        break
                    await asyncio.sleep(0.02)
                assert waiting and not pending.done()
                await _assert_nowait_conflict(
                    test_engine,
                    "SELECT id FROM public.project_members WHERE id=$1 FOR UPDATE NOWAIT",
                    targets[0][1],
                )
                await blocker.execute(
                    text(
                        "UPDATE public.project_members SET role='reviewer' WHERE id=:id"
                    ),
                    {"id": targets[1][1]},
                )
                await blocker.commit()
                rows = await asyncio.wait_for(pending, timeout=5)
                assert [row.user_id for row in rows] == [item[0] for item in targets]
                assert [row.role for row in rows] == ["annotator", "reviewer"]
            finally:
                await blocker.rollback()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("corruption", ["project", "user", "duplicate"])
async def test_bad_bulk_scope_rolls_back_prior_caller_write(
    test_engine, monkeypatch, corruption
):
    from app.services.workspace_member_locks import lock_project_members

    async with _world(test_engine) as (owner, _api, world, _url):
        async with owner() as db:
            original = await db.scalar(
                text("SELECT name FROM public.projects WHERE id=:id"),
                {"id": world["project_id"]},
            )
        with pytest.raises(HTTPException) as caught:
            async with owner.begin() as db:
                scalar = db.scalar

                async def corrupt(statement, *args, **kwargs):
                    value = await scalar(statement, *args, **kwargs)
                    if "aap_workspace_v1.project_member_rows" in str(statement):
                        rows = json.loads(value)
                        if corruption == "duplicate":
                            rows.append(rows[0])
                        else:
                            rows[0][
                                "project_id" if corruption == "project" else "user_id"
                            ] = str(uuid.uuid4())
                        return json.dumps(rows)
                    return value

                monkeypatch.setattr(db, "scalar", corrupt)
                await db.execute(
                    text(
                        "UPDATE public.projects SET name='pending member probe' WHERE id=:id"
                    ),
                    {"id": world["project_id"]},
                )
                await lock_project_members(
                    db, world["project_id"], [world["member_id"]]
                )
        assert caught.value.status_code == 503
        async with owner() as db:
            assert (
                await db.scalar(
                    text("SELECT name FROM public.projects WHERE id=:id"),
                    {"id": world["project_id"]},
                )
                == original
            )


async def test_member_native_functions_have_exact_owner_and_grants(test_engine):
    async with _world(test_engine) as (owner, _api, _world_data, _url):
        async with owner() as db:
            artifact = json.loads(
                (
                    Path(__file__).resolve().parents[1]
                    / "alembic/sql/0292_workspace_member_locks.schema.json"
                ).read_text()
            )
            signatures = artifact["x-postgresql-functions"]
            assert len(signatures) == 2
            assert set(signatures) == {
                item["signature"]
                for item in artifact["x-native-capabilities"]["functions"]
            }
            for signature in signatures:
                row = (
                    await db.execute(
                        text(
                            "SELECT pg_get_userbyid(proowner),prosecdef FROM pg_proc "
                            "WHERE oid=CAST(:signature AS regprocedure)"
                        ),
                        {"signature": signature},
                    )
                ).one()
                assert row == ("workspace_code", True)
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
                        "SELECT EXISTS (SELECT 1 FROM pg_proc p CROSS JOIN LATERAL "
                        "aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a "
                        "WHERE p.oid=CAST(:signature AS regprocedure) AND a.grantee=0 "
                        "AND a.privilege_type='EXECUTE')"
                    ),
                    {"signature": signature},
                )


@pytest.mark.parametrize("timestamp", ["infinity", "-infinity", "12000-01-01"])
async def test_bulk_member_temporals_match_installed_orm(test_engine, timestamp):
    from app.services.workspace_member_locks import lock_project_members

    async with _world(test_engine) as (owner, api, world, _url):
        async with owner.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.project_members SET assigned_at=CAST(CAST(:value AS text) AS timestamptz),"
                    "updated_at=CAST(CAST(:value AS text) AS timestamptz) WHERE id=:id"
                ),
                {"id": world["membership_id"], "value": timestamp},
            )
        results = []
        for maker, legacy in ((owner, True), (api, False)):
            async with maker() as db:
                try:
                    row = (
                        await db.get(ProjectMember, world["membership_id"])
                        if legacy
                        else (
                            await lock_project_members(
                                db, world["project_id"], [world["member_id"]]
                            )
                        )[0]
                    )
                    results.append(
                        (row.assigned_at.isoformat(), row.updated_at.isoformat())
                    )
                except Exception as exc:
                    results.append(type(exc).__name__)
        assert results[0] == results[1]


async def test_node_member_locks_need_no_python_proxy(test_engine):
    async with _world(test_engine) as (owner, _api, world, url):
        second_id, second_member_id = await _second_member(owner, world)
        process = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).parent / "fixtures/workspace_member_locks_peer.mjs"),
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
                            "contender_url": test_engine.url.set(
                                drivername="postgresql"
                            ).render_as_string(hide_password=False),
                            "project_id": str(world["project_id"]),
                            "users": [str(second_id), str(world["member_id"])],
                            "members": [
                                str(second_member_id),
                                str(world["membership_id"]),
                            ],
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
