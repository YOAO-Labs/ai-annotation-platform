"""Celery transport for cooperative execution cancellation requests."""

from app.workers.celery_app import celery_app


class CeleryJobExecutionControl:
    def request_cancel(self, execution_id: str) -> None:
        celery_app.control.revoke(execution_id, terminate=False)
