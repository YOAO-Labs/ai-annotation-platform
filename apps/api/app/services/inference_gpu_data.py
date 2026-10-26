"""Borrow only finite GPU caller capabilities from the API's unit of work."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aap_inference.gpu.caller_admin import (
    GPUCallerAdministrativeData,
    bind_gpu_caller_administrative_data,
)
from aap_inference_contract.gpu import GPUAuthorityUnavailable
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def bind_gpu_authority_data(db: AsyncSession) -> GPUCallerAdministrativeData:
    """Keep writes in the caller transaction without private owner authority."""
    try:
        connection = await db.connection()
        # SQLAlchemy begins the physical transaction lazily, before raw-driver use.
        await connection.execute(text("SELECT pg_catalog.pg_current_xact_id()"))
        raw = await connection.get_raw_connection()
        return await bind_gpu_caller_administrative_data(raw.driver_connection)
    except GPUAuthorityUnavailable:
        raise
    except Exception:
        raise GPUAuthorityUnavailable() from None


@asynccontextmanager
async def open_gpu_authority_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[GPUCallerAdministrativeData]:
    async with session_factory.begin() as db:
        yield await bind_gpu_authority_data(db)
