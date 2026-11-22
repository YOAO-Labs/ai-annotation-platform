"""Exercise the export fixture reader against the private owner and real store."""

import hashlib
import os
import uuid
from dataclasses import replace

import pytest
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.user import User
from app.services import (
    fixture_original_read,
    managed_original_reader,
    original_sources,
)
from app.services.content_snapshot import ContentError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.identity_e2e_support import fixture_routing_owner
from tests.seed_owned_scenarios import _build_owned
from tests.seed_owner_support import private_owned_suite

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_FIXTURE_EXPORT_ORIGINAL_LIVE") != "1",
        reason="requires the explicitly enabled private fixture-store verifier",
    ),
]


async def test_private_original_exact_version_and_caller_transaction(
    test_engine, monkeypatch
):
    async with (
        fixture_routing_owner(test_engine, monkeypatch),
        private_owned_suite() as suite,
    ):
        run = suite.new_run(uuid.uuid4().hex[:10])
        world = await _build_owned(suite.owner, run)
        sessions = async_sessionmaker(test_engine, expire_on_commit=False)
        async with sessions() as db:
            actor_id = await db.scalar(
                select(User.id).where(User.email == world["admin_email"])
            )
            source = await original_sources.resolve_task(
                db, uuid.UUID(world["task_ids"][0])
            )
            assert actor_id is not None and source.owner_kind == "dataset_asset"
            assert await fixture_original_read.is_fixture(db, source)
            receipt = await db.get(
                ManagedOriginalCreation, source.replica_facts.creation_id
            )
            assert receipt is not None and receipt.version_id and receipt.etag
            version, etag = receipt.version_id, receipt.etag
            await db.rollback()

            await db.execute(
                text(
                    "CREATE TEMP TABLE export_reader_transaction "
                    "(value integer) ON COMMIT DROP"
                )
            )
            await db.execute(text("INSERT INTO export_reader_transaction VALUES (7)"))
            plan = original_sources.read_plan(source)
            async with fixture_original_read.admitted(
                db, source, actor_id, plan.spec
            ) as (
                spec,
                options,
            ):
                assert (spec.expected_version_id, spec.expected_etag) == (version, etag)
                stage = await managed_original_reader.capture(
                    spec, max_bytes=source.size_bytes, timeout_seconds=30, **options
                )
                try:
                    payload = stage.file.read()
                finally:
                    stage.close()
                assert len(payload) == source.size_bytes
                assert hashlib.sha256(payload).hexdigest() == source.sha256
            assert (
                await db.scalar(text("SELECT value FROM export_reader_transaction"))
            ) == 7

            changed = replace(source, sha256="0" * 64)
            with pytest.raises(ContentError) as error:
                async with fixture_original_read.admitted(
                    db, changed, actor_id, original_sources.read_plan(changed).spec
                ):
                    pytest.fail("changed source identity was admitted")
            assert error.value.code == "content_binding_changed"
            assert (
                await db.scalar(text("SELECT value FROM export_reader_transaction"))
            ) == 7
            await db.rollback()
