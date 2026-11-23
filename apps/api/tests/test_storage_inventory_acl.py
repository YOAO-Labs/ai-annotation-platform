"""The ordinary LOGIN observes exact references without private relation grants."""

from datetime import date, datetime, timezone
import json
import os
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.audit_archive_output import AuditArchiveOutput
from app.db.models.storage_usage import StorageUsageMutation
from app.services import (
    audit_archive_storage,
    storage_usage_accounting as accounting,
    storage_usage_categories,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.storage_migration_reference_collector import FAMILIES, Spool, collect
from app.services.storage_migration_references import Budget, Limits
from tests.test_workspace_project_authority_locking import _api_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned PostgreSQL and ordinary API LOGIN",
    ),
]


def archive(identifier, *, ready):
    moment = datetime.now(timezone.utc)
    endpoint = storage_usage_categories.endpoint()
    bucket = storage_service.audit_archive_bucket
    ref = {
        "schema": 1,
        "purpose": "audit-archive",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "object_key": audit_archive_storage.key_for(2097, 1, identifier),
        "storage_identity": managed_storage_identity(endpoint, bucket),
    }
    return AuditArchiveOutput(
        id=identifier,
        job_id=uuid.uuid4(),
        year=2097,
        month=1,
        source_schema="public",
        source_table="audit_logs_y2097m01",
        parent_oid=1,
        source_oid=2,
        month_start=date(2097, 1, 1),
        month_end=date(2097, 2, 1),
        row_count=1,
        source_size_bytes=27,
        source_sha256="f" * 64,
        sha256="7" * 64,
        size_bytes=15,
        storage_ref=ref,
        state="ready" if ready else "pending",
        created_at=moment,
        published_at=moment if ready else None,
    )


async def test_ordinary_snapshot_sees_all_reference_families_and_pages_audit_claims(
    test_engine, tmp_path
):
    pending, published = sorted((uuid.uuid4(), uuid.uuid4()))
    archives = [archive(pending, ready=False), archive(published, ready=True)]
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner.begin() as db:
        db.add_all(archives)
        revisions = list(
            await db.scalars(
                text(
                    "SELECT version_num FROM public.alembic_version ORDER BY version_num"
                )
            )
        )

    ordinary = create_async_engine(_api_url(test_engine), hide_parameters=True)
    try:
        async with ordinary.connect() as conn:
            visible = (
                await conn.scalars(
                    text(
                        "SELECT name FROM unnest(CAST(:names AS text[])) AS name "
                        "WHERE pg_catalog.has_any_column_privilege("
                        "current_user, 'public.' || name, 'SELECT,INSERT,UPDATE,REFERENCES') "
                        "OR pg_catalog.has_table_privilege(current_user, 'public.' || name, "
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                    ),
                    {
                        "names": [
                            *(family.table for family in FAMILIES),
                            "alembic_version",
                            "storage_locations",
                            "storage_connections",
                            "storage_bindings",
                            "storage_usage_inventories",
                            "storage_usage_snapshots",
                            "storage_usage_schedules",
                            "storage_usage_mutations",
                            "storage_usage_reconciliations",
                            "storage_usage_inventory_objects",
                            "storage_migration_inventories",
                            "storage_migration_copies",
                            "storage_migration_copy_attempts",
                            "storage_migration_copy_deltas",
                            "storage_migration_windows",
                            "storage_migration_finalizations",
                            "storage_migration_read_publications",
                            "storage_migration_read_routes",
                            "storage_migration_cutovers",
                        ]
                    },
                )
            ).all()
            assert not visible

        path = tmp_path / "reference-snapshot.jsonl"
        budget = Budget(Limits(page_size=1))
        spool = Spool(path, budget)
        try:
            metadata = await collect(ordinary, spool, budget)
        finally:
            spool.stream.close()
        assert metadata["enumeration_complete"], metadata
        assert metadata["revisions"] == revisions
        assert metadata["families"] == [family.table for family in FAMILIES]
        assert "families_not_present_in_schema" not in metadata
        archive_ids = [[str(pending)], [str(published)]]
        reference_rows = []
        with path.open() as stream:
            for line in stream:
                row = json.loads(line)
                if (
                    row.get("family") == "audit_archive_outputs"
                    and row.get("owner_id") in archive_ids
                ):
                    reference_rows.append(row)
        assert [row["owner_id"] for row in reference_rows] == archive_ids
        assert [row["owner_state"] for row in reference_rows] == ["pending", "ready"]
        assert [row["key"] for row in reference_rows] == [
            a.storage_ref["object_key"] for a in archives
        ]
        assert all(
            row["sha256"] == "7" * 64 and "source_sha256" not in row
            for row in reference_rows
        )

        factory = async_sessionmaker(ordinary)
        async with factory() as db:
            classified = await storage_usage_categories.classify(
                db,
                archives[0].storage_ref["bucket"],
                [
                    {"key": a.storage_ref["object_key"], "size": a.size_bytes}
                    for a in archives
                ],
            )
            assert classified["categories"]["other_platform"] == {
                "object_count": 2,
                "size_bytes": 30,
            }
    finally:
        await ordinary.dispose()
        async with owner.begin() as db:
            await db.execute(
                delete(AuditArchiveOutput).where(
                    AuditArchiveOutput.id.in_((pending, published))
                )
            )


async def test_ordinary_usage_intent_replay_and_exact_settlement(test_engine):
    ordinary = create_async_engine(_api_url(test_engine), hide_parameters=True)
    factory = async_sessionmaker(ordinary)
    owner = async_sessionmaker(test_engine)
    values = {
        "endpoint_url": "https://inventory-ledger.test",
        "bucket": f"acl-{uuid.uuid4().hex}",
        "object_key": "exact/%2F/中文/payload",
        "producer": "managed_original_creation",
        "owner_id": uuid.uuid4(),
        "purpose": "managed_original",
        "kind": "create",
        "nonce": "one-attempt",
    }
    scope = accounting.scope_key(values["endpoint_url"], values["bucket"])
    try:
        async with factory() as db:
            rolled_back = await accounting.begin_in_transaction(db, **values)
            await db.rollback()
        async with factory.begin() as db:
            intent = await accounting.begin_in_transaction(db, **values)
            assert intent.fresh and intent.id != rolled_back.id
        async with factory.begin() as db:
            replay = await accounting.begin_in_transaction(db, **values)
            assert replay.id == intent.id and not replay.fresh
        await accounting.publish(
            factory,
            intent,
            {
                "physical_outcome": "written",
                "physical_size_bytes": 27,
                "current_delta_bytes": 27,
                "returned_version": "pinned-%2F-original",
            },
        )
        await accounting.publish(factory, replay, {"current_delta_bytes": 999})
        async with factory() as db:
            ledger, _, _ = await accounting.summaries(db, [{"key": scope}])
            assert ledger.net_change_bytes == 27 and ledger.pending_operation_count == 0
        async with owner() as db:
            row = await db.get(StorageUsageMutation, intent.id)
            assert row.object_key == values["object_key"] and row.state == "confirmed"
            assert row.evidence["returned_version"] == "pinned-%2F-original"
    finally:
        await ordinary.dispose()
        async with owner.begin() as db:
            await db.execute(
                delete(StorageUsageMutation).where(
                    StorageUsageMutation.scope_key == scope
                )
            )
