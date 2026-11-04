"""Durable, exact managed-original read admission for Images operations."""

from __future__ import annotations

import asyncio
import builtins
from contextlib import asynccontextmanager

from yoao_assets_images import contract as c


async def _finish(database, operation, permit):
    task = asyncio.create_task(
        database.call(
            operation,
            c.FinishOriginalReadRequest(
                creation_id=permit.creation_id, operation_id=permit.operation_id
            ),
        )
    )
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
        except BaseException:
            break
    try:
        task.result()
    except BaseException as exc:
        if cancelled is not None:
            raise builtins.BaseExceptionGroup(
                "Image source settlement was cancelled and then failed",
                [cancelled, exc],
            ) from None
        raise
    if cancelled is not None:
        cancelled.storage_operation_settled = True
        raise cancelled


@asynccontextmanager
async def admitted_original(database, storage, operation, request, finish_operation):
    permit = await database.call(operation, request)
    if not isinstance(permit, c.OriginalRead):
        raise c.ImageFailure("invalid_response")
    failure = None
    try:
        yield storage.bind_original(permit)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if permit.ownership == "fixture" and not getattr(
            failure, "storage_operation_unsettled", False
        ):
            try:
                await _finish(database, finish_operation, permit)
            except BaseException as finish_error:
                uncertain = not getattr(
                    finish_error, "storage_operation_settled", False
                )
                if uncertain:
                    finish_error.storage_operation_unsettled = True
                if failure is not None:
                    combined = builtins.BaseExceptionGroup(
                        "Image source read and permit settlement both failed",
                        [failure, finish_error],
                    )
                    if uncertain:
                        combined.storage_operation_unsettled = True
                    raise combined from None
                raise
