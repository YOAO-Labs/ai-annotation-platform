from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import Response

from aap_assets_images.contract import (
    AcceptedImageWork,
    ImageBackfillSpec,
    ImageWorkReference,
)
from app.api.v1 import files as files_api
from app.application.legacy_file_upload import LegacyFileUploadWorkflow


def _accepted(request, *, job_id: uuid.UUID) -> AcceptedImageWork:
    accepted_before = datetime.now(UTC)
    spec = request.spec.model_copy(
        update={
            "selection": request.spec.selection.model_copy(
                update={"accepted_before": accepted_before}
            )
        }
    )
    return AcceptedImageWork(
        operation_id=request.operation_id,
        source=None,
        work=ImageWorkReference(
            job_id=job_id,
            generation=1,
            owner=None,
            source_generation=0,
            kind="image_backfill",
            status="pending",
        ),
        spec=spec,
    )


@pytest.mark.asyncio
async def test_project_task_backfill_checks_manager_and_accepts_project_job(
    monkeypatch,
):
    actor_id, project_id, operation_id, job_id = (uuid.uuid4() for _ in range(4))
    authorization = []
    requests = []

    class ImageOwner:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call(self, operation, request):
            assert operation == "schedule_image_media"
            requests.append(request)
            return _accepted(request, job_id=job_id)

    workflow = LegacyFileUploadWorkflow(object(), storage=object())

    async def manager_access(actor, project, *, lock):
        authorization.append((actor, project, lock))

    monkeypatch.setattr(workflow, "_manager_access", manager_access)
    monkeypatch.setattr(
        "app.application.legacy_file_upload.image_gateway", lambda: ImageOwner()
    )

    accepted = await workflow.accept_thumbnail_backfill(
        actor_id=actor_id,
        project_id=project_id,
        operation_id=operation_id,
    )

    assert authorization == [(actor_id, project_id, True)]
    assert len(requests) == 1
    request = requests[0]
    assert request.operation_id == operation_id
    assert request.owner_kind is None and request.owner_id is None
    assert isinstance(request.spec, ImageBackfillSpec)
    assert request.spec.selection.owner_kind == "task"
    assert request.spec.selection.project_id == project_id
    assert request.spec.selection.purpose == "thumbnail"
    assert request.spec.selection.only_missing is True
    assert accepted.work.job_id == job_id
    assert accepted.spec.selection.accepted_before is not None


@pytest.mark.asyncio
async def test_project_task_backfill_route_returns_typed_idempotent_receipt(
    monkeypatch,
):
    actor_id, project_id, operation_id, job_id = (uuid.uuid4() for _ in range(4))
    request_spec = ImageBackfillSpec(
        selection={
            "owner_kind": "task",
            "project_id": project_id,
            "purpose": "thumbnail",
            "accepted_before": datetime.now(UTC),
        }
    )
    accepted = AcceptedImageWork(
        operation_id=operation_id,
        source=None,
        work=ImageWorkReference(
            job_id=job_id,
            generation=1,
            owner=None,
            source_generation=0,
            kind="image_backfill",
            status="pending",
        ),
        spec=request_spec,
    )

    class Workflow:
        def __init__(self, *_args, **_kwargs):
            pass

        async def accept_thumbnail_backfill(self, **request):
            assert request == {
                "actor_id": actor_id,
                "project_id": project_id,
                "operation_id": operation_id,
            }
            return accepted

    monkeypatch.setattr(files_api, "LegacyFileUploadWorkflow", Workflow)
    response = Response()
    result = await files_api.backfill_project_thumbnails(
        project_id,
        response,
        db=object(),
        current_user=SimpleNamespace(id=actor_id),
        idempotency_key=operation_id,
    )

    assert result.project_id == project_id
    assert result.operation_id == operation_id
    assert result.job_id == job_id
    assert result.status == "pending"
    assert result.accepted_before == request_spec.selection.accepted_before
    assert response.headers["Idempotency-Key"] == str(operation_id)
    route = next(
        route
        for route in files_api.router.routes
        if route.endpoint is files_api.backfill_project_thumbnails
    )
    assert route.status_code == 202
