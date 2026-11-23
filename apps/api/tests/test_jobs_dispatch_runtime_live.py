"""Process-loss recovery through the normal Jobs sender, Celery worker and Beat.

Model responses and retained-byte readers are fixtures. PostgreSQL, Redis,
the publisher process death, replay and frame writes are real.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from types import SimpleNamespace

from celery import signals
from celery.contrib.testing.worker import start_worker
import pytest
import redis
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from yoao_jobs.dispatch_postgres import JobsDispatchPostgres
from app.application.preannotation_contract import (
    FramePreannotationDispatchRequest,
    VideoFrameTaskPlan,
)
from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.prediction import Prediction
from app.workers import frame_preannotate
from app.workers.celery_app import celery_app
from tests.jobs_recovery_helpers import (
    jobs_broker_env,
    jobs_child,
    jobs_recovery_worker,
    jobs_secret,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_frame_preannotate_persistence import (
    _install_frame_execution_fake,
    _install_video_gateway,
)
from tests.test_frame_preannotate_persistence import frame_world as frame_world
from tests.test_identity_sessions_live import _verify_redis

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_JOBS_DISPATCH_RUNTIME_LIVE") != "1",
        reason="requires the verifier's isolated database and broker",
    ),
]


class _SecondPublicationGate:
    """Forward real RESP traffic, but hold the second LPUSH before Redis sees it."""

    def __init__(self, target):
        self.target = target
        self.publications = 0
        self.blocked = asyncio.Event()
        self.connections = set()

    async def _request(self, reader):
        head = await reader.readline()
        if not head:
            return None
        assert head.startswith(b"*"), "expected a Redis command array"
        raw = bytearray(head)
        values = []
        for _ in range(int(head[1:-2])):
            length = await reader.readline()
            assert length.startswith(b"$")
            body = await reader.readexactly(int(length[1:-2]) + 2)
            raw.extend(length)
            raw.extend(body)
            values.append(body[:-2])
        return bytes(raw), values

    async def _connection(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        target_writer = None
        response_task = None
        try:
            target_reader, target_writer = await asyncio.open_connection(
                self.target.host, self.target.port
            )

            async def responses():
                while body := await target_reader.read(65536):
                    writer.write(body)
                    await writer.drain()

            response_task = asyncio.create_task(responses())
            while packet := await self._request(reader):
                raw, values = packet
                if values[0].upper() == b"LPUSH":
                    self.publications += 1
                    if self.publications == 2:
                        self.blocked.set()
                        await asyncio.Future()
                target_writer.write(raw)
                await target_writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if response_task is not None:
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)
            if target_writer is not None:
                target_writer.close()
                await target_writer.wait_closed()
            writer.close()
            await writer.wait_closed()
            self.connections.discard(task)

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._connection, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self.server.close()
        for task in tuple(self.connections):
            task.cancel()
        await asyncio.gather(*tuple(self.connections), return_exceptions=True)
        await self.server.wait_closed()


_PUBLISHER = r"""
import asyncio, os
from pathlib import Path
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yoao_jobs.dispatch_contract import DispatchLease
from app.services.jobs_dispatch import publish_prepared_dispatch
from app.services.jobs_queue import publish_queue_envelope

async def main():
    lease = DispatchLease.model_validate_json(Path(os.environ['LEASE_FILE']).read_text())
    dsn = Path(os.environ['API_DSN_FILE']).read_text()
    engine = create_async_engine(dsn)
    try:
        async with async_sessionmaker(engine)() as db:
            accepted = await publish_prepared_dispatch(
                db, lease, publish=lambda: publish_queue_envelope(lease.envelope)
            )
            assert accepted == lease.execution_id
    finally:
        await engine.dispose()

