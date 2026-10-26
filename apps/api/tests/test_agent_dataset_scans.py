"""Remote I/O feeds the existing inventory ledger and periodic sync owners."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import stat
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import func, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import StorageLocation, StorageLocationProbe
from app.db.models.dataset_source import DatasetScanEntry, DatasetScanRun
from app.db.models.site_agent import AgentCredential, AgentRegistration, Site
from app.schemas.dataset_source import DatasetSourceCreate, SourceSyncScheduleUpdate
from app.services import dataset_scans as scans, dataset_source_sync as sync
from app.services import filesystem_storage as fs, storage_access
from app.services import storage_location as locations
from app.services.site_agents import SiteAgentError
from app.workers import dataset_scan as worker, dataset_source_sync as sync_worker

NODE = "agent-scan@test"
ANCHOR = {"path_digest": "a" * 64, "root": [7, 11], "base": [7, 12]}
HOST = "b" * 64


def probe_result(host=HOST):
    return {
        "ok": True,
        "code": "ok",
        "anchor": ANCHOR,
        "host_id": host,
        "maintenance_revision": 1,
        "capabilities": {
            "list": "verified",
            "read": "verified",
            "range": "verified",
            "write": "unsupported",
            "delete": "unsupported",
            "presign": "unsupported",
        },
    }


@pytest.fixture
async def agent_world(db_session, super_admin):
    actor = super_admin[0]
    stamp = datetime.now(timezone.utc)
    site = Site(name=f"scan-site-{uuid.uuid4().hex}", created_by=actor.id)
    db_session.add(site)
    await db_session.flush()
    limits = {
        "max_concurrency": 2,
        "bandwidth_bytes_per_second": 1024 * 1024,
        "max_bytes": 64 * 1024 * 1024,
    }
    agent = AgentRegistration(
        site_id=site.id,
        name="Inventory Agent",
        protocol_version=1,
        credential_revision=1,
        roots=[{"root_handle": "photos", "root_revision": 1}],
        limits=limits,
        reported_limits=limits,
        session_id=uuid.uuid4(),
        last_seen=stamp,
        created_by=actor.id,
    )
    dataset = Dataset(
        display_id=f"ds-{uuid.uuid4().hex[:10]}",
        name="Remote inventory",
        created_by=actor.id,
    )
    db_session.add_all([agent, dataset])
    await db_session.flush()
    db_session.add(
        AgentCredential(
            agent_id=agent.id,
            revision=1,
            token_digest=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            expires_at=stamp + timedelta(days=1),
        )
    )
    location = StorageLocation(
        id=uuid.uuid4(),
        name=f"remote-{uuid.uuid4().hex}",
        kind="agent",
        config_revision=1,
        enabled=True,
        config={
            "agent_id": str(agent.id),
            "root_handle": "photos",
            "root_revision": 1,
            "relative_path": "mounted",
            "scope": "global",
            "use_user_ids": [],
            "cache_policy": "preview",
            "agent_anchor": ANCHOR,
        },
    )
    db_session.add(location)
    await db_session.flush()
    await locations.store_probe(
        db_session, location, f"agent:{agent.id}", probe_result(), stamp
    )
    source = await scans.create_source(
        db_session,
        DatasetSourceCreate(
            dataset_id=dataset.id,
            location_id=location.id,
            name="Agent inventory",
            relative_path="dataset",
            node_id=NODE,
        ),
        actor,
    )
    await db_session.commit()
    return SimpleNamespace(
        actor=actor, dataset=dataset, location=location, source=source, agent=agent
    )


def entry(name, inode, kind="file"):
    return {
        "name": name,
        "kind": kind,
        "identity": [7, inode],
        "mode": (stat.S_IFDIR | 0o755)
        if kind == "directory"
        else (stat.S_IFREG | 0o644),
        "size": 0 if kind == "directory" else 3,
        "mtime_ns": 1,
        "ctime_ns": 1,
    }


def manifest(path):
    if path == "dataset":
        identity = [7, 21]
        entries = [entry("a.png", 31), entry("child", 22, "directory")]
    else:
        assert path == "dataset/child"
        identity, entries = [7, 22], [entry("b.png", 32)]
    directory_digest = hashlib.sha256(
        json.dumps([identity, entries], sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()
    return {"identity": identity, "entries": entries, "digest": directory_digest}


async def begin(db, world):
    run, created = await scans.start_scan(db, world.source.id, world.actor)
    assert created
    job = await db.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db, run.id, job.celery_task_id, NODE)
    assert fence is not None
    await db.commit()
    return run, job, fence


async def next_work(db, run, job, fence):
    for _ in range(10):
        work = await scans.next_directory(db, run.id, job.celery_task_id, fence)
        await db.commit()
        if work is None or not work.get("transition"):
            return work
    raise AssertionError("Inventory did not advance")


async def test_agent_probe_and_host_are_remote_but_worker_and_fence_remain_central(
    db_session, agent_world
):
    world = agent_world
    run, job, fence = await begin(db_session, world)
    assert run.snapshot["scan_host_id"] == HOST
    assert world.source.config["node_id"] == NODE
    assert (
        await db_session.get(
            StorageLocationProbe, (world.location.id, f"worker:{NODE}")
        )
        is None
    )
    work = await next_work(db_session, run, job, fence)
    assert work["kind"] == "agent" and work["path"] == "dataset"
    assert work["anchor"] == ANCHOR and work["identity"] is None
    assert work["agent_fence"] == {
        "source_id": str(world.source.id),
        "run_id": str(run.id),
        "job_id": str(job.id),
        "task_id": job.celery_task_id,
        "generation": fence[0],
        "lease_token": str(fence[1]),
        "user_id": str(world.actor.id),
    }


@pytest.mark.parametrize(
    "path", ["../outside", "/outside", "child//file", "child\\file"]
)
async def test_agent_source_uses_filesystem_relative_path_rules(
    db_session, agent_world, path
):
    with pytest.raises(fs.FilesystemStorageError):
        await scans.create_source(
            db_session,
            DatasetSourceCreate(
                dataset_id=agent_world.dataset.id,
                location_id=agent_world.location.id,
                name="Invalid path",
                relative_path=path,
                node_id=NODE,
            ),
            agent_world.actor,
        )


async def test_revoked_agent_cannot_claim_a_previously_queued_scan(
    db_session, agent_world
):
    run, _ = await scans.start_scan(
        db_session, agent_world.source.id, agent_world.actor
    )
    job = await db_session.get(AsyncJob, run.async_job_id)
    agent_world.agent.revoked_at = await scans.now(db_session)
    await db_session.commit()
    assert await scans.claim(db_session, run.id, job.celery_task_id, NODE) is None
    await db_session.commit()
    assert run.status == job.status == "failed" and run.error_code == "agent_revoked"


async def test_agent_resume_preserves_checkpoint_and_rejects_previous_fence(
    db_session, agent_world
):
    world = agent_world
    run, old_job, old_fence = await begin(db_session, world)
    work = await next_work(db_session, run, old_job, old_fence)
    await scans.apply_directory(
        db_session,
        run.id,
        old_job.celery_task_id,
        old_fence,
        work,
        manifest(work["path"]),
    )
    await scans.cancel_job(db_session, old_job.id, world.actor)
    await db_session.commit()
    await locations.store_probe(
        db_session,
        world.location,
        f"agent:{world.agent.id}",
        probe_result("c" * 64),
        await scans.now(db_session),
    )
    await scans.resume_scan(db_session, run.id, world.actor)
    job = await db_session.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db_session, run.id, job.celery_task_id, NODE)
    await db_session.commit()
    assert fence != old_fence and job.id != old_job.id
    assert run.snapshot["scan_host_id"] == "c" * 64
    with pytest.raises(scans.LostScanLease):
        await scans.apply_directory(
            db_session,
            run.id,
            old_job.celery_task_id,
            old_fence,
            work,
            manifest(work["path"]),
        )
    for _ in range(20):
        current = await next_work(db_session, run, job, fence)
        if current is None:
            break
        await scans.apply_directory(
            db_session,
            run.id,
            job.celery_task_id,
            fence,
            current,
            manifest(current["path"]),
        )
        await db_session.commit()
    else:
        raise AssertionError("Resumed inventory did not complete")
    assert run.complete and run.counts["file_count"] == 2
    assert run.counts["directory_count"] == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.dataset_id == world.dataset.id)
        )
        == 0
    )


@pytest.fixture
def worker_session(db_session, monkeypatch):
    @asynccontextmanager
    async def session():
        yield db_session

    async def dispose():
        return None

    monkeypatch.setattr(
        worker,
        "create_async_engine",
        lambda *args, **kwargs: SimpleNamespace(dispose=dispose),
    )
    monkeypatch.setattr(worker, "async_sessionmaker", lambda *args, **kwargs: session)


def remote_exchange(monkeypatch, world, *, after_result=None, uncertain=False):
    from aap_site_agent_contract import digest, validate_result
    from app.services import agent_transport as transport

    receipts = []

    async def exchange(
        db,
        location,
        operation,
        arguments,
        *,
        fence,
        purpose,
        node_id,
        expected_revision,
    ):
        assert operation == purpose == "scan" and node_id == NODE
        assert (
            arguments["relative_path"] == "" and arguments["expected_anchor"] == ANCHOR
        )
        row = await transport.enqueue(
            db,
            location,
            operation,
            arguments,
            fence={
                **fence,
                "purpose": purpose,
                "maintenance_revision": expected_revision,
            },
        )
        assert row.payload["arguments"]["relative_path"] == "mounted"
        row.lease_session_id = world.agent.session_id
        row.state = "unsettled" if uncertain else "received"
        if not uncertain:
            row.result = validate_result(
                {
                    "protocol_version": 1,
                    "command_id": str(row.id),
                    "command_digest": row.command_digest,
                    "status": "completed",
                    "result": manifest(arguments["path"]),
                    "error_code": None,
                    "closed": True,
                }
            )
            transport.validate_payload(row.payload, row.result)
            row.result_digest, row.closed = digest(row.result), True
        await db.commit()
        receipts.append(row.id)
        if uncertain:
            from app.services.content_snapshot import ContentError

            raise storage_access.mark_unsettled(
                ContentError("agent_command_expired", 503)
            )
        if after_result:
            await after_result(db, row)
        return row

    monkeypatch.setattr(transport, "exchange", exchange)
    return receipts


async def queued(db, world):
    run, created = await scans.start_scan(db, world.source.id, world.actor)
    assert created
    job = await db.get(AsyncJob, run.async_job_id)
    await db.commit()
    return run.id, job.id, job.celery_task_id


async def test_worker_applies_agent_receipts_through_real_scan_owner(
    db_session, agent_world, worker_session, monkeypatch
):
    from app.db.models.site_agent_command import SiteAgentCommand

    receipts = remote_exchange(monkeypatch, agent_world)
    run_id, job_id, task_id = await queued(db_session, agent_world)
    await worker.execute_scan(run_id, task_id, NODE)
    run, job = (
        await db_session.get(DatasetScanRun, run_id),
        await db_session.get(AsyncJob, job_id),
    )
    assert run.complete and run.status == job.status == "completed"
    assert run.counts["file_count"] == 2 and run.counts["directory_count"] == 1
    commands = list(
        await db_session.scalars(
            select(SiteAgentCommand).where(SiteAgentCommand.id.in_(receipts))
        )
    )
    assert len(commands) == 4 and all(
        row.state == "applied" and row.closed for row in commands
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.dataset_id == agent_world.dataset.id)
        )
        == 0
    )


async def test_cancel_during_remote_io_rejects_closed_receipt_without_checkpoint(
    db_session, agent_world, worker_session, monkeypatch
):
    from app.db.models.site_agent_command import SiteAgentCommand

    async def cancel(db, row):
        await scans.cancel_job(
            db, uuid.UUID(row.payload["fence"]["job_id"]), agent_world.actor
        )
        await db.commit()

    receipts = remote_exchange(monkeypatch, agent_world, after_result=cancel)
    run_id, _, task_id = await queued(db_session, agent_world)
    await worker.execute_scan(run_id, task_id, NODE)
    run = await db_session.get(DatasetScanRun, run_id)
    command = await db_session.get(SiteAgentCommand, receipts[0])
    root = await db_session.get(DatasetScanEntry, (run_id, scans.path_digest("")))
    assert run.status == "cancelled" and not run.complete
    assert root.state == "pending" and root.directory_digest is None
    assert command.state == "rejected" and command.closed is True


@pytest.mark.parametrize("retire_fails", [False, True])
async def test_ack_failure_rolls_back_checkpoint_and_preserves_original_failure(
    db_session, agent_world, worker_session, monkeypatch, retire_fails
):
    from app.db.models.site_agent_command import SiteAgentCommand
    from app.services import agent_transport as transport

    receipts = remote_exchange(monkeypatch, agent_world)
    acknowledge = transport.acknowledge

    async def fail_ack(db, row, *, accepted, code=None):
        if accepted:
            await db.flush()
            raise fs.FilesystemStorageError("directory_changed")
        if retire_fails:
            raise RuntimeError("Receipt retirement failed")
        await acknowledge(db, row, accepted=accepted, code=code)

    monkeypatch.setattr(transport, "acknowledge", fail_ack)
    run_id, job_id, task_id = await queued(db_session, agent_world)
    await worker.execute_scan(run_id, task_id, NODE)
    run, job = (
        await db_session.get(DatasetScanRun, run_id),
        await db_session.get(AsyncJob, job_id),
    )
    command = await db_session.get(SiteAgentCommand, receipts[0])
    root = await db_session.get(DatasetScanEntry, (run_id, scans.path_digest("")))
    assert (
        run.status == job.status == "failed" and run.error_code == "directory_changed"
    )
    assert job.error_message == "directory_changed"
    assert root.state == "pending" and root.directory_digest is None
    assert command.state == ("received" if retire_fails else "rejected")
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetScanEntry)
            .where(DatasetScanEntry.run_id == run_id)
        )
        == 1
    )


async def test_unknown_remote_close_remains_unsettled_and_does_not_advance(
    db_session, agent_world, worker_session, monkeypatch
):
    from app.db.models.site_agent_command import SiteAgentCommand

    receipts = remote_exchange(monkeypatch, agent_world, uncertain=True)
    run_id, _, task_id = await queued(db_session, agent_world)
    await worker.execute_scan(run_id, task_id, NODE)
    run = await db_session.get(DatasetScanRun, run_id)
    command = await db_session.get(SiteAgentCommand, receipts[0])
    root = await db_session.get(DatasetScanEntry, (run_id, scans.path_digest("")))
    assert run.status == "failed" and run.error_code == "storage_operation_unsettled"
    assert command.state == "unsettled" and command.closed is None
    assert root.state == "pending" and root.directory_digest is None


async def test_periodic_sync_stores_remote_probe_and_dispatches_central_worker(
    db_session, agent_world, monkeypatch
):
    world = agent_world
    row = await sync.configure(
        db_session,
        world.source.id,
        SourceSyncScheduleUpdate(expected_revision=0, enabled=True),
        world.actor,
    )
    await db_session.commit()
    probes, dispatched = [], []

    async def probe(db, location, node, path, **kwargs):
        probes.append(node)
        return probe_result("d" * 64)

    async def dispatch(db, run_id):
        dispatched.append(run_id)

    monkeypatch.setattr(locations, "perform_probe", probe)
    monkeypatch.setattr(worker, "dispatch", dispatch)
    wakeup = await sync.claim_due(db_session, world.source.id)
    await db_session.commit()
    await sync.process(db_session, *wakeup)
    stored = await db_session.get(
        StorageLocationProbe,
        (world.location.id, f"agent:{world.agent.id}"),
        populate_existing=True,
    )
    run = await db_session.get(DatasetScanRun, row.scan_id)
    assert stored.result["host_id"] == run.snapshot["scan_host_id"] == "d" * 64
    assert probes == [f"worker:{NODE}"] and dispatched == [run.id]
    assert (
        await db_session.get(
            StorageLocationProbe, (world.location.id, f"worker:{NODE}")
        )
        is None
    )


@pytest.mark.parametrize(
    "code,unsettled,blocked",
    [
        ("agent_revoked", False, True),
        ("agent_unpaired", False, True),
        ("credential_expired", False, True),
        ("credential_revision_changed", False, True),
        ("agent_root_revision_changed", False, True),
        ("agent_location_changed", False, True),
        ("agent_offline", False, False),
        ("agent_unavailable", False, False),
        ("agent_command_expired", False, False),
        ("agent_draining", False, False),
        ("agent_command_expired", True, True),
    ],
)
async def test_periodic_sync_blocks_permanent_agent_failure_and_bounds_offline_retry(
    db_session, agent_world, monkeypatch, code, unsettled, blocked
):
    world = agent_world
    row = await sync.configure(
        db_session,
        world.source.id,
        SourceSyncScheduleUpdate(expected_revision=0, enabled=True),
        world.actor,
    )
    await db_session.commit()
    wakeup = await sync.claim_due(db_session, world.source.id)
    await db_session.commit()

    async def probe(*args, **kwargs):
        failure = SiteAgentError(code, 409)
        if unsettled:
            storage_access.mark_unsettled(failure)
        raise failure

    monkeypatch.setattr(locations, "perform_probe", probe)
    with pytest.raises(fs.FilesystemStorageError) as failure:
        await sync.process(db_session, *wakeup)
    await db_session.rollback()
    expected = "storage_operation_unsettled" if unsettled else code
    assert sync_worker.error_code(failure.value) == expected
    await sync.record_failure(
        db_session, *wakeup[:3], sync_worker.error_code(failure.value)
    )
    await db_session.commit()
    await db_session.refresh(row)
    assert row.last_error == expected and row.enabled is not blocked
    assert (row.state == "blocked") is blocked
    assert row.dispatch_token is None and row.next_check_at > row.last_checked_at
