"""Autogeneration must not propose dropping the native input/evidence ledgers."""

from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import text

from app.db.base import Base
from app.db.native_schema import NATIVE_MANAGED_TABLES, include_orm_schema_object


@pytest.mark.asyncio
async def test_native_ledgers_are_retained_while_unmapped_orm_drops_still_surface(
    test_engine,
):
    control = "test_autogen_control_" + uuid4().hex

    def proposed_drops(connection, include_object=None):
        context = MigrationContext.configure(
            connection,
            opts={"include_object": include_object} if include_object else {},
        )
        return {
            row[1].name
            for row in compare_metadata(context, Base.metadata)
            if isinstance(row, tuple) and row[0] == "remove_table"
        }

    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                text(f"CREATE TABLE public.{control} (id integer PRIMARY KEY)")
            )
            unguarded = await connection.run_sync(proposed_drops)
            assert NATIVE_MANAGED_TABLES <= unguarded
            guarded = await connection.run_sync(
                proposed_drops, include_orm_schema_object
            )
            assert not NATIVE_MANAGED_TABLES & guarded
            assert control in guarded
        finally:
            await transaction.rollback()
