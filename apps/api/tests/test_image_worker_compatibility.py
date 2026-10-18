"""Pre-cutover Celery messages cannot admit image work from owner IDs."""

from __future__ import annotations

from types import SimpleNamespace
import uuid

from app.workers.image_pyramid import generate_image_pyramid
from app.workers.media import (
    backfill_media,
    backfill_tasks,
    generate_task_thumbnail,
    generate_thumbnail,
)
from app.workers.media_dispatch import CeleryMediaJobDispatcher


def test_legacy_dispatchers_and_task_messages_only_wake_accepted_pool(monkeypatch):
    wakeups = []

    def wake(job_id):
        wakeups.append(job_id)
        return SimpleNamespace(id=f"wake-{len(wakeups)}")

    monkeypatch.setattr("app.workers.image_pyramid.execute_image_media.delay", wake)
    dispatcher = CeleryMediaJobDispatcher()
    item_id = uuid.uuid4()
    task_id = uuid.uuid4()
    project_id = uuid.uuid4()

    dispatcher.dispatch_thumbnail(item_id)
    dispatcher.dispatch_task_thumbnail(task_id)
    assert dispatcher.dispatch_task_thumbnail_backfill(project_id) == "wake-3"

    generate_thumbnail.run(str(item_id))
    generate_task_thumbnail.run(str(task_id))
    backfill_tasks.run(str(project_id))
    backfill_media.run("00000000-0000-0000-0000-000000000001", schedule_video=False)

    assert wakeups == [None] * 7


def test_obsolete_owner_id_delivery_is_acknowledged_without_execution(monkeypatch):
    executed = []
    monkeypatch.setattr(
        "app.services.assets_images.run_image_job",
        lambda job_id=None: executed.append(job_id),
    )

    generate_image_pyramid.run("dataset_item", str(uuid.uuid4()), True)

    assert executed == []
