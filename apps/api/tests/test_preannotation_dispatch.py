"""Pure tests for the application batch-prediction boundary and Celery adapter."""

from __future__ import annotations


import pytest

from app.application.preannotation_contract import BatchPredictionRequest
from app.application import preannotation_execution


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_unit", ["task", "frame"])
async def test_application_command_disposes_engine_when_workflow_exits(
    monkeypatch,
    execution_unit,
):
    import sqlalchemy.ext.asyncio as sa_async

    disposed = []

    class FakeEngine:
        async def dispose(self):
            disposed.append(True)

    class FailingSessionFactory:
        def __call__(self):
            raise RuntimeError("simulated early workflow exit")

    async def fail_after_command_starts(*_args, **_kwargs):
        raise RuntimeError("simulated early workflow exit")

    monkeypatch.setattr(sa_async, "create_async_engine", lambda *_a, **_k: FakeEngine())
    monkeypatch.setattr(
        sa_async, "async_sessionmaker", lambda *_a, **_k: FailingSessionFactory()
    )
    monkeypatch.setattr(
        preannotation_execution, "_execute_preannotation", fail_after_command_starts
    )

    expected = (
        pytest.raises(ValueError, match="requires a retained video manifest")
        if execution_unit == "frame"
        else pytest.raises(RuntimeError, match="simulated early workflow exit")
    )
    with expected:
        await preannotation_execution.run_preannotation(
            BatchPredictionRequest(
                project_id="p", ml_backend_id="b", execution_unit=execution_unit
            ),
            execution_id="celery-id",
        )

    assert disposed == [True]
