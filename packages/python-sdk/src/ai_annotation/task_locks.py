"""Current-actor task edit leases with explicit acquire, heartbeat, and release."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.task_workflow_models import TaskLock, TaskLockHeartbeat

IdLike = str | UUID


class TaskLocks:
    def __init__(self, http: HttpTransport):
        self._http = http

    def acquire(self, task_id: IdLike) -> TaskLock:
        resp = self._http.request("POST", f"/tasks/{task_id}/lock")
        return TaskLock.model_validate(resp.json())

    def heartbeat(self, task_id: IdLike) -> TaskLockHeartbeat:
        resp = self._http.request("POST", f"/tasks/{task_id}/lock/heartbeat")
        return TaskLockHeartbeat.model_validate(resp.json())

    def release(self, task_id: IdLike) -> None:
        self._http.request("DELETE", f"/tasks/{task_id}/lock")
