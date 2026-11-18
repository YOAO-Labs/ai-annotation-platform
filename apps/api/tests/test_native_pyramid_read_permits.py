"""Focused durable-edge tests for Native pyramid read permits.

The permit is a durable storage responsibility, not a transaction held across
HTTP I/O.  A close may arrive before its admission acknowledgement; the closed
row is a tombstone that must fence every later admission of the same caller
identifiers.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


def _reply(value):
    return json.loads(value) if isinstance(value, str) else value


async def _complete(session, operation_id, token):
    return _reply(
        await session.scalar(
            text(
                "SELECT yoao_assets_v1.complete_native_pyramid_read("
                "CAST(:operation_id AS uuid), CAST(:token AS uuid))"
            ),
            {"operation_id": operation_id, "token": token},
        )
    )


async def _begin(session, operation_id, token):
    return _reply(
        await session.scalar(
            text(
                "SELECT yoao_assets_v1.begin_native_pyramid_read("
                "CAST(:actor_id AS uuid), CAST(:task_id AS uuid), NULL, 1, "
                "'pyramid-v1', CAST(:operation_id AS uuid), CAST(:token AS uuid))"
            ),
            {
                "actor_id": uuid.uuid4(),
                "task_id": uuid.uuid4(),
                "operation_id": operation_id,
                "token": token,
            },
        )
    )


async def test_close_before_admission_never_reopens_the_permit(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await session.begin()
        try:
            operation_id, token = uuid.uuid4(), uuid.uuid4()
            closed = await _complete(session, operation_id, token)
            assert closed["ok"] is True
            assert closed["data"] == {
                "operation_id": str(operation_id),
                "token": str(token),
                "closed": True,
            }

            # The tombstone is selected before any source authority work, so an
            # actor that would otherwise fail cannot open a replacement permit.
            late = await _begin(session, operation_id, token)
            assert late["ok"] is False
            assert late["error"]["code"] == "operation_conflict"
            assert (
                await session.scalar(
                    text(
                        "SELECT state FROM yoao_assets_v1.native_pyramid_read_permits "
                        "WHERE operation_id = CAST(:operation_id AS uuid)"
                    ),
                    {"operation_id": operation_id},
                )
                == "closed"
            )

            # Replayed physical closure is idempotent.
            replay = await _complete(session, operation_id, token)
            assert replay["data"] == closed["data"]
        finally:
            await session.rollback()


async def test_permit_token_is_exact_and_cannot_be_shared(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await session.begin()
        try:
            operation_id, token = uuid.uuid4(), uuid.uuid4()
            assert (await _complete(session, operation_id, token))["ok"] is True

            wrong = await _complete(session, operation_id, uuid.uuid4())
            assert wrong["ok"] is False
            assert wrong["error"]["code"] == "invalid_request"

            other_operation = await _complete(session, uuid.uuid4(), token)
            assert other_operation["ok"] is False
            assert other_operation["error"]["code"] == "invalid_request"
            assert (
                await session.scalar(
                    text(
                        "SELECT state FROM yoao_assets_v1.native_pyramid_read_permits "
                        "WHERE operation_id = CAST(:operation_id AS uuid)"
                    ),
                    {"operation_id": operation_id},
                )
                == "closed"
            )
        finally:
            await session.rollback()
