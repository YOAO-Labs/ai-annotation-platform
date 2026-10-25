"""The actual tracker task is redelivered after an owned prefork child is killed."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

from celery import Celery
import pytest
from sqlalchemy.engine import make_url

from tests.test_identity_sessions_live import _verify_redis

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified owned module-data broker",
    ),
]

ROOT = Path(__file__).resolve().parents[3]


async def _wait_json(path, worker, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        assert worker.poll() is None, "owned tracker delivery worker exited"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                pass
        assert time.monotonic() < deadline, (
            f"tracker delivery did not reach {path.name}"
        )
        await asyncio.sleep(0.1)


async def test_tracker_task_broker_redelivers_after_prefork_child_sigkill(tmp_path):
    verified = _verify_redis()
    queue = "video_delivery_" + uuid4().hex
    job_id = str(uuid4())
    broker = os.environ["CELERY_BROKER_URL"]
    target = make_url(broker)
    assert (target.host, target.port) == (verified.host, verified.port)
    sender = Celery("video-delivery-acceptance", broker=broker, backend=broker)
    worker = delivery = None
    log = (tmp_path / "worker.log").open("wb")
    environment = {
        **os.environ,
        "AAP_TEST_VIDEO_DELIVERY_DIRECTORY": str(tmp_path),
        "AAP_TEST_VIDEO_DELIVERY_JOB": job_id,
        "CELERY_BROKER_URL": broker,
        "CELERY_BROKER_READ_URL": broker,
        "CELERY_BROKER_WRITE_URL": broker,
        "CELERY_RESULT_BACKEND": broker,
    }
    try:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.fixtures.video_window_delivery_peer",
                "worker",
                "--pool=prefork",
                "--concurrency=1",
                "--prefetch-multiplier=1",
                "--queues=" + queue,
                "--hostname=" + queue + "@owned",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "--loglevel=WARNING",
            ],
            cwd=ROOT / "apps/api",
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        ready = await _wait_json(tmp_path / "ready.json", worker)
        assert ready["pid"] == worker.pid
        assert (
            ready["worker_sha256"]
            == hashlib.sha256(
                (ROOT / "apps/api/app/workers/video_tracker.py").read_bytes()
            ).hexdigest()
        )
        delivery = await asyncio.to_thread(
            sender.send_task,
            "app.workers.video_tracker.run_video_tracker_job",
            args=[job_id],
            task_id=job_id,
            queue=queue,
        )
        first = await _wait_json(tmp_path / "first.json", worker)
        assert first["pid"] != worker.pid
        assert first["task_id"] == job_id and first["retries"] == 0
        os.kill(first["pid"], signal.SIGKILL)
        completed = await _wait_json(tmp_path / "completed.json", worker)
        assert completed["pid"] not in {first["pid"], worker.pid}
        assert completed["task_id"] == job_id
        assert completed["retries"] == 0 and completed["redelivered"] is True
        await asyncio.to_thread(delivery.get, timeout=15)
        events = [
            json.loads(line)
            for line in (tmp_path / "deliveries.jsonl").read_text().splitlines()
        ]
        assert events == [first, completed]
    finally:
        if worker is not None and worker.poll() is None:
            os.killpg(worker.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(worker.wait, timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, signal.SIGKILL)
                await asyncio.to_thread(worker.wait, timeout=5)
        log.close()
        _verify_redis()
        if delivery is not None:
            await asyncio.to_thread(delivery.forget)
        with sender.connection_for_write() as connection:
            connection.default_channel.queue_delete(queue)
        sender.close()
