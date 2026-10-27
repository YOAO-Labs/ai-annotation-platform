"""Controlled PostgreSQL overlaps for the Inference registry/pool lock graph."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_inference.topology import RegistryTopologySnapshot
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.inference_health_data import InferenceHealthData
from app.services.inference_topology_data import InferenceTopologyData
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

_UPSERT_LOCK = "aap_inference_topology_v1.lock_member_upsert_rows("
_REMOVE_LOCK = "aap_inference_topology_v1.remove_member("
_DRIFT_LOCK = "aap_inference_topology_v1.lock_drift_acceptance_rows("
_DELETE_LOCK = "aap_inference_topology_v1.delete_registry("


class PoolGateSession(AsyncSession):
    """Pause one session after its Native call acquired the pooled locks."""

    def __init__(self, *args, gate, pool_locked, release_pool, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate_fragments = tuple(fragment.lower() for fragment in gate)
        self.pool_locked = pool_locked
        self.release_pool = release_pool
        self.gated = False

    async def _gate(self, statement):
        source = str(statement).lower()
        if not self.gated and any(
            fragment in source for fragment in self.gate_fragments
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


def _fake_health(monkeypatch):
    """Answer the coordinator's caller-UOW health client without any network hop."""

    class Client:
        async def health_meta(self, *, gpu_health_challenge=None):
            return True, {"compute": {"configured_device": "cpu"}}

        async def setup(self):
            return _SETUP

    def factory(db):
        def create(backend):
            return Client()

        return create

    monkeypatch.setattr(
        "app.services.inference_backend_control.health_observation_client_factory",
        factory,
    )


async def _seed(test_engine, monkeypatch, *, state="active"):
    """Create a connected registry whose singleton pool carries the capability baseline."""
    _fake_health(monkeypatch)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        service = MLBackendService(db)
        backend = await service.create_registry(
            "Concurrent topology " + uuid.uuid4().hex,
            f"http://controlled-{uuid.uuid4().hex}.invalid",
        )
        assert await service.check_health(backend.id) is True
        pool_id = await service.pool_id_for_registry(backend.id)
        if state == "draining":
            await service.drain_pool_member(pool_id, backend.id)
        else:
            assert state == "active"
        return backend.id, pool_id


async def _snapshot(db, registry_id):
    data = InferenceTopologyData(db)
    pool = await data.pool_for_registry(registry_id)
    member = None
    if pool is not None:
        member = next(
            (
                row
                for row in await data.pool_members(pool.id)
                if row.registry_id == registry_id
            ),
            None,
        )
    if pool is None or member is None:
        return RegistryTopologySnapshot(registry_id, None, None, None, None)
    return RegistryTopologySnapshot(
        registry_id=registry_id,
        member_id=member.id,
        pool_id=pool.id,
        routing_generation=pool.routing_generation,
        traffic_state=member.traffic_state,
    )


def _local_authority(monkeypatch, maker, mutate=None):
    """Stand in for the durable routing owner while preserving the observed topology."""

    class Authority:
        async def quiescence(self, registry_id):
            async with maker() as db:
                snapshot = await _snapshot(db, registry_id)
            if mutate is not None:
                await mutate()
            return SimpleNamespace(
                status="ok",
                topology=SimpleNamespace(
                    member_id=snapshot.member_id,
                    pool_id=snapshot.pool_id,
                    routing_generation=snapshot.routing_generation,
                    traffic_state=snapshot.traffic_state,
                ),
            )

    monkeypatch.setattr("app.services.ml_routing.safety.RoutingAuthority", Authority)


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


