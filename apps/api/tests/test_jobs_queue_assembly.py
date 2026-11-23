from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from yoao_jobs.queue_contract import (
    JOBS_QUEUE_PROTOCOL,
    JobsQueueContractError,
    load_queue_configuration,
)
from app.config import Settings
from app import deps
from app.services import jobs_queue
from app.workers import celery_app as worker_assembly
from app.workers.celery_app import celery_app
from app.workers.job_execution_control import CeleryJobExecutionControl
from app.workers.jobs_dispatch import recover_job_dispatches


_TASK_NAME = "app.workers.cross_frame_job.run_cross_frame_job"
_EXECUTION_ID = "predeclared-execution-id"
_JOB_ID = "8ccbf8e4-cce2-4eec-a15d-9f58222987a1"


def test_jobs_recovery_is_registered_and_scheduled_on_only_its_queue():
    assert "app.workers.jobs_dispatch" in celery_app.conf.include
    entry = celery_app.conf.beat_schedule["recover-job-dispatches"]
    assert entry["task"] == recover_job_dispatches.name
    assert entry["schedule"].total_seconds() == 5
    assert entry["options"] == {"queue": "jobs.recovery", "expires": 5}
    assert recover_job_dispatches.queue == "jobs.recovery"
    assert {
        "app.workers.identity_mail_delivery",
        "app.workers.identity_mail_v2_delivery",
    }.isdisjoint(celery_app.conf.include)


def test_api_celery_assembly_uses_the_jobs_route_resource():
    config = load_queue_configuration()
    assert celery_app.conf.task_default_queue == config["task_default_queue"]
    assert celery_app.conf.task_serializer == config["task_serializer"]
    assert celery_app.conf.result_serializer == config["result_serializer"]
    assert celery_app.conf.accept_content == config["accept_content"]
    assert celery_app.conf.task_routes == config["task_routes"]
    assert celery_app.conf.task_routes[_TASK_NAME] == {"queue": "default"}
    assert celery_app.conf.timezone == "UTC"
    assert celery_app.conf.task_track_started is True
    assert celery_app.conf.worker_max_memory_per_child == 512_000


@pytest.mark.parametrize(
    ("selectors", "suppressed"),
    [
        (
            {"assets_video_executor": "split"},
            {"reconcile-accepted-video-media", "execute-video-media-recovery"},
        ),
        (
            {"assets_image_executor": "split"},
            {"reconcile-image-pyramids"},
        ),
        (
            {"notification_publication_url": "http://notifications-publisher:8181"},
            {"relay-notification-deliveries"},
        ),
    ],
)
def test_beat_only_suppresses_the_separately_hosted_tasks(
    selectors, suppressed, monkeypatch
):
    values = {
        "assets_video_executor": "celery",
        "assets_image_executor": "celery",
        "notification_publication_url": "",
    }
    monkeypatch.setattr(worker_assembly, "settings", Settings(_env_file=None, **values))
    embedded = worker_assembly._build_beat_schedule()
    monkeypatch.setattr(
        worker_assembly, "settings", Settings(_env_file=None, **(values | selectors))
    )
    selected = worker_assembly._build_beat_schedule()
    assert set(embedded) - set(selected) == suppressed
    assert selected == {
        key: value for key, value in embedded.items() if key not in suppressed
    }
    assert "cleanup-video-frame-assets" in selected
    assert "purge-unreferenced-raster-masks" in selected
    assert "recover-job-dispatches" in selected


def test_dispatch_uses_the_platform_broker_with_a_stable_execution_id(monkeypatch):
    calls = []

    def send_task(task_name, **kwargs):
        calls.append((task_name, kwargs))

    monkeypatch.setattr(celery_app, "send_task", send_task)

    result = asyncio.run(
        jobs_queue.dispatch_job(
            task_name=_TASK_NAME,
            execution_id=_EXECUTION_ID,
            args=[_JOB_ID],
            kwargs=None,
            options=None,
        )
    )
    assert result == _EXECUTION_ID
    assert calls == [
        (
            _TASK_NAME,
            {
                "args": [_JOB_ID],
                "kwargs": None,
                "task_id": _EXECUTION_ID,
            },
        )
    ]


def test_fastapi_revoke_uses_platform_broker_without_termination(monkeypatch):
    revoke = Mock()
    monkeypatch.setattr(celery_app.control, "revoke", revoke)
    control = deps.get_job_execution_control()
    assert isinstance(control, CeleryJobExecutionControl)
    control.request_cancel(_EXECUTION_ID)
    revoke.assert_called_once_with(_EXECUTION_ID, terminate=False)


def test_saved_dispatch_rejects_unqualified_options_before_publish(monkeypatch):
    published = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda *_args, **_kwargs: published.append(True)
    )

    with pytest.raises(JobsQueueContractError):
        asyncio.run(
            jobs_queue.publish_queue_envelope(
                {
                    "schema": JOBS_QUEUE_PROTOCOL,
                    "operation": "dispatch",
                    "task_name": _TASK_NAME,
                    "execution_id": _EXECUTION_ID,
                    "args": [_JOB_ID],
                    "kwargs": {},
                    "options": {"expires": 1},
                }
            )
        )
    assert not published
