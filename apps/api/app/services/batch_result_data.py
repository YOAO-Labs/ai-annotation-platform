"""Bind the portable result capabilities to the caller's database transaction."""

from __future__ import annotations

from contextlib import nullcontext

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.mask_gc import mask_fence_error_code


def _native_rejection(exc):
    from typing import get_args
    from aap_preannotation_results.contract import ErrorCode

    allowed = frozenset(get_args(ErrorCode))
    pending, seen = [exc], set()
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


class SqlAlchemyBatchResultData:
    """Never commit: business writes and their receipt have one transaction owner."""

    def __init__(self, db, *, writer: bool = False):
        self.db = db
        self.writer = writer

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        from aap_preannotation_results.contract import BatchResultFailure
        from aap_preannotation_results_runtime.database import (
            prepare_native_call,
            validate_native_result,
        )

        statement, arguments = prepare_native_call(
            operation, request, writer=self.writer
        )
        if len(arguments) != 1:
            raise BatchResultFailure("invalid_request")
        try:
            with getattr(self.db, "no_autoflush", nullcontext()):
                raw = (
                    await self.db.execute(
                        text(statement.replace("$1::jsonb", "CAST(:payload AS jsonb)")),
                        {"payload": arguments[0]},
                    )
                ).scalar_one()
                return validate_native_result(operation, request, raw)
        except BatchResultFailure:
            raise
        except SQLAlchemyError as exc:
            raise BatchResultFailure(
                mask_fence_error_code(exc)
                or _native_rejection(exc)
                or "storage_unavailable"
            ) from None


class IndependentBatchResultData:
    """Commit one checkpoint or heartbeat independently of the inference session."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        from aap_preannotation_results.contract import BatchResultFailure

        async with self.session_factory() as db:
            try:
                result = await SqlAlchemyBatchResultData(db).call(operation, request)
                await db.commit()
                return result
            except BatchResultFailure:
                await db.rollback()
                raise
            except SQLAlchemyError:
                await db.rollback()
                # The commit acknowledgement can be lost. Immutable calls and
                # reads of their receipt resolve it on the next delivery.
                raise BatchResultFailure("storage_unavailable") from None