@pytest.mark.parametrize("operation", ["remove", "upsert"])
async def test_health_and_pool_writer_complete_without_deadlock(
    test_engine, monkeypatch, operation
):
    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, monkeypatch)
    pool_locked = asyncio.Event()
    release = asyncio.Event()
    health_started = asyncio.Event()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    writer = PoolGateSession(
        bind=test_engine,
        expire_on_commit=False,
        gate=((_REMOVE_LOCK,) if operation == "remove" else (_UPSERT_LOCK,)),
        pool_locked=pool_locked,
        release_pool=release,
    )
    health = maker()

    class ObservedData(InferenceHealthData):
        async def lock_current_backend(self, registry_id):
            health_started.set()
            return await super().lock_current_backend(registry_id)

    monkeypatch.setattr(
        "app.services.inference_backend_control.InferenceHealthData", ObservedData
    )
    tasks = []
    try:
        writer_pid = await _timeouts(writer)
        health_pid = await _timeouts(health)

        async def mutate():
            snapshot = await _snapshot(writer, registry_id)
            if operation == "remove":
                result = await MLBackendService(writer).remove_pool_member(
                    pool_id, registry_id, expected_topology=snapshot
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
    test_engine, monkeypatch, operation
):
    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(
        test_engine,
        monkeypatch,
        state="draining" if operation == "resume" else "active",
    )
    pool_locked = asyncio.Event()
    release = asyncio.Event()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    writer = PoolGateSession(
        bind=test_engine,
        expire_on_commit=False,
        gate=(_UPSERT_LOCK,),
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


@pytest.mark.parametrize("change", ["resume", "recreate", "move", "generation"])
@pytest.mark.parametrize("operation", ["remove", "delete"])
async def test_destructive_write_rejects_changed_topology_after_quiescence(
    test_engine, monkeypatch, change, operation
):
    from app.services.ml_routing.contracts import MLBackendQuiescenceError
    from app.services.ml_routing.safety import require_registry_quiescent
    from app.application.ml_registry_deletion import delete_registry

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, monkeypatch, state="draining")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    extra_pools = []

    async def mutate():
        async with maker.begin() as other:
            service = MLBackendService(other)
            if change == "resume":
                await service.resume_pool_member(pool_id, registry_id)
            elif change == "generation":
                await service.update_pool(pool_id, name="Changed generation")
            else:
                await service.remove_pool_member(
                    pool_id,
                    registry_id,
                    expected_topology=await _snapshot(other, registry_id),
                )
                if change == "move":
                    pool = await service.create_pool("Moved member")
                    extra_pools.append(pool.id)
                    target = pool.id
                else:
                    target = pool_id
                await service.add_pool_member(target, registry_id)
                await service.drain_pool_member(target, registry_id)

    _local_authority(monkeypatch, maker, mutate)
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
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.application.ml_registry_deletion import delete_registry

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, monkeypatch, state="draining")
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
    _local_authority(monkeypatch, maker)
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
    registry_id, pool_id = await _seed(test_engine, monkeypatch)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as seed:
        other = await MLBackendService(seed).create_registry(
            "Other concurrent member", "http://other-health.invalid"
        )
        assert await MLBackendService(seed).check_health(other.id) is True
        other_pool = await MLBackendService(seed).pool_id_for_registry(other.id)
        await MLBackendService(seed).remove_pool_member(
            other_pool,
            other.id,
            expected_topology=await _snapshot(seed, other.id),
        )
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
        gate=(_DRIFT_LOCK,),
        pool_locked=pool_locked,
        release_pool=release,
    )
    health = maker()
    tasks = []

    class ObservedData(InferenceHealthData):
        async def lock_current_backend(self, registry_id):
            health_started.set()
            return await super().lock_current_backend(registry_id)

    monkeypatch.setattr(
        "app.services.inference_backend_control.InferenceHealthData", ObservedData
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
    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, monkeypatch)
    member_deleted = asyncio.Event()
    release = asyncio.Event()
    health_started = asyncio.Event()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    writer = PoolGateSession(
        bind=test_engine,
        expire_on_commit=False,
        gate=(_DELETE_LOCK,),
        pool_locked=member_deleted,
        release_pool=release,
    )
    health = maker()
    tasks = []

    class ObservedData(InferenceHealthData):
        async def lock_current_backend(self, registry_id):
            health_started.set()
            return await super().lock_current_backend(registry_id)

    monkeypatch.setattr(
        "app.services.inference_backend_control.InferenceHealthData", ObservedData
    )
    try:
        writer_pid = await _timeouts(writer)
        health_pid = await _timeouts(health)
        snapshot = await _snapshot(writer, registry_id)

        async def remove():
            result = await MLBackendService(writer).delete_with_topology(
                registry_id, expected_topology=snapshot
            )
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
        assert results[0].deleted is True, results
        assert results[1] is False, results
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


