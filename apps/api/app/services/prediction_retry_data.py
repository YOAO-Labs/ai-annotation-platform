"""Bind retry admission and completion to their provider-owned transaction."""

from __future__ import annotations

from contextlib import asynccontextmanager, nullcontext

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.mask_gc import mask_fence_error_code


def _retry_rejection(error, allowed):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == "P0001":
            message = getattr(current, "message", None)
            if message in allowed:
                return message
        for name in ("orig", "__cause__", "__context__"):
            child = getattr(current, name, None)
            if isinstance(child, BaseException):
                pending.append(child)
    return None


class SqlAlchemyPredictionRetryData:
    """Never commit: admission/final receipts belong to the caller transaction."""

    def __init__(self, db, *, writer=False):
        self.db = db
        self.writer = writer

    async def call(self, operation, request):
        from typing import get_args
        from aap_preannotation_results import retry_contract as c
        from aap_preannotation_results_runtime.retry_database import (
            prepare_native_call,
            validate_native_result,
        )

        statement, arguments = prepare_native_call(
            operation, request, writer=self.writer
        )
        if len(arguments) != 1:
            raise c.PredictionRetryFailure("invalid_request")
        try:
            with getattr(self.db, "no_autoflush", nullcontext()):
                raw = (
                    await self.db.execute(
                        text(statement.replace("$1::jsonb", "CAST(:payload AS jsonb)")),
                        {"payload": arguments[0]},
                    )
                ).scalar_one()
                return validate_native_result(operation, request, raw)
        except c.PredictionRetryFailure:
            raise
        except SQLAlchemyError as error:
            raise c.PredictionRetryFailure(
                mask_fence_error_code(error)
                or _retry_rejection(error, frozenset(get_args(c.ErrorCode)))
                or "storage_unavailable"
            ) from None


class IndependentPredictionRetryData:
    """Commit one read/publication checkpoint without borrowing business locks."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def call(self, operation, request):
        from aap_preannotation_results.retry_contract import PredictionRetryFailure

        async with self.session_factory() as db:
            try:
                result = await SqlAlchemyPredictionRetryData(db).call(
                    operation, request
                )
                await db.commit()
                return result
            except PredictionRetryFailure:
                await db.rollback()
                raise
            except SQLAlchemyError:
                await db.rollback()
                raise PredictionRetryFailure("storage_unavailable") from None


@asynccontextmanager
async def retry_executor_sessions(fallback):
    """Use the function-only executor connection for retry reads and delivery."""
    from app.config import settings

    if not (
        settings.preannotation_results_database_url
        or settings.preannotation_results_database_url_file
    ):
        yield fallback
        return
    from aap_preannotation_results_runtime.config import ConfigurationError, load_config
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.application.preannotation_results import PreannotationResultDeferred

    try:
        config = load_config(
            {
                "PREANNOTATION_RESULTS_DATABASE_URL": settings.preannotation_results_database_url,
                "PREANNOTATION_RESULTS_DATABASE_URL_FILE": settings.preannotation_results_database_url_file,
            }
        )
    except ConfigurationError:
        raise PreannotationResultDeferred("storage_unavailable") from None
    engine = create_async_engine(
        make_url(config.database_url).set(drivername="postgresql+asyncpg"), echo=False
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
