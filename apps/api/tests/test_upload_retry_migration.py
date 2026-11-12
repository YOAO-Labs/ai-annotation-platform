"""Round-trip bo0458 and its retry-history downgrade guard."""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.asyncio

API_ROOT = Path(__file__).resolve().parents[1]
SQL_ROOT = API_ROOT / "alembic" / "sql"
_FUNCTION_BODY = re.compile(r"\$function\$(?P<body>.*?)\$function\$", re.DOTALL)

_RESERVED_KEY = "dataset-sources/retry/dataset/item/publication/file.png"
_STAGING_KEY = "dataset-uploads/item/source"


def _frozen_body(name: str) -> str:
    source = (SQL_ROOT / name).read_text(encoding="utf-8")
    match = re.search(
        r"CREATE OR REPLACE FUNCTION\s+aap_assets_v1\.begin_operator_auxiliary\s*\("
        r".*?\$function\$(?P<body>.*?)\$function\$;",
        source,
        re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def _config(url: str) -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _state(url: str) -> tuple[str, str, set[str], set[str]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            version = await connection.scalar(
                text("SELECT version_num FROM public.alembic_version")
            )
            definition = await connection.scalar(
                text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
                {"signature": "aap_assets_v1.begin_operator_auxiliary(jsonb)"},
            )
            indexes = set(
                await connection.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname='public' "
                        "AND tablename='managed_original_creations'"
                    )
                )
            )
            constraints = set(
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid='public.managed_original_creations'::regclass "
                        "AND contype='u'"
                    )
                )
            )
    finally:
        await engine.dispose()
    match = _FUNCTION_BODY.search(definition)
    assert match is not None
    return version, match.group("body"), indexes, constraints


async def _insert_retry_rows(url: str) -> uuid.UUID:
    engine = create_async_engine(url)
    dataset_id = uuid.uuid4()
    rows = [
        {
            "id": uuid.uuid4(),
            "status": "failed",
            "completed_at": "now()",
            "error_code": "test_failed",
        },
        {"id": uuid.uuid4(), "status": "allocated"},
    ]
    try:
        async with engine.begin() as connection:
            for row in rows:
                await connection.execute(
                    text(
                        "INSERT INTO public.managed_original_creations ("
                        "id, owner_kind, dataset_id, location_id, storage_identity, "
                        "bucket, object_key, logical_path, origin, max_bytes, "
                        "timeout_seconds, content_type, requested_by, authority, "
                        "configuration, input_fingerprint, maintenance_revision, "
                        "staging_bucket, staging_key, status, completed_at, error_code"
                        ") VALUES ("
                        ":id, 'dataset', :dataset, :location, :identity, "
                        ":bucket, :object_key, 'file.png', 'direct', 1048576, "
                        "60, 'image/png', :actor, "
                        'CAST(\'{"kind":"dataset_upload"}\' AS jsonb), '
                        "CAST('{}' AS jsonb), :fingerprint, 1, "
                        ":staging_bucket, :staging_key, :status, "
                        + (
                            "now(), :error_code"
                            if row["status"] == "failed"
                            else "NULL, NULL"
                        )
                        + ")"
                    ),
                    {
                        "id": row["id"],
                        "dataset": dataset_id,
                        "location": uuid.uuid4(),
                        "identity": "a" * 64,
                        "bucket": "retry-acceptance",
                        "object_key": _RESERVED_KEY,
                        "actor": uuid.uuid4(),
                        "fingerprint": "b" * 64,
                        "staging_bucket": "retry-staging",
                        "staging_key": _STAGING_KEY,
                        "status": row["status"],
                        **(
                            {"error_code": row["error_code"]}
                            if "error_code" in row
                            else {}
                        ),
                    },
                )
    finally:
        await engine.dispose()
    return rows[0]["id"]


async def test_upload_retry_migration_roundtrip_and_refusal(test_engine):
    async with test_engine.connect() as connection:
        can_create = await connection.scalar(
            text(
                "SELECT rolsuper OR rolcreatedb FROM pg_roles "
                "WHERE rolname=current_user"
            )
        )
    if not can_create:
        pytest.skip("migration roundtrip needs a disposable database owner")

    admin_url = make_url(test_engine.url).set(database="postgres")
    name = f"aap_wt_{uuid.uuid4().hex[:12]}_upload_retry_test"
    target_url = make_url(test_engine.url).set(database=name)
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
        url = target_url.render_as_string(hide_password=False)
        config = _config(url)
        old_body = _frozen_body("0379_operator_auxiliary.sql")
        new_body = _frozen_body("0458_upload_retry_after_failed_publication.sql")
        assert old_body != new_body

        await asyncio.to_thread(command.upgrade, config, "bo0456")
        version, body, indexes, constraints = await _state(url)
        assert version == "bo0456"
        assert body == old_body
        assert "uq_managed_creation_reserved_key" in constraints
        assert "managed_original_creations_staging_key_key" in constraints
        assert "uq_managed_creation_reserved_key_active" not in indexes

        await asyncio.to_thread(command.upgrade, config, "head")
        version, body, indexes, constraints = await _state(url)
        assert version == "bo0458"
        assert body == new_body
        assert "uq_managed_creation_reserved_key_active" in indexes
        assert "uq_managed_creation_staging_key_active" in indexes
        assert "uq_managed_creation_reserved_key" not in constraints
        assert "managed_original_creations_staging_key_key" not in constraints

        await asyncio.to_thread(command.downgrade, config, "bo0456")
        version, body, indexes, constraints = await _state(url)
        assert version == "bo0456"
        assert body == old_body
        assert "uq_managed_creation_reserved_key_active" not in indexes
        assert "uq_managed_creation_staging_key_active" not in indexes
        assert "uq_managed_creation_reserved_key" in constraints
        assert "managed_original_creations_staging_key_key" in constraints

        await asyncio.to_thread(command.upgrade, config, "head")
        failed_id = await _insert_retry_rows(url)
        before = await _state(url)
        with pytest.raises(Exception) as refused:
            await asyncio.to_thread(command.downgrade, config, "bo0456")
        assert "reserved original keys have retry history" in str(refused.value)
        assert await _state(url) == before
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM public.managed_original_creations "
                            "WHERE object_key=:key AND staging_key=:staging"
                        ),
                        {"key": _RESERVED_KEY, "staging": _STAGING_KEY},
                    )
                    == 2
                )
                assert (
                    await connection.scalar(
                        text(
                            "SELECT status FROM public.managed_original_creations "
                            "WHERE id=:id"
                        ),
                        {"id": failed_id},
                    )
                    == "failed"
                )
        finally:
            await engine.dispose()
    finally:
        async with admin.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            )
        await admin.dispose()