@pytest.mark.parametrize("change", ["delete", "move", "delete_pool"])
async def test_workspace_enablement_rejects_membership_changed_while_waiting(
    test_engine, monkeypatch, change
):
    from sqlalchemy.exc import DBAPIError
    from tests.factory import create_user, create_project
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.db.models.ml_backend_registry import ProjectMLBackendPool
    from app.services.workspace_configuration_data import _diagnostic
    from aap_workspace.configuration_postgres import configuration_busy_error

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, monkeypatch, state="draining")
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
            deleted = await service.delete_with_topology(
                registry_id, expected_topology=await _snapshot(writer, registry_id)
            )
            assert deleted.deleted is True
        else:
            assert await service.remove_pool_member(
                pool_id,
                registry_id,
                expected_topology=await _snapshot(writer, registry_id),
            )
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
async def test_project_enablement_locks_project_before_pool(
    test_engine, monkeypatch, operation
):
    from tests.factory import create_user, create_project
    from app.db.models.project import Project
    from app.db.models.user import User

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, monkeypatch)
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
    test_engine, monkeypatch, operation, remove_pool
):
    from fastapi import HTTPException
    from tests.factory import create_user, create_project
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.db.models.ml_backend_registry import ProjectMLBackendPool

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, monkeypatch)
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
        assert await MLBackendService(deleter).remove_pool_member(
            pool_id,
            registry_id,
            expected_topology=await _snapshot(deleter, registry_id),
        )
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
    test_engine, monkeypatch
):
    from fastapi import HTTPException
    from tests.factory import create_user, create_project
    from app.db.models.project import Project
    from app.db.models.user import User

    await assert_owned_test_database(test_engine)
    registry_id, pool_id = await _seed(test_engine, monkeypatch)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as seed:
        assert await MLBackendService(seed).remove_pool_member(
            pool_id,
            registry_id,
            expected_topology=await _snapshot(seed, registry_id),
        )
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


async def _seed_repair_route(test_engine, monkeypatch):
    """Create one enabled project route whose singleton pool is usable."""
    from tests.factory import create_user, create_project

    registry_id, pool_id = await _seed(test_engine, monkeypatch)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as seed:
        user = await create_user(
            seed,
            "project_admin",
            f"repair-route-{uuid.uuid4().hex}@example.test",
            "Owner",
        )
        project = await create_project(seed, owner_id=user.id)
        (await seed.get(MLBackendServicePool, pool_id)).enabled = True
        await seed.flush()
        association = await MLBackendService(seed).set_pool_enabled(
            project.id, pool_id, True
        )
        assert association.enabled is True
        return registry_id, pool_id, project.id, user.id


async def _cleanup_repair_route(
    test_engine, registry_id, pool_id, project_id, actor_id, *extra_pool_ids
):
    from app.db.models.project import Project
    from app.db.models.user import User

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as cleanup:
        await cleanup.execute(delete(Project).where(Project.id == project_id))
        await cleanup.execute(delete(User).where(User.id == actor_id))
        for extra_pool_id in extra_pool_ids:
            await cleanup.execute(
                delete(MLBackendServicePool).where(
                    MLBackendServicePool.id == extra_pool_id
                )
            )
    await _cleanup(test_engine, registry_id, pool_id)


