"""Durable, exact managed-original read admission for Media operations."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar

from yoao_assets_media import contract as c


_fixture_original = ContextVar("media_fixture_original", default=None)


def fixture_original_active():
    return _fixture_original.get() is not None


def fixture_upload_started():
    context = _fixture_original.get()
    if context is not None:
        context["write_started"] = True


def fixture_upload_was_started():
    context = _fixture_original.get()
    return context is not None and context["write_started"]


def blocks_physical_source(permit, endpoint, bucket):
    """Check the saved source and exclusions against the adapter's target."""
    if not permit.blocked_storage_identities and permit.source_storage_identity is None:
        return False
    identity = hashlib.sha256(
        json.dumps([endpoint.rstrip("/"), bucket]).encode()
    ).hexdigest()
    return (
        identity in permit.blocked_storage_identities
        or permit.source_storage_identity is not None
        and identity != permit.source_storage_identity
    )


def _unsettled(failure):
    if getattr(failure, "storage_operation_unsettled", False):
        return True
    return isinstance(failure, BaseExceptionGroup) and any(
        _unsettled(child) for child in failure.exceptions
    )


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
            cancelled.storage_operation_unsettled = True
            raise cancelled from exc
        raise
    if cancelled is not None:
        cancelled.storage_operation_settled = True
        raise cancelled


@asynccontextmanager
async def original_permit(database, operation, request, finish_operation):
    """Own durable admission until the caller's physical work has drained."""
    permit = await database.call(operation, request)
    if not isinstance(permit, c.OriginalRead):
        raise c.MediaFailure("invalid_response")
    failure = None
    evidence = {"write_started": False} if permit.ownership == "fixture" else None
    context = _fixture_original.set(evidence)
    try:
        yield permit
    except BaseException as exc:
        failure = exc
        if evidence is not None and evidence["write_started"]:
            exc.storage_operation_unsettled = True
        raise
    finally:
        _fixture_original.reset(context)
        # Uncertain provider handles retain the durable operation even if local
        # cancellation or process death makes a later settlement impossible.
        if permit.ownership == "fixture" and not _unsettled(failure):
            try:
                await _finish(database, finish_operation, permit)
            except BaseException as finish_error:
                uncertain = not getattr(
                    finish_error, "storage_operation_settled", False
                )
                if uncertain:
                    finish_error.storage_operation_unsettled = True
                if isinstance(failure, asyncio.CancelledError):
                    if uncertain:
                        failure.storage_operation_unsettled = True
                    raise failure from finish_error
                if failure is not None:
                    combined = builtins.BaseExceptionGroup(
                        "Media source read and permit settlement both failed",
                        [failure, finish_error],
                    )
                    if uncertain:
                        combined.storage_operation_unsettled = True
                    raise combined from None
                raise


@asynccontextmanager
async def admitted_original(database, storage, operation, request, finish_operation):
    async with original_permit(
        database, operation, request, finish_operation
    ) as permit:
        yield storage.bind_original(permit)
