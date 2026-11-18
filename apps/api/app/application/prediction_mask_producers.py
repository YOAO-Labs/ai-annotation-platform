"""Bind image prediction Mask production to a durable execution generation."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import uuid4

from sqlalchemy import text

from app.application.preannotation_results import (
    PreannotationResultDeferred,
    _json_text,
)


_current = ContextVar("prediction_mask_producer", default=None)


def current_prediction_mask_producer():
    return _current.get()


@asynccontextmanager
async def prediction_mask_producer(execution, ordinal):
    """Keep an independent pending anchor until the prepared result is sealed."""
    producer_id = uuid4()
    async with execution.session_factory() as db:
        admitted = await db.scalar(
            text(
                "SELECT yoao_jobs_v1.begin_prediction_mask_producer(CAST(:scope AS jsonb), :ordinal, :id)"
            ),
            {
                "scope": _json_text(execution._values()),
                "ordinal": ordinal,
                "id": producer_id,
            },
        )
        if admitted is not True:
            raise PreannotationResultDeferred("lease_lost")
        await db.commit()
    token = _current.set(producer_id)
    try:
        yield
    except BaseException:
        # Unknown outcomes retain the durable anchor and exact owned objects.
        raise
    else:
        async with execution.session_factory() as db:
            settled = await db.scalar(
                text("SELECT yoao_jobs_v1.finish_prediction_mask_producer(:id)"),
                {"id": producer_id},
            )
            if settled is not True:
                raise PreannotationResultDeferred("storage_unavailable")
            await db.commit()
    finally:
        _current.reset(token)