@pytest.mark.parametrize("release", ["commit", "rollback"])
async def test_repair_model_lock_holds_route_until_transaction_ends(
    test_engine, monkeypatch, release
):
    await assert_owned_test_database(test_engine)
    registry_id, pool_id, project_id, actor_id = await _seed_repair_route(
        test_engine, monkeypatch
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    holder, peer = maker(), maker()
    tasks = []
    try:
        holder_pid = await _timeouts(holder)
        peer_pid = await _timeouts(peer)
        assert (
            await InferenceTopologyData(holder).lock_repair_model_rows(
                project_id, pool_id, [registry_id]
            )
            == "ready"
        )

        async def drain():
            await MLBackendService(peer).drain_pool_member(pool_id, registry_id)
            await peer.commit()

        tasks.append(asyncio.create_task(drain()))
        await _blocked_by(test_engine, peer_pid, holder_pid)
        if release == "commit":
            await holder.commit()
        else:
            await holder.rollback()
        await asyncio.wait_for(tasks[0], 4)
        async with maker() as observer:
            member = await observer.scalar(
                select(MLBackendPoolMember).where(
                    MLBackendPoolMember.registry_id == registry_id
                )
            )
            assert member.traffic_state == "draining"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await holder.rollback()
        await peer.rollback()
        await holder.close()
        await peer.close()
        await _cleanup_repair_route(
            test_engine, registry_id, pool_id, project_id, actor_id
        )


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("pool_disabled", "backend_unavailable"),
        ("association_disabled", "backend_unavailable"),
        ("association_missing", "backend_unavailable"),
        ("member_missing", "model_changed"),
        ("member_draining", "model_changed"),
        ("member_moved", "model_changed"),
        ("unknown_registry", "model_changed"),
    ],
)
async def test_repair_model_lock_reports_unusable_routes(
    test_engine, monkeypatch, change, expected
):
    from app.db.models.ml_backend_registry import ProjectMLBackendPool

    await assert_owned_test_database(test_engine)
    registry_id, pool_id, project_id, actor_id = await _seed_repair_route(
        test_engine, monkeypatch
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    destination_pool_id = None
    requested_registry_ids = [registry_id]
    try:
        async with maker.begin() as change_db:
            if change == "pool_disabled":
                await change_db.execute(
                    update(MLBackendServicePool)
                    .where(MLBackendServicePool.id == pool_id)
                    .values(enabled=False)
                )
            elif change == "association_disabled":
                await change_db.execute(
                    update(ProjectMLBackendPool)
                    .where(
                        ProjectMLBackendPool.project_id == project_id,
                        ProjectMLBackendPool.pool_id == pool_id,
                    )
                    .values(enabled=False)
                )
            elif change == "association_missing":
                await change_db.execute(
                    delete(ProjectMLBackendPool).where(
                        ProjectMLBackendPool.project_id == project_id,
                        ProjectMLBackendPool.pool_id == pool_id,
                    )
                )
            elif change == "member_missing":
                await change_db.execute(
                    delete(MLBackendPoolMember).where(
                        MLBackendPoolMember.registry_id == registry_id
                    )
                )
            elif change == "member_draining":
                await change_db.execute(
                    update(MLBackendPoolMember)
                    .where(MLBackendPoolMember.registry_id == registry_id)
                    .values(traffic_state="draining")
                )
            elif change == "member_moved":
                destination = await MLBackendService(change_db).create_pool(
                    "Repair route destination"
                )
                destination_pool_id = destination.id
                await change_db.execute(
                    update(MLBackendPoolMember)
                    .where(MLBackendPoolMember.registry_id == registry_id)
                    .values(pool_id=destination_pool_id)
                )
            else:
                assert change == "unknown_registry"
                requested_registry_ids = [uuid.uuid4()]

        async with maker() as db:
            try:
                assert (
                    await InferenceTopologyData(db).lock_repair_model_rows(
                        project_id, pool_id, requested_registry_ids
                    )
                    == expected
                )
            finally:
                await db.rollback()
    finally:
        await _cleanup_repair_route(
            test_engine,
            registry_id,
            pool_id,
            project_id,
            actor_id,
            *([destination_pool_id] if destination_pool_id is not None else []),
        )


async def test_repair_model_lock_propagates_nowait_conflict(test_engine, monkeypatch):
    from sqlalchemy.exc import DBAPIError

    from app.services.inference_topology_data import _sqlstate

    await assert_owned_test_database(test_engine)
    registry_id, pool_id, project_id, actor_id = await _seed_repair_route(
        test_engine, monkeypatch
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    holder, peer = maker(), maker()
    try:
        await _timeouts(holder)
        await _timeouts(peer)
        await peer.execute(
            select(MLBackendServicePool.id)
            .where(MLBackendServicePool.id == pool_id)
            .with_for_update()
        )
        with pytest.raises(DBAPIError) as failure:
            await InferenceTopologyData(holder).lock_repair_model_rows(
                project_id, pool_id, [registry_id]
            )
        assert _sqlstate(failure.value) == "55P03"
    finally:
        await holder.rollback()
        await peer.rollback()
        await holder.close()
        await peer.close()
        await _cleanup_repair_route(
            test_engine, registry_id, pool_id, project_id, actor_id
        )


async def test_repair_model_lock_validates_and_deduplicates_registry_ids(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    registry_id, pool_id, project_id, actor_id = await _seed_repair_route(
        test_engine, monkeypatch
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        try:
            data = InferenceTopologyData(db)
            with pytest.raises(ValueError):
                await data.lock_repair_model_rows(project_id, pool_id, [])
            with pytest.raises(ValueError):
                await data.lock_repair_model_rows(
                    project_id, pool_id, [registry_id, uuid.uuid4(), uuid.uuid4()]
                )
            assert (
                await data.lock_repair_model_rows(
                    project_id, pool_id, [registry_id, registry_id]
                )
                == "ready"
            )
        finally:
            await db.rollback()
    await _cleanup_repair_route(test_engine, registry_id, pool_id, project_id, actor_id)
