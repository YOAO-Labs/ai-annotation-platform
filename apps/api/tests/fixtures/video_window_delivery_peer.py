"""Exercise the registered tracker task's broker policy without model or DB I/O."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

from celery import current_task
from celery.signals import worker_ready

from app.workers.celery_app import celery_app
from app.workers import video_tracker


async def _delivery_probe(job_id, celery_task_id):
    directory = Path(os.environ["AAP_TEST_VIDEO_DELIVERY_DIRECTORY"])
    assert job_id == os.environ["AAP_TEST_VIDEO_DELIVERY_JOB"]
    request = current_task.request
    assert celery_task_id == request.id
    event = {
        "pid": os.getpid(),
        "task_id": request.id,
        "retries": request.retries,
        "redelivered": bool((request.delivery_info or {}).get("redelivered")),
    }
    descriptor = os.open(
        directory / "deliveries.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
    )
    try:
        os.write(descriptor, (json.dumps(event) + "\n").encode())
    finally:
        os.close(descriptor)
    try:
        first = os.open(
            directory / "first.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        (directory / "completed.json").write_text(json.dumps(event))
        return
    try:
        os.write(first, json.dumps(event).encode())
    finally:
        os.close(first)
    while True:
        await asyncio.sleep(0.1)


def _ready(**_kwargs):
    directory = Path(os.environ["AAP_TEST_VIDEO_DELIVERY_DIRECTORY"])
    source = Path(video_tracker.__file__)
    (directory / "ready.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "worker_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    # Keep the actual registered task/decorator and Celery parent/prefork child.
    # Only its async business entry is replaced; execution recovery has separate
    # real-owner SIGKILL tests and is not asserted by this delivery-only probe.
    video_tracker._run_video_tracker_job = _delivery_probe
    celery_app.conf.include = []
    worker_ready.connect(_ready, weak=False)
    celery_app.worker_main(sys.argv[1:])
