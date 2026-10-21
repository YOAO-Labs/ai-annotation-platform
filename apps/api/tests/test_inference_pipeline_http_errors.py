from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aap_inference.pipeline_contract import PipelineErrorDetails, PipelineFailure
from app.api.v1 import project_pipelines
from app.schemas.project_pipeline import ProjectPipelineCreate, ProjectPipelineUpdate
from app.services import inference_pipelines


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.asyncio
async def test_saved_pipeline_error_is_mapped_at_http_route(monkeypatch, operation):
    async def reject(_owner, _db, action, command):
        assert action == "execute"
        return PipelineFailure(
            schema="aap-inference-pipelines/v1",
            ok=False,
            operation_id=command.operation_id,
            operation=command.operation,
            actor_id=command.admitted_actor.id,
            target_id=command.target_id,
            error=PipelineErrorDetails(
                status_code=422,
                detail="preannotate_pipeline 校验失败: exact detail",
                headers=None,
                rollback=False,
            ),
            commit_state="caller_pending",
        )

    monkeypatch.setattr(inference_pipelines, "_invoke", reject)
    monkeypatch.setattr(
        inference_pipelines,
        "_select_owner",
        lambda: inference_pipelines._Owner("borrowed"),
    )
    actor = SimpleNamespace(id=uuid.uuid4(), role="project_admin")

    if operation == "create":
        request = project_pipelines.create_pipeline(
            data=ProjectPipelineCreate(
                name="Invalid stages", project_id=uuid.uuid4(), stages=[{}]
            ),
            db=None,
            user=actor,
        )
    else:
        request = project_pipelines.update_pipeline(
            pipeline_id=uuid.uuid4(),
            data=ProjectPipelineUpdate(stages=[{}]),
            db=None,
            user=actor,
        )

    with pytest.raises(HTTPException) as error:
        await request
    assert error.value.status_code == 422
    assert error.value.detail == "preannotate_pipeline 校验失败: exact detail"