asyncio.run(main())
"""


async def test_killed_publisher_partial_canvas_replays_original_ids_and_completes_once(
    frame_world, test_engine, monkeypatch, tmp_path, record_property
):
    _verify_postgres(test_engine.url)
    broker_target = _verify_redis()
    broker_url = settings.redis_url
    assert make_url(broker_url) == broker_target
    runtime = make_url(settings.database_url)
    relay_dsn = make_url(os.environ["AAP_MODULE_DATA_JOBS_DISPATCH_RELAY_URL"])
    publication_dsn = make_url(os.environ["AAP_MODULE_DATA_JOBS_PUBLICATION_URL"])
    expected = (test_engine.url.host, test_engine.url.port, test_engine.url.database)
    assert (runtime.host, runtime.port, runtime.database) == expected
    assert (relay_dsn.host, relay_dsn.port, relay_dsn.database) == expected
    assert (
        publication_dsn.host,
        publication_dsn.port,
        publication_dsn.database,
    ) == expected
    assert (
        len(
            {
                runtime.username,
                relay_dsn.username,
                publication_dsn.username,
                test_engine.url.username,
            }
        )
        == 4
    )
    world = frame_world
    _install_video_gateway(monkeypatch, world)
    monkeypatch.setattr(settings, "frame_preannotate_chunk_size", 1)

    async def predict(_tasks, _context):
        return [
            SimpleNamespace(
                result=[
                    {
                        "type": "rectanglelabels",
                        "value": {
                            "x": 10,
                            "y": 20,
                            "width": 30,
                            "height": 40,
                            "rectanglelabels": ["car"],
                        },
                    }
                ],
                score=0.9,
                model_version="protocol-fixture",
                inference_time_ms=12,
                meta={"total_tokens": 7},
            )
        ]

    _install_frame_execution_fake(
        monkeypatch, predict, actual_instance_id=world.backend_id
    )
    dispatcher = frame_preannotate.JobsFramePreannotationDispatcher()
    async with world.maker() as acceptance:
        prepared = await dispatcher.prepare(
            FramePreannotationDispatchRequest(
                project_id=str(world.project_id),
                ml_backend_id=str(world.backend_id),
                stage0={},
                job_id=str(world.job_id),
                root_execution_id="crash-frame-root",
                actor_id=str(world.actor_id),
                video_task_plans=(
                    VideoFrameTaskPlan(
                        task_id=str(world.task_id),
                        input_id=str(world.input_id),
                        input_job_id=str(world.job_id),
                        plan_digest=world.plan.plan_digest,
                        selected_count=world.plan.selected_count,
                    ),
                ),
                accepted_video_manifest_id=str(world.job_id),
                accepted_video_selection_digest=world.selection_digest,
                accepted_video_sealed_digest=world.sealed_digest,
            ),
            db=acceptance,
        )
        job = await acceptance.get(AsyncJob, world.job_id)
        job.result = {
            **job.result,
            "frame_dispatch": {
                "intent_id": str(prepared.lease.intent_id),
                "segment_ids": list(prepared.segment_ids),
                "total_frames": prepared.total_frames,
            },
        }
        await acceptance.commit()
    lease = prepared.lease
    lease_file = jobs_secret(tmp_path, "accepted-lease.json", lease.model_dump_json())
    api_file = jobs_secret(
        tmp_path, "api-dsn", runtime.render_as_string(hide_password=False)
    )
    publication_url = publication_dsn.render_as_string(hide_password=False)
    publication_file = jobs_secret(tmp_path, "publisher-dsn", publication_url)
    with redis.from_url(broker_url) as broker:
        assert broker.llen("ml") == 0
        assert (
            broker.get(frame_preannotate._progress_total_key(str(world.job_id))) is None
        )
    async with _SecondPublicationGate(broker_target) as gate:
        proxied = broker_target.set(host="127.0.0.1", port=gate.port).render_as_string(
            hide_password=False
        )
        async with jobs_child(
            ["-c", _PUBLISHER],
            environment={
                **jobs_broker_env(proxied),
                "LEASE_FILE": lease_file,
                "API_DSN_FILE": api_file,
                "JOBS_PUBLICATION_DATABASE_URL_FILE": publication_file,
            },
            directory=tmp_path,
            name="publisher",
        ) as publisher:
            await asyncio.wait_for(gate.blocked.wait(), 20)
            with redis.from_url(broker_url) as broker:
                queued = broker.lrange("ml", 0, -1)
                assert len(queued) == 1
                assert (
                    json.loads(queued[0])["headers"]["id"]
                    == (lease.envelope["tasks"][0]["execution_id"])
                )
            publisher.kill()
            assert await asyncio.to_thread(publisher.wait, timeout=5) == -signal.SIGKILL
            crashed = {
                "publisher_pid": publisher.pid,
                "publisher_exit": publisher.returncode,
            }
    async with await JobsDispatchPostgres.connect(
        relay_dsn.render_as_string(hide_password=False)
    ) as observer:
        before = await observer.client.status(lease.intent_id)
        assert before.state == "leased" and before.generation == 1
        async with jobs_recovery_worker(
            directory=tmp_path / "recovery",
            dispatch_url=relay_dsn.render_as_string(hide_password=False),
            publication_url=publication_url,
        ) as (recovery, beat):
            async with asyncio.timeout(75):
                while True:
                    assert recovery.poll() is None and beat.poll() is None
                    submitted = await observer.client.status(lease.intent_id)
                    if submitted.state == "submitted":
                        break
                    await asyncio.sleep(0.1)
    assert submitted.generation == 2
    async with test_engine.connect() as db:
        attempts = (
            await db.execute(
                text(
                    "SELECT generation,state FROM yoao_jobs_v1.dispatch_publication_attempts "
                    "WHERE intent_id=:id ORDER BY generation"
                ),
                {"id": lease.intent_id},
            )
        ).all()
    assert attempts == [(1, "entered"), (2, "accepted")]
    with redis.from_url(broker_url) as broker:
        envelopes = [json.loads(value) for value in broker.lrange("ml", 0, -1)]
        wanted = [task["execution_id"] for task in lease.envelope["tasks"]]
        observed = [message["headers"]["id"] for message in envelopes]
        assert sorted(observed) == sorted([wanted[0], *wanted])
    finished = __import__("threading").Event()
    seen = []

    def completion(task=None, **_kwargs):
        if (
            task.name == frame_preannotate.predict_video_segment.name
            and task.request.kwargs.get("job_id") == str(world.job_id)
        ):
            seen.append(task.request.id)
            if len(seen) == 4:
                finished.set()

    signals.task_postrun.connect(completion, weak=False)
    try:
        with start_worker(
            celery_app,
            pool="solo",
            concurrency=1,
            queues=("ml", "default"),
            perform_ping_check=False,
            shutdown_timeout=20,
        ):
            assert await asyncio.to_thread(finished.wait, 60)
            async with asyncio.timeout(20):
                while True:
                    async with world.maker() as db:
                        job = await db.get(AsyncJob, world.job_id)
                        if job.status == "completed":
                            rows = list(
                                (
                                    await db.scalars(
                                        select(Prediction).where(
                                            Prediction.task_id == world.task_id
                                        )
                                    )
                                ).all()
                            )
                            assert job.result["frames_done"] == 3
                            assert job.result["failed"] == 0
                            assert len(rows) == 3
                            assert {
                                row.result[0]["geometry"]["frame_index"] for row in rows
                            } == {0, 1, 2}
                            break
                    await asyncio.sleep(0.05)
    finally:
        signals.task_postrun.disconnect(completion)
    record_property(
        "jobs_partial_canvas_runtime",
        {
            "crashed": crashed,
            "intent_id": str(lease.intent_id),
            "frozen_envelope_sha256": hashlib.sha256(
                json.dumps(lease.envelope, sort_keys=True).encode()
            ).hexdigest(),
            "original_child_ids": wanted,
            "broker_deliveries": observed,
            "physical_attempts": [list(attempt) for attempt in attempts],
            "consumer_deliveries": seen,
            "recovered_generation": submitted.generation,
            "canonical_saved_frames": 3,
            "model_and_byte_readers": "fixtures; not GPU qualification",
        },
    )
