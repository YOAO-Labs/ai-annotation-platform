"""Controlled PostgreSQL overlaps for the Inference registry/pool lock graph."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_inference import extract_capabilities
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.inference_health_data import InferenceHealthData
from app.services.ml_backend import MLBackendService
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]
_SETUP = {"name": "lock test", "supported_prompts": ["bbox"]}


class PoolGateSession(AsyncSession):
    def __init__(self, *args, pool_locked, release_pool, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool_locked = pool_locked
        self.release_pool = release_pool
        self.gated = False

    async def _gate(self, statement):
        source = str(statement).lower()
        if not self.gated and (
            ("from ml_backend_service_pools" in source and "for update" in source)
            or "aap_inference_health_data_v1.pool_row(" in source
        ):
            self.gated = True
            self.pool_locked.set()
            await self.release_pool.wait()

    async def scalar(self, statement, *args, **kwargs):
        result = await super().scalar(statement, *args, **kwargs)
        await self._gate(statement)
        return result

    async def execute(self, statement, *args, **kwargs):
        result = await super().execute(statement, *args, **kwargs)
        await self._gate(statement)
        return result


async def _seed(test_engine, *, state="active"):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        backend = await MLBackendService(db).create_registry(
            "Concurrent topology " + uuid.uuid4().hex,
            "http://controlled-health.invalid",
        )
        backend.state = "connected"
        backend.health_meta = {"capabilities": extract_capabilities(_SETUP)}
        await db.flush()
        await MLBackendService(db)._reconcile_pool_capability(
            backend.id, backend.health_meta["capabilities"]
        )
        member = await db.scalar(
            select(MLBackendPoolMember).where(
                MLBackendPoolMember.registry_id == backend.id
            )
        )
        member.traffic_state = state
        return backend.id, member.pool_id


async def _cleanup(test_engine, registry_id, pool_id):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        await db.execute(
            delete(MLBackendPoolMember).where(
                MLBackendPoolMember.registry_id == registry_id
            )
        )
        await db.execute(
            delete(MLBackendServicePool).where(MLBackendServicePool.id == pool_id)
        )
        await db.execute(
            delete(MLBackendRegistry).where(MLBackendRegistry.id == registry_id)
        )


async def _timeouts(db):
    await db.execute(text("SET LOCAL deadlock_timeout='100ms'"))
    await db.execute(text("SET LOCAL statement_timeout='5000ms'"))
    return await db.scalar(text("SELECT pg_backend_pid()"))


async def _blocked_by(test_engine, waiter, holder):
    deadline = time.monotonic() + 4
    async with test_engine.connect() as db:
        while True:
            pids = await db.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter}
            )
            if holder in pids:
                return
            assert time.monotonic() < deadline, (
                "Expected real PostgreSQL lock wait did not occur"
            )
            await asyncio.sleep(0.01)


def _fake_health(monkeypatch):
    async def health(self):
        return True, {"compute": {"configured_device": "cpu"}}

    async def setup(self):
        return _SETUP

    monkeypatch.setattr("app.services.ml_client.MLBackendClient.health_meta", health)
    monkeypatch.setattr("app.services.ml_client.MLBackendClient.setup", setup)


@pytest.mark.parametrize("operation", ["remove", "upsert"])
async def test_health_and_pool_writer_complete_without_deadlock(
    test_engine, monkeypatch, operation
):
    await assert_owned_test_database(test_engine)
    _fake_health(monkeypatch)
    registry_id, pool_id = await _seed(test_engine)
    pool_locked = asyncio.Event()
    release = asyncio.Event()
    health_started = asyncio.Event()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    writer = PoolGateSession(
        bind=test_engine,
        expire_on_commit=False,
        pool_locked=pool_locked,
        release_pool=release,
    )
    health = maker()

    class ObservedData(InferenceHealthData):
        async def lock_current_backend(self, registry):
            health_started.set()
            return await super().lock_current_backend(registry)

    monkeypatch.setattr(
        "app.services.inference_health_data.InferenceHealthData", ObservedData
    )
    tasks = []
    try:
        writer_pid = await _timeouts(writer)
        health_pid = await _timeouts(health)

        async def mutate():
            if operation == "remove":
                result = await MLBackendService(writer).remove_pool_member(
                    pool_id, registry_id
                )
            else:
                result = await MLBackendService(writer).add_pool_member(
                    pool_id, registry_id, weight=2
                )
            await writer.commit()
            return result

        async def probe():
            result = await MLBackendService(health).check_health(registry_id)
            await health.commit()
            return result

        tasks.append(asyncio.create_task(mutate()))
        await asyncio.wait_for(pool_locked.wait(), 3)
        tasks.append(asyncio.create_task(probe()))
        await asyncio.wait_for(health_started.wait(), 3)
        await _blocked_by(test_engine, health_pid, writer_pid)
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 5
        )
        assert not [
            type(r).__name__ for r in results if isinstance(r, BaseException)
        ], results
        assert results[1] is True
        async with maker() as observer:
            member = await observer.scalar(
                select(MLBackendPoolMember).where(
                    MLBackendPoolMember.registry_id == registry_id
                )
            )
            assert member is None if operation == "remove" else member.weight == 2
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await writer.rollback()
        await health.rollback()
        await writer.close()
        await health.close()
        await _cleanup(test_engine, registry_id, pool_id)


@pytest.mark.parametrize("operation", ["drain", "resume"])
async def test_member_state_writer_and_upsert_use_one_pool_member_order(
    test_engine, operation
):
    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(
        test_engine, state="draining" if operation == "resume" else "active"
    )
    pool_locked = asyncio.Event()
    release = asyncio.Event()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    writer = PoolGateSession(
        bind=test_engine,
        expire_on_commit=False,
        pool_locked=pool_locked,
        release_pool=release,
    )
    state_writer = maker()
    tasks = []
    try:
        first_pid = await _timeouts(writer)
        second_pid = await _timeouts(state_writer)

        async def upsert():
            await MLBackendService(writer).add_pool_member(
                pool_id, registry_id, weight=2
            )
            await writer.commit()

        async def change_state():
            service = MLBackendService(state_writer)
            if operation == "drain":
                await service.drain_pool_member(pool_id, registry_id)
            else:
                await service.resume_pool_member(pool_id, registry_id)
            await state_writer.commit()

        tasks.append(asyncio.create_task(upsert()))
        await asyncio.wait_for(pool_locked.wait(), 3)
        tasks.append(asyncio.create_task(change_state()))
        await _blocked_by(test_engine, second_pid, first_pid)
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 5
        )
        assert not [
            type(r).__name__ for r in results if isinstance(r, BaseException)
        ], results
        async with maker() as observer:
            member = await observer.scalar(
                select(MLBackendPoolMember).where(
                    MLBackendPoolMember.registry_id == registry_id
                )
            )
            assert member.weight == 2 and member.traffic_state == (
                "draining" if operation == "drain" else "active"
            )
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await writer.rollback()
        await state_writer.rollback()
        await writer.close()
        await state_writer.close()
        await _cleanup(test_engine, registry_id, pool_id)


class _ZeroLedger:
    async def healthcheck(self):
        pass

    async def member_inflight(self, pool_id, registry_id):
        return 0

    async def aclose(self):
        pass


@pytest.mark.parametrize("change", ["resume", "recreate", "move", "generation"])
@pytest.mark.parametrize("operation", ["remove", "delete"])
async def test_destructive_write_rejects_changed_topology_after_quiescence(
    test_engine, monkeypatch, change, operation
):
    from app.config import settings
    from app.services.ml_routing.contracts import MLBackendQuiescenceError
    from app.services.ml_routing.safety import require_registry_quiescent
    from app.application.ml_registry_deletion import delete_registry

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, state="draining")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    extra_pools = []
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")

    class Ledger(_ZeroLedger):
        changed = False

        async def member_inflight(self, *args):
            if not self.changed:
                self.changed = True
                async with maker.begin() as other:
                    service = MLBackendService(other)
                    if change == "resume":
                        await service.resume_pool_member(pool_id, registry_id)
                    elif change == "generation":
                        await service.update_pool(pool_id, name="Changed generation")
                    else:
                        await service.remove_pool_member(pool_id, registry_id)
                        if change == "move":
                            pool = await service.create_pool("Moved member")
                            extra_pools.append(pool.id)
                            target = pool.id
                        else:
                            target = pool_id
                        await service.add_pool_member(target, registry_id)
                        await service.drain_pool_member(target, registry_id)
            return 0

    monkeypatch.setattr(
        "app.services.ml_routing.safety.make_ledger_from_settings", Ledger
    )
    try:
        async with maker() as caller:
            with pytest.raises(MLBackendQuiescenceError) as caught:
                if operation == "remove":
                    snapshot = await require_registry_quiescent(caller, registry_id)
                    await MLBackendService(caller).remove_pool_member(
                        pool_id, registry_id, expected_topology=snapshot
                    )
                else:
                    await delete_registry(caller, registry_id)
            assert caught.value.error_code == "ml_backend_topology_changed"
            await caller.rollback()
        async with maker() as observer:
            assert await observer.get(MLBackendRegistry, registry_id) is not None
            member = await observer.scalar(
                select(MLBackendPoolMember).where(
                    MLBackendPoolMember.registry_id == registry_id
                )
            )
            assert member is not None
            assert member.traffic_state == (
                "active" if change == "resume" else "draining"
            )
    finally:
        async with maker.begin() as cleanup:
            await cleanup.execute(
                delete(MLBackendPoolMember).where(
                    MLBackendPoolMember.registry_id == registry_id
                )
            )
            await cleanup.execute(
                delete(MLBackendServicePool).where(
                    MLBackendServicePool.id.in_([pool_id, *extra_pools])
                )
            )
            await cleanup.execute(
                delete(MLBackendRegistry).where(MLBackendRegistry.id == registry_id)
            )


async def test_delete_and_workspace_binding_writer_fail_fast_instead_of_deadlocking(
    test_engine, monkeypatch
):
    from fastapi import HTTPException
    from tests.factory import create_user, create_project
    from app.config import settings
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.application.ml_registry_deletion import delete_registry

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, state="draining")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as seed:
        user = await create_user(
            seed,
            "project_admin",
            f"binding-cycle-{uuid.uuid4().hex}@example.test",
            "Binding conflict owner",
        )
        project = await create_project(seed, owner_id=user.id)
        project.ml_backend_pool_id = pool_id
        actor_id, project_id = user.id, project.id
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    monkeypatch.setattr(
        "app.services.ml_routing.safety.make_ledger_from_settings", _ZeroLedger
    )
    before_binding = asyncio.Event()
    continue_clear = asyncio.Event()

    class DeletingSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if "clear_primary_backend_bindings" in str(statement):
                before_binding.set()
                await continue_clear.wait()
            return await super().execute(statement, *args, **kwargs)

    deleter = DeletingSession(bind=test_engine, expire_on_commit=False)
    workspace = maker()
    tasks = []
    try:
        delete_pid = await _timeouts(deleter)
        workspace_pid = await _timeouts(workspace)
        await workspace.execute(
            select(Project).where(Project.id == project_id).with_for_update()
        )

        async def remove():
            try:
                await delete_registry(deleter, registry_id)
            except HTTPException as error:
                assert error.status_code == 409 and error.detail == {
                    "reason": "project_busy"
                }
                await deleter.rollback()
                return "conflict"
            raise AssertionError("Contended binding must abort the complete deletion")

        async def bind():
            await workspace.execute(
                select(MLBackendServicePool)
                .where(MLBackendServicePool.id == pool_id)
                .with_for_update()
            )
            await workspace.commit()
            return "workspace_completed"

        tasks.append(asyncio.create_task(remove()))
        await asyncio.wait_for(before_binding.wait(), 3)
        tasks.append(asyncio.create_task(bind()))
        await _blocked_by(test_engine, workspace_pid, delete_pid)
        continue_clear.set()
        assert await asyncio.wait_for(asyncio.gather(*tasks), 4) == [
            "conflict",
            "workspace_completed",
        ]
        async with maker() as observer:
            assert await observer.get(MLBackendRegistry, registry_id) is not None
            assert (
                await observer.scalar(
                    select(MLBackendPoolMember.id).where(
                        MLBackendPoolMember.registry_id == registry_id
                    )
                )
                is not None
            )
            assert (
                await observer.get(Project, project_id)
            ).ml_backend_pool_id == pool_id
    finally:
        continue_clear.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await deleter.rollback()
        await workspace.rollback()
        await deleter.close()
        await workspace.close()
        async with maker.begin() as cleanup:
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id == actor_id))
        await _cleanup(test_engine, registry_id, pool_id)


async def test_accept_drift_does_not_lock_other_registry_behind_pool(
    test_engine, monkeypatch
):
    from aap_inference.pool_capability import capability_fingerprint

    await assert_owned_test_database(test_engine)
    _fake_health(monkeypatch)
    registry_id, pool_id = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as seed:
        other = await MLBackendService(seed).create_registry(
            "Other concurrent member", "http://other-health.invalid"
        )
        other.state = "connected"
        other.health_meta = {"capabilities": extract_capabilities(_SETUP)}
        await seed.flush()
        other_pool = await MLBackendService(seed).pool_id_for_registry(other.id)
        await MLBackendService(seed).remove_pool_member(other_pool, other.id)
        await MLBackendService(seed).add_pool_member(pool_id, other.id)
        target = await seed.scalar(
            select(MLBackendPoolMember).where(
                MLBackendPoolMember.registry_id == registry_id
            )
        )
        target.traffic_state = "disabled"
        pool = await seed.get(MLBackendServicePool, pool_id)
        pool.enabled = False
        expected = capability_fingerprint(pool.capability_snapshot)
        other_id = other.id
    pool_locked = asyncio.Event()
    release = asyncio.Event()
    health_started = asyncio.Event()
    acceptor = PoolGateSession(
        bind=test_engine,
        expire_on_commit=False,
        pool_locked=pool_locked,
        release_pool=release,
    )
    health = maker()
    tasks = []

    class ObservedData(InferenceHealthData):
        async def lock_current_backend(self, registry):
            health_started.set()
            return await super().lock_current_backend(registry)

    monkeypatch.setattr(
        "app.services.inference_health_data.InferenceHealthData", ObservedData
    )
    try:
        accept_pid = await _timeouts(acceptor)
        health_pid = await _timeouts(health)

        async def accept():
            result = await MLBackendService(acceptor).accept_capability_drift(
                pool_id,
                registry_id,
                expected_candidate_fingerprint=expected,
                enable_pool=True,
            )
            await acceptor.commit()
            return result is not None

        async def probe():
            result = await MLBackendService(health).check_health(other_id)
            await health.commit()
            return result

        tasks.append(asyncio.create_task(accept()))
        await asyncio.wait_for(pool_locked.wait(), 3)
        tasks.append(asyncio.create_task(probe()))
        await asyncio.wait_for(health_started.wait(), 3)
        await _blocked_by(test_engine, health_pid, accept_pid)
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 5
        )
        assert results == [True, True], results
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await acceptor.rollback()
        await health.rollback()
        await acceptor.close()
        await health.close()
        async with maker.begin() as cleanup:
            await cleanup.execute(
                delete(MLBackendPoolMember).where(
                    MLBackendPoolMember.registry_id.in_([registry_id, other_id])
                )
            )
            await cleanup.execute(
                delete(MLBackendServicePool).where(
                    MLBackendServicePool.id.in_([pool_id, other_pool])
                )
            )
            await cleanup.execute(
                delete(MLBackendRegistry).where(
                    MLBackendRegistry.id.in_([registry_id, other_id])
                )
            )


async def test_registry_delete_serializes_before_health_member_locks(
    test_engine, monkeypatch
):
    from sqlalchemy import event

    await assert_owned_test_database(test_engine)
    _fake_health(monkeypatch)
    registry_id, pool_id = await _seed(test_engine)
    member_deleted = asyncio.Event()
    release = asyncio.Event()
    health_started = asyncio.Event()
    deletion_seen = False

    class DeletingSession(AsyncSession):
        gated = False

        async def flush(self, *args, **kwargs):
            await super().flush(*args, **kwargs)
            if deletion_seen and not self.gated:
                self.gated = True
                member_deleted.set()
                await release.wait()

    writer = DeletingSession(bind=test_engine, expire_on_commit=False)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    health = maker()
    tasks = []

    class ObservedData(InferenceHealthData):
        async def lock_current_backend(self, registry):
            health_started.set()
            return await super().lock_current_backend(registry)

    monkeypatch.setattr(
        "app.services.inference_health_data.InferenceHealthData", ObservedData
    )
    connection = await writer.connection()

    def observe(conn, cursor, statement, parameters, context, many):
        nonlocal deletion_seen
        if statement.lower().startswith("delete from ml_backend_pool_members"):
            deletion_seen = True

    event.listen(connection.sync_connection, "after_cursor_execute", observe)
    try:
        writer_pid = await _timeouts(writer)
        health_pid = await _timeouts(health)

        async def remove():
            result = await MLBackendService(writer).delete(registry_id)
            await writer.commit()
            return result

        async def probe():
            result = await MLBackendService(health).check_health(registry_id)
            await health.commit()
            return result

        tasks.append(asyncio.create_task(remove()))
        await asyncio.wait_for(member_deleted.wait(), 3)
        tasks.append(asyncio.create_task(probe()))
        await asyncio.wait_for(health_started.wait(), 3)
        await _blocked_by(test_engine, health_pid, writer_pid)
        release.set()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 5
        )
        assert results == [True, False], results
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(connection.sync_connection, "after_cursor_execute", observe)
        await writer.rollback()
        await health.rollback()
        await writer.close()
        await health.close()
        await _cleanup(test_engine, registry_id, pool_id)


@pytest.mark.parametrize("change", ["delete", "move", "delete_pool"])
async def test_workspace_enablement_rejects_membership_changed_while_waiting(
    test_engine, change
):
    from sqlalchemy.exc import DBAPIError
    from tests.factory import create_user, create_project
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.db.models.ml_backend_registry import ProjectMLBackendPool
    from app.services.workspace_configuration_data import _diagnostic
    from aap_workspace.configuration_postgres import configuration_busy_error

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, state="draining")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as seed:
        user = await create_user(
            seed,
            "project_admin",
            f"enable-race-{uuid.uuid4().hex}@example.test",
            "Owner",
        )
        project = await create_project(seed, owner_id=user.id)
        actor_id, project_id = user.id, project.id
        pool = await seed.get(MLBackendServicePool, pool_id)
        pool.legacy_instance_id = None
        pool.enabled = False
        destination = await MLBackendService(seed).create_pool("Destination")
        destination_id = destination.id
    writer, workspace = maker(), maker()
    tasks = []
    try:
        writer_pid = await _timeouts(writer)
        workspace_pid = await _timeouts(workspace)
        await writer.execute(
            select(MLBackendServicePool)
            .where(MLBackendServicePool.id == pool_id)
            .with_for_update()
        )
        await workspace.execute(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        await workspace.execute(text("SET LOCAL ROLE workspace_code"))

        async def enable():
            try:
                await workspace.scalar(
                    text(
                        "SELECT aap_inference_v1.enable_workspace_project_backend(:project, :registry)"
                    ),
                    {"project": project_id, "registry": registry_id},
                )
            except DBAPIError as error:
                assert _diagnostic(error)[0] == "40001"
                mapped = configuration_busy_error(*_diagnostic(error))
                assert mapped.status_code == 409 and mapped.detail == {
                    "reason": "project_busy"
                }
                await workspace.rollback()
                return "conflict"
            raise AssertionError("A stale registry selection must not enable the pool")

        tasks.append(asyncio.create_task(enable()))
        await _blocked_by(test_engine, workspace_pid, writer_pid)
        service = MLBackendService(writer)
        if change == "delete":
            assert await service.delete(registry_id)
        else:
            assert await service.remove_pool_member(pool_id, registry_id)
            if change == "delete_pool":
                assert await service.delete_pool(pool_id)
            else:
                await service.add_pool_member(destination_id, registry_id)
        await writer.commit()
        assert await asyncio.wait_for(tasks[0], 4) == "conflict"
        async with maker() as observer:
            remaining_pool = await observer.get(MLBackendServicePool, pool_id)
            if change == "delete_pool":
                assert remaining_pool is None
            else:
                assert remaining_pool.enabled is False
            assert (await observer.get(Project, project_id)).ml_backend_pool_id is None
            assert (
                await observer.scalar(
                    select(ProjectMLBackendPool.id).where(
                        ProjectMLBackendPool.project_id == project_id
                    )
                )
                is None
            )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await writer.rollback()
        await workspace.rollback()
        await writer.close()
        await workspace.close()
        async with maker.begin() as cleanup:
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id == actor_id))
            await cleanup.execute(
                delete(MLBackendPoolMember).where(
                    MLBackendPoolMember.registry_id == registry_id
                )
            )
            await cleanup.execute(
                delete(MLBackendServicePool).where(
                    MLBackendServicePool.id == destination_id
                )
            )
        await _cleanup(test_engine, registry_id, pool_id)


@pytest.mark.parametrize("operation", ["registry_enable", "pool_enable"])
async def test_project_enablement_locks_project_before_pool(test_engine, operation):
    from tests.factory import create_user, create_project
    from app.db.models.project import Project
    from app.db.models.user import User

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as seed:
        user = await create_user(
            seed,
            "project_admin",
            f"project-order-{uuid.uuid4().hex}@example.test",
            "Owner",
        )
        project = await create_project(seed, owner_id=user.id)
        actor_id, project_id = user.id, project.id
        (await seed.get(MLBackendServicePool, pool_id)).enabled = True
    workspace, writer = maker(), maker()
    tasks = []
    try:
        workspace_pid = await _timeouts(workspace)
        writer_pid = await _timeouts(writer)
        await workspace.execute(
            select(Project).where(Project.id == project_id).with_for_update()
        )

        async def enable():
            service = MLBackendService(writer)
            if operation == "registry_enable":
                association = await service.set_enabled(project_id, registry_id, True)
            else:
                association = await service.set_pool_enabled(project_id, pool_id, True)
            await writer.commit()
            return association.enabled

        tasks.append(asyncio.create_task(enable()))
        await _blocked_by(test_engine, writer_pid, workspace_pid)
        # The existing Workspace operation can take Pool while the writer waits on Project.
        reply = await workspace.scalar(
            text(
                "SELECT aap_inference_v1.enable_workspace_project_backend(:project, :registry)"
            ),
            {"project": project_id, "registry": registry_id},
        )
        assert reply["pool_id"] == str(pool_id)
        await workspace.commit()
        assert await asyncio.wait_for(tasks[0], 4) is True
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await writer.rollback()
        await workspace.rollback()
        await writer.close()
        await workspace.close()
        async with maker.begin() as cleanup:
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id == actor_id))
        await _cleanup(test_engine, registry_id, pool_id)


@pytest.mark.parametrize("operation", ["registry_enable", "pool_enable"])
@pytest.mark.parametrize("remove_pool", [False, True])
async def test_pool_enablement_revalidates_disabled_pool_after_wait(
    test_engine, operation, remove_pool
):
    from fastapi import HTTPException
    from tests.factory import create_user, create_project
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.db.models.ml_backend_registry import ProjectMLBackendPool

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as seed:
        user = await create_user(
            seed,
            "project_admin",
            f"pool-recheck-{uuid.uuid4().hex}@example.test",
            "Owner",
        )
        project = await create_project(seed, owner_id=user.id)
        actor_id, project_id = user.id, project.id
        (await seed.get(MLBackendServicePool, pool_id)).enabled = True
    deleter, writer = maker(), maker()
    tasks = []
    try:
        delete_pid = await _timeouts(deleter)
        writer_pid = await _timeouts(writer)
        # Prime the identity map as the public route does before calling its service.
        cached = await writer.get(MLBackendServicePool, pool_id)
        assert cached.enabled is True
        await deleter.execute(
            select(MLBackendServicePool)
            .where(MLBackendServicePool.id == pool_id)
            .with_for_update()
        )

        async def enable():
            with pytest.raises((HTTPException, ValueError)) as failure:
                if operation == "registry_enable":
                    await MLBackendService(writer).set_enabled(
                        project_id, registry_id, True
                    )
                else:
                    await MLBackendService(writer).set_pool_enabled(
                        project_id, pool_id, True
                    )
            if remove_pool and operation == "pool_enable":
                # The pool endpoint maps its existing missing-pool ValueError to404.
                assert isinstance(failure.value, ValueError)
            else:
                assert failure.value.status_code == 409
                assert failure.value.detail["error_code"] == (
                    "ml_backend_topology_changed"
                    if operation == "registry_enable"
                    else "ml_backend_pool_not_enabled"
                )
            await writer.rollback()

        tasks.append(asyncio.create_task(enable()))
        await _blocked_by(test_engine, writer_pid, delete_pid)
        assert await MLBackendService(deleter).remove_pool_member(pool_id, registry_id)
        if remove_pool:
            assert await MLBackendService(deleter).delete_pool(pool_id)
        await deleter.commit()
        await asyncio.wait_for(tasks[0], 4)
        async with maker() as observer:
            assert (
                await observer.scalar(
                    select(ProjectMLBackendPool.id).where(
                        ProjectMLBackendPool.project_id == project_id
                    )
                )
                is None
            )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await deleter.rollback()
        await writer.rollback()
        await deleter.close()
        await writer.close()
        async with maker.begin() as cleanup:
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id == actor_id))
        await _cleanup(test_engine, registry_id, pool_id)


async def test_empty_pool_delete_retries_busy_project_binding_without_partial_delete(
    test_engine,
):
    from fastapi import HTTPException
    from tests.factory import create_user, create_project
    from app.db.models.project import Project
    from app.db.models.user import User

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as seed:
        assert await MLBackendService(seed).remove_pool_member(pool_id, registry_id)
        user = await create_user(
            seed,
            "project_admin",
            f"empty-pool-{uuid.uuid4().hex}@example.test",
            "Owner",
        )
        project = await create_project(seed, owner_id=user.id)
        project.ml_backend_pool_id = pool_id
        actor_id, project_id = user.id, project.id
    workspace, deleter = maker(), maker()
    try:
        await _timeouts(workspace)
        await _timeouts(deleter)
        await workspace.execute(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        with pytest.raises(HTTPException) as failure:
            await MLBackendService(deleter).delete_pool(pool_id)
        assert failure.value.status_code == 409
        assert failure.value.detail == {"reason": "project_busy"}
        await deleter.rollback()
        async with maker() as observer:
            assert await observer.get(MLBackendServicePool, pool_id) is not None
            assert (
                await observer.get(Project, project_id)
            ).ml_backend_pool_id == pool_id
        await workspace.rollback()
        assert await MLBackendService(deleter).delete_pool(pool_id)
        await deleter.commit()
        async with maker() as observer:
            assert await observer.get(MLBackendServicePool, pool_id) is None
            assert (await observer.get(Project, project_id)).ml_backend_pool_id is None
    finally:
        await workspace.rollback()
        await deleter.rollback()
        await workspace.close()
        await deleter.close()
        async with maker.begin() as cleanup:
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id == actor_id))
        await _cleanup(test_engine, registry_id, pool_id)
