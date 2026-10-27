"""Real Agent stage and SQL owners; network and independent admission are seams."""

from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

from aap_filesystem_read import RootSpec, capture_anchor, probe_directory
from PIL import Image
from sqlalchemy import delete, func, select

from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import StorageLocationProbe
from app.db.models.site_agent import AgentCredential
from app.db.models.site_agent_command import SiteAgentCommand
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import agent_transport as transport
from app.services import dataset_registration as registration, dataset_scans as scans
from app.services import storage_location as locations
from app.services.dataset_references import reference_snapshot
from app.workers import dataset_registration as registration_worker
from tests.test_agent_dataset_scans import NODE, agent_world as agent_world


async def test_agent_scan_preflight_confirm_and_authorized_original_share_existing_owners(
    db_session, test_engine, agent_world, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[2] / "storage-agent")
    )
    from storage_agent.config import Config, Credentials
    from storage_agent.journal import Journal
    from storage_agent.runner import Runner

    db, world = db_session, agent_world
    location_id, agent_id = world.location.id, world.agent.id
    root = tmp_path / "root"
    directory = root / "mounted" / "dataset"
    directory.mkdir(parents=True)
    output = io.BytesIO()
    Image.new("RGB", (48, 32), "red").save(output, format="PNG")
    data = output.getvalue()
    (directory / "图片.png").write_bytes(data)
    root_spec = RootSpec("photos", str(root))
    location_spec = replace(root_spec, relative_base="mounted")
    world.location.config = {
        **world.location.config,
        "agent_anchor": capture_anchor(location_spec),
        "cache_policy": "none",
    }
    world.source.config = {**world.source.config, "cache_policy": "none"}
    # Replace the generic fixture's synthetic probe before this real root is qualified.
    await db.execute(
        delete(StorageLocationProbe).where(
            StorageLocationProbe.location_id == location_id
        )
    )
    probe = {
        **probe_directory(location_spec, "dataset/图片.png"),
        "maintenance_revision": 1,
    }
    await locations.store_probe(
        db, world.location, f"agent:{agent_id}", probe, transport.now()
    )
    await db.commit()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config = Config(
        "https://center.example", state, tmp_path / "credentials", {"photos": root_spec}
    )
    credentials = Credentials(str(agent_id), 1, "s" * 40, int(time.time()) + 3600)
    rows = []
    client = transport.redis_client()
    try:
        with Journal(state) as journal:
            runner = Runner(config, credentials, journal)

            async def acquired(
                session, where, purpose, *, expected_revision=None, node_id=None
            ):
                # A SAVEPOINT fixture cannot commit the independent operation's
                # closure to another connection. Real admission has its own tests.
                revision = await transport.storage_access.check(
                    session, where, expected_revision=expected_revision
                )

                @asynccontextmanager
                async def permit():
                    yield SimpleNamespace(id=uuid.uuid4(), revision=revision)

                return permit()

            monkeypatch.setattr(transport.storage_access, "acquire", acquired)

            async def deliver(session, command_id):
                rows.append(command_id)
                command = await transport.lease(
                    session, agent_id, uuid.UUID(journal.poll_session(credentials))
                )
                assert command is not None and command["command_id"] == str(command_id)
                await session.commit()
                result = runner.execute(command)
                assert result["status"] == "completed", result
                credential = await session.get(AgentCredential, (agent_id, 1))
                if command["operation"] == "read":
                    with runner.outgoing(result) as body:
                        offset = 0
                        while chunk := body.read(transport.CHUNK_BYTES):
                            await transport.receive_chunk(
                                session,
                                command_id,
                                world.agent,
                                credential,
                                result["command_digest"],
                                offset,
                                hashlib.sha256(chunk).hexdigest(),
                                chunk,
                            )
                            offset += len(chunk)
                ack = await transport.receive_result(
                    session, command_id, world.agent, credential, result
                )
                await session.commit()
                journal.acknowledge(ack)
                return await session.get(
                    SiteAgentCommand, command_id, populate_existing=True
                )

            monkeypatch.setattr(transport, "wait", deliver)
            run, _ = await scans.start_scan(db, world.source.id, world.actor)
            job = await db.get(AsyncJob, run.async_job_id)
            scan_id, task_id = run.id, job.celery_task_id
            fence = await scans.claim(db, scan_id, task_id, NODE)
            await db.commit()
            for _ in range(10):
                work = await scans.next_directory(db, scan_id, task_id, fence)
                await db.commit()
                if work is None:
                    break
                if work.get("transition"):
                    continue
                receipt = await transport.exchange(
                    db,
                    world.location,
                    "scan",
                    {
                        "path": work["path"],
                        "expected_anchor": work["anchor"],
                        "expected_identity": work["identity"],
                    },
                    fence=work["agent_fence"],
                    purpose="scan",
                    node_id=NODE,
                    expected_revision=work["maintenance_revision"],
                )
                await scans.apply_directory(
                    db, scan_id, task_id, fence, work, receipt.result["result"]
                )
                await transport.acknowledge(db, receipt, accepted=True)
                await db.commit()
            assert run.complete
            assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 0

            registration_run, _ = await registration.start_preflight(
                db, scan_id, world.actor
            )

            async def finish_phase():
                job = await db.get(AsyncJob, registration_run.async_job_id)
                run_id, task_id = registration_run.id, job.celery_task_id
                fence = await registration.claim(db, run_id, task_id, NODE)
                await db.commit()
                for _ in range(10):
                    work = await registration.next_entry(db, run_id, task_id, fence)
                    await db.commit()
                    if work is None:
                        return
                    await registration_worker.process_one(
                        db, run_id, task_id, fence, work
                    )
                raise AssertionError("Registration did not finish")

            await finish_phase()
            assert registration_run.counts["ready_count"] == 1
            assert await db.scalar(select(func.count()).select_from(DatasetItem)) == 0
            registration_run, _ = await registration.confirm(
                db, registration_run.id, world.actor
            )
            await finish_phase()
            assert registration_run.counts["registered_count"] == 1
            assert registration_run.counts["original_copy_bytes"] == 0
            item = await db.scalar(
                select(DatasetItem).where(DatasetItem.dataset_id == world.dataset.id)
            )
            snapshot = await reference_snapshot(
                db,
                item.id,
                world.actor,
                expected_sha256=hashlib.sha256(data).hexdigest(),
            )
            try:
                assert snapshot.file.read() == data
            finally:
                snapshot.close()
            commands = (
                await db.scalars(
                    select(SiteAgentCommand).where(SiteAgentCommand.id.in_(rows))
                )
            ).all()
            assert {row.state for row in commands} == {"applied"}
            assert {row.operation_id is not None for row in commands} == {True}
            assert (directory / "图片.png").read_bytes() == data
    finally:
        for command_id in rows:
            await client.delete(transport.relay_key(command_id))
        await client.aclose()
        await db.rollback()
        await db.bind.rollback()
        async with test_engine.begin() as connection:
            await connection.execute(
                delete(StorageOperation).where(
                    StorageOperation.location_id == location_id
                )
            )
            await connection.execute(
                delete(StorageMaintenanceGate).where(
                    StorageMaintenanceGate.location_id == location_id
                )
            )
