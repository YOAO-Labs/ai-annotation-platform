"""Autogeneration must not propose dropping native ledgers and GC authority."""

from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import text

from app.db.base import Base
from app.db.native_schema import (
    NATIVE_MANAGED_RELATIONS,
    NATIVE_MANAGED_TABLES,
    include_orm_schema_object,
)


@pytest.mark.asyncio
async def test_native_ledgers_are_retained_while_unmapped_orm_drops_still_surface(
    test_engine,
):
    assert {
        "video_tracker_input_scopes",
        "annotation_video_tracker_input_sources",
        "mask_gc_intents",
        "mask_gc_key_epochs",
        "mask_gc_sweeps",
        "mask_gc_roots",
        "mask_gc_write_permit_secrets",
        "identity_revoked_jtis",
    } <= NATIVE_MANAGED_TABLES
    assert {
        ("aap_jobs_v1", "video_restore_fence"),
        *(
            ("aap_inference_routing_v1", name)
            for name in (
                "video_restore_records",
                "video_restore_contexts",
                "video_restore_scopes",
                "video_restore_attempts",
                "video_restore_scope_retirements",
                "video_restore_no_scope_receipts",
                "video_restore_function_restore",
            )
        ),
    } <= NATIVE_MANAGED_RELATIONS
    control = "test_autogen_control_" + uuid4().hex

    def proposed_drops(connection, include_object=None):
        options = {
            "include_schemas": True,
            "include_name": lambda name, kind, parents: (
                kind != "schema"
                or name in {None, *(schema for schema, _ in NATIVE_MANAGED_RELATIONS)}
            ),
        }
        if include_object is not None:
            options["include_object"] = include_object
        context = MigrationContext.configure(
            connection,
            opts=options,
        )
        return {
            (row[1].schema or "public", row[1].name)
            for row in compare_metadata(context, Base.metadata)
            if isinstance(row, tuple) and row[0] == "remove_table"
        }

    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                text(f"CREATE TABLE public.{control} (id integer PRIMARY KEY)")
            )
            await connection.execute(
                text(f"CREATE TABLE aap_jobs_v1.{control} (id integer PRIMARY KEY)")
            )
            unguarded = await connection.run_sync(proposed_drops)
            assert NATIVE_MANAGED_RELATIONS <= unguarded
            guarded = await connection.run_sync(
                proposed_drops, include_orm_schema_object
            )
            assert not NATIVE_MANAGED_RELATIONS & guarded
            assert ("public", control) in guarded
            assert ("aap_jobs_v1", control) in guarded
        finally:
            await transaction.rollback()
