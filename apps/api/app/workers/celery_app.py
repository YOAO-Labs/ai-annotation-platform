from celery import Celery
from yoao_jobs.queue_contract import load_queue_configuration

from app.assembly.builders import build_beat_schedule, collect_celery_includes
from app.config import settings
from yoao_assets_media_runtime.additional_original import (
    load_additional_original_config,
)

load_additional_original_config()

celery_app = Celery(
    "annotation_worker",
    broker=settings.effective_celery_broker,
    backend=settings.effective_celery_broker,
    include=collect_celery_includes(),
)


def _build_beat_schedule():
    return build_beat_schedule(settings)


celery_app.conf.update(
    **load_queue_configuration(),
    timezone="UTC",
    task_track_started=True,
    worker_max_memory_per_child=512_000,
    worker_direct=True,
    beat_schedule=_build_beat_schedule(),
)

from app.workers.heartbeat import HeartbeatStep  # noqa: E402

celery_app.steps["worker"].add(HeartbeatStep)
