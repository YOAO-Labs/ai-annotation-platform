from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from aap_inference import PipelineValidationError
from app.api.v1 import project_pipelines


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.asyncio
async def test_saved_pipeline_error_is_mapped_at_http_route(monkeypatch, operation):
    async def reject(*_args, **_kwargs):
        raise PipelineValidationError("preannotate_pipeline 校验失败: exact detail")

    if operation == "create":
        monkeypatch.setattr(project_pipelines, "create_pipeline_command", reject)
        request = project_pipelines.create_pipeline(data=None, db=None, user=None)
    else:
        monkeypatch.setattr(project_pipelines, "update_pipeline_command", reject)
        request = project_pipelines.update_pipeline(
            pipeline_id=uuid.uuid4(), data=None, db=None, user=None
        )

    with pytest.raises(HTTPException) as error:
        await request
    assert error.value.status_code == 422
    assert error.value.detail == "preannotate_pipeline 校验失败: exact detail"
