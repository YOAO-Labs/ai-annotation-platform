import httpx
import pytest
from fastapi import FastAPI

from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
)


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("task_busy", True),
        ("prediction_task_scope_changed", False),
        ("prediction_source_changed", False),
    ],
)
def test_prediction_write_retries_contention_but_rejects_stale_scope(code, retryable):
    failure = ApplyPredictionResultError(
        code=code,
        status_code=409,
        detail={"reason": code},
    )

    assert failure.retryable is retryable


@pytest.mark.asyncio
async def test_prediction_write_http_handler_preserves_status_and_detail(app_module):
    probe = FastAPI(exception_handlers=app_module.exception_handlers)

    @probe.get("/")
    async def reject_write():
        raise ApplyPredictionResultError(
            code="task_busy",
            status_code=409,
            detail={"reason": "task_busy"},
            headers={"Retry-After": "1"},
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=probe), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "task_busy"}}
    assert response.headers["Retry-After"] == "1"
