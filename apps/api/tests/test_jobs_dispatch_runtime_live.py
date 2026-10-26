"""Owned CLI crash recovery of a partially published real frame canvas.

Model responses and retained-byte readers are fixtures. PostgreSQL, Redis, the
independently installed queue/relay CLIs, process deaths, and frame writes are real.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
from types import SimpleNamespace

from celery import signals
from celery.contrib.testing.worker import start_worker
import httpx
import pytest
import redis
from sqlalchemy import select
from sqlalchemy.engine import make_url

from aap_jobs.dispatch_postgres import JobsDispatchPostgres
from app.application.preannotation_contract import (
    FramePreannotationDispatchRequest,
    VideoFrameTaskPlan,
)
from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.prediction import Prediction
from app.workers import frame_preannotate
from app.workers.celery_app import celery_app
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
        reason="requires verifier-owned services and the independently installed Jobs CLIs",
    ),
]


@asynccontextmanager
async def _child(command, *, environment, directory, name):
    log = (directory / f"{name}.log").open("xb")
    process = subprocess.Popen(
        command,
        cwd=directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
        log.close()


def _secret(directory, name, value):
    path = directory / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)
    return str(path)


async def _wait_ready(process, origin):
    async with asyncio.timeout(20):
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            while process.poll() is None:
                try:
                    response = await client.get(origin + "/health/ready")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
    raise AssertionError("owned queue CLI did not become ready")


@asynccontextmanager
async def _queue(binary, *, directory, environment, broker, token_file, name):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    broker_file = _secret(directory, name + "-broker", broker)
    env = {
        **environment,
        "JOBS_QUEUE_BROKER_URL_FILE": broker_file,
        "JOBS_QUEUE_TOKEN_FILE": token_file,
        "JOBS_QUEUE_HOST": "127.0.0.1",
        "JOBS_QUEUE_PORT": str(port),
    }
    async with _child(
        [str(binary)], environment=env, directory=directory, name=name
    ) as process:
        origin = f"http://127.0.0.1:{port}"
        await _wait_ready(process, origin)
        yield process, origin


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
import asyncio, json, os
from pathlib import Path
from aap_jobs.dispatch_contract import DispatchLease
from aap_jobs.dispatch_postgres import JobsDispatchPostgres
from aap_jobs.queue_client import JobsQueueClient
async def main():
    lease = DispatchLease.model_validate_json(Path(os.environ['LEASE_FILE']).read_text())
    token = Path(os.environ['TOKEN_FILE']).read_text()
    client = JobsQueueClient(os.environ['QUEUE_URL'], token)
    accepted = await client.publish_envelope(lease.envelope)
    assert accepted == lease.execution_id
    dsn = Path(os.environ['API_DSN_FILE']).read_text()
    async with await JobsDispatchPostgres.connect(dsn) as provider:
        acknowledged = await provider.client.finish(lease, accepted=True)
        assert acknowledged.state == 'submitted'
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
    expected = (test_engine.url.host, test_engine.url.port, test_engine.url.database)
    assert (runtime.host, runtime.port, runtime.database) == expected
    assert (relay_dsn.host, relay_dsn.port, relay_dsn.database) == expected
    assert runtime.username != relay_dsn.username != test_engine.url.username
    binary = Path(os.environ["AAP_JOBS_DISPATCH_INSTALLED_BIN"])
    assert binary.is_absolute()
    assert all(
        (binary / name).is_file()
        for name in ("python", "aap-jobs-queue", "aap-jobs-dispatch-relay")
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
    token = secrets.token_urlsafe(32)
    token_file = _secret(tmp_path, "queue-token", token)
    lease_file = _secret(tmp_path, "accepted-lease.json", lease.model_dump_json())
    api_file = _secret(
        tmp_path,
        "api-dsn",
        runtime.set(drivername="postgresql").render_as_string(hide_password=False),
    )
    relay_file = _secret(
        tmp_path, "relay-dsn", relay_dsn.render_as_string(hide_password=False)
    )
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1"}
    with redis.from_url(broker_url) as broker:
        assert broker.llen("ml") == 0
        assert (
            broker.get(frame_preannotate._progress_total_key(str(world.job_id))) is None
        )
    async with _SecondPublicationGate(broker_target) as gate:
        proxied = broker_target.set(host="127.0.0.1", port=gate.port).render_as_string(
            hide_password=False
        )
        async with _queue(
            binary / "aap-jobs-queue",
            directory=tmp_path,
            environment=environment,
            broker=proxied,
            token_file=token_file,
            name="first-queue",
        ) as (queue, origin):
            publisher_env = {
                **environment,
                "LEASE_FILE": lease_file,
                "TOKEN_FILE": token_file,
                "API_DSN_FILE": api_file,
                "QUEUE_URL": origin,
            }
            async with _child(
                [str(binary / "python"), "-I", "-c", _PUBLISHER],
                environment=publisher_env,
                directory=tmp_path,
                name="publisher",
            ) as publisher:
                await asyncio.wait_for(gate.blocked.wait(), 20)
                with redis.from_url(broker_url) as broker:
                    queued = broker.lrange("ml", 0, -1)
                    assert len(queued) == 1
                    assert (
                        json.loads(queued[0])["headers"]["id"]
                        == lease.envelope["tasks"][0]["execution_id"]
                    )
                publisher.kill()
                assert (
                    await asyncio.to_thread(publisher.wait, timeout=5)
                    == -signal.SIGKILL
                )
                queue.kill()
                assert await asyncio.to_thread(queue.wait, timeout=5) == -signal.SIGKILL
                crashed = {
                    "publisher_pid": publisher.pid,
                    "publisher_exit": publisher.returncode,
                    "queue_pid": queue.pid,
                    "queue_exit": queue.returncode,
                }
    async with await JobsDispatchPostgres.connect(
        relay_dsn.render_as_string(hide_password=False)
    ) as observer:
        before = await observer.client.status(lease.intent_id)
        assert before.state == "leased" and before.generation == 1
        async with _queue(
            binary / "aap-jobs-queue",
            directory=tmp_path,
            environment=environment,
            broker=broker_url,
            token_file=token_file,
            name="recovered-queue",
        ) as (_queue_process, origin):
            relay_env = {
                **environment,
                "JOBS_DISPATCH_RELAY_ENABLED": "true",
                "JOBS_DISPATCH_RELAY_DATABASE_URL_FILE": relay_file,
                "JOBS_QUEUE_URL": origin,
                "JOBS_QUEUE_TOKEN_FILE": token_file,
            }
            async with _child(
                [str(binary / "aap-jobs-dispatch-relay")],
                environment=relay_env,
                directory=tmp_path,
                name="relay",
            ) as relay:
                async with asyncio.timeout(75):
                    while True:
                        assert relay.poll() is None
                        submitted = await observer.client.status(lease.intent_id)
                        if submitted.state == "submitted":
                            break
                        await asyncio.sleep(0.1)
                assert submitted.generation == 2
                with redis.from_url(broker_url) as broker:
                    envelopes = [
                        json.loads(value) for value in broker.lrange("ml", 0, -1)
                    ]
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
                                                        Prediction.task_id
                                                        == world.task_id
                                                    )
                                                )
                                            ).all()
                                        )
                                        assert job.result["frames_done"] == 3
                                        assert job.result["failed"] == 0
                                        assert len(rows) == 3
                                        assert {
                                            row.result[0]["geometry"]["frame_index"]
                                            for row in rows
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
            "consumer_deliveries": seen,
            "recovered_generation": submitted.generation,
            "canonical_saved_frames": 3,
            "model_and_byte_readers": "fixtures; not GPU qualification",
        },
    )
