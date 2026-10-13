"""The real project-access guard must reject before any inference runs.

The test stubs only the database/resource lookup and the resolved access
context; the production ``require_task_annotation_write_strict`` dependency and
its ``_task_write_allowed`` predicate decide whether the request is admitted.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app import deps
from app.api.v1.tasks import _shared as tasks_shared
from app.api.v1.tasks import annotations
from app.db.models.task import Task
from app.db.models.user import User
from app.services.project_access import (
    ACCESS_KIND_MEMBER,
    MANAGER_CAPABILITIES,
    ProjectAccess,
    project_role_capabilities,
)


def _access(user, project, platform_role, project_role, *, manager):
    """The resolved access context that the stubbed resolver returns."""

    capabilities = (
        MANAGER_CAPABILITIES
        if manager
        else project_role_capabilities(project_role or "")
    )
    return ProjectAccess(
        user_id=user.id,
        project_id=project.id,
        platform_role=platform_role,
        project_role=None if manager else project_role,
        membership_id=None if manager else uuid.uuid4(),
        membership_version=None if manager else 1,
        access_kind="manager" if manager else ACCESS_KIND_MEMBER,
        capabilities=frozenset(capabilities),
        is_manager=manager,
    )


class _FakeSession:
    """Minimal session for the stubbed lookup: only ``Project`` is fetched."""

    def __init__(self, project):
        self._project = project

    async def get(self, model, pk, **kwargs):
        return self._project


@pytest.mark.parametrize(
    ("platform_role", "project_role", "manager", "admitted"),
    [
        # A super administrator manages every project.
        ("super_admin", None, True, True),
        # A platform project administrator who is only an annotator member gets
        # that membership's capabilities, which include annotation write.
        ("project_admin", "annotator", False, True),
        # A literal employee with an annotator membership may run inference.
        ("employee", "annotator", False, True),
        # Reviewers and viewers have no annotation.write capability.
        ("employee", "reviewer", False, False),
        ("viewer", "viewer", False, False),
    ],
)
@pytest.mark.asyncio
async def test_secondary_inference_capability_gate_precedes_inference(
    monkeypatch, platform_role, project_role, manager, admitted
):
    project = SimpleNamespace(id=uuid.uuid4())
    user = User(id=uuid.uuid4(), role=platform_role, is_active=True)
    access = _access(user, project, platform_role, project_role, manager=manager)

    app = FastAPI()
    app.include_router(annotations.router, prefix="/tasks")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.get_db] = lambda: _FakeSession(project)
    app.dependency_overrides[deps.get_gpu_shadow_session_factory] = lambda: object()
    app.dependency_overrides[deps.get_gpu_dispatch_context_factory] = lambda: object()

    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id="T-GATE",
        file_name="gate.png",
        file_path="/gate.png",
        file_type="image",
        status="pending",
    )

    async def _load_task(db, task_id):
        return task

    async def _resolve(db, *, user, project, lock_membership=False):
        return access

    # Stub the dependency's DB lookups so the real predicate runs on the
    # resolved context; separate the endpoint body's lookup so a successful
    # gate is still observable as a 404 without running inference.
    monkeypatch.setattr(tasks_shared, "_load_task_or_404", _load_task)
    monkeypatch.setattr(tasks_shared, "resolve_project_access", _resolve)
    load_task = AsyncMock(side_effect=HTTPException(404, "task not found"))
    infer = AsyncMock()
    monkeypatch.setattr(annotations, "_load_task_or_404", load_task)
    monkeypatch.setattr(annotations, "run_secondary_inference", infer)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/tasks/{task.id}/annotations/{uuid.uuid4()}/secondary-inference",
            json={"ml_backend_id": str(uuid.uuid4()), "write_target": "geometry"},
        )

    assert response.status_code == (404 if admitted else 403), response.text
    assert load_task.await_count == int(admitted)
    infer.assert_not_awaited()
