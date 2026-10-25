"""New derivative attempts settle only after their original publication commits."""

import asyncio
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services import (
    dataset_references as references,
    directory_previews as previews,
)
from app.services.content_snapshot import ContentError
from tests.test_dataset_content import content_world as content_world
from tests.test_directory_policies import object_store as object_store
from tests.test_storage_fault_events import world as world
from tests.test_storage_observation_readers import rate_world as rate_world, totals


async def register(db, world, **kwargs):
    return await references.register_entry(
        db, world[6].id, world[7].path_digest, world[2], **kwargs
    )


async def test_published_preview_failure_then_success_and_reuse_have_one_result_each(
    rate_world, db_session, test_engine, monkeypatch, object_store
):
    w = rate_world
    w[5].cache_policy = "preview"
    original = previews.encode
    monkeypatch.setattr(
        previews, "encode", AsyncMock(side_effect=RuntimeError("encoder"))
    )
    failed = await register(db_session, w)
    assert failed["preview_failed"] and failed["created"]
    assert await totals(test_engine, w[5].id, "preview_generate") == (0, 1)
    monkeypatch.setattr(previews, "encode", original)
    ready = await register(db_session, w)
    assert not ready["preview_failed"] and ready["preview_bytes"] > 0
    assert await totals(test_engine, w[5].id, "preview_generate") == (1, 1)
    await register(db_session, w)
    assert await totals(test_engine, w[5].id, "preview_generate") == (1, 1)
    assert await totals(test_engine, w[5].id) == (6, 0)


async def test_disabled_preview_has_no_attempt(rate_world, db_session, test_engine):
    w = rate_world
    assert w[5].cache_policy == "none"
    await register(db_session, w)
    assert await totals(test_engine, w[5].id, "preview_generate") == (0, 0)


async def test_ready_preparation_with_refused_publish_does_not_count(
    rate_world, db_session, test_engine, object_store
):
    w = rate_world
    w[5].cache_policy = "preview"

    async def refuse(_db, _result):
        assert await totals(test_engine, w[5].id, "preview_generate") == (0, 0)
        raise ContentError("publication_refused")

    with pytest.raises(ContentError, match="publication_refused"):
        await register(db_session, w, on_result=refuse)
    assert await totals(test_engine, w[5].id, "preview_generate") == (0, 0)


async def test_lost_final_commit_reply_never_fabricates_preview_result(
    rate_world, db_session, test_engine, object_store, monkeypatch
):
    w = rate_world
    w[5].cache_policy = "preview"
    original = db_session.commit

    async def lose_reply(_db, _result):
        async def commit():
            monkeypatch.setattr(db_session, "commit", original)
            await original()
            raise ConnectionError("lost acknowledgement")

        monkeypatch.setattr(db_session, "commit", commit)

    with pytest.raises(ConnectionError, match="lost acknowledgement"):
        await register(db_session, w, on_result=lose_reply)
    assert await totals(test_engine, w[5].id, "preview_generate") == (0, 0)


@pytest.mark.parametrize("kind", ["unsettled", "authority", "database", "context"])
async def test_preview_context_and_unsettled_failures_are_excluded_after_publish(
    rate_world, db_session, test_engine, monkeypatch, kind
):
    w = rate_world
    w[5].cache_policy = "preview"
    failure = {
        "unsettled": RuntimeError("close uncertain"),
        "authority": HTTPException(403, "revoked"),
        "database": SQLAlchemyError("unavailable"),
        "context": ContentError("configuration_changed"),
    }[kind]
    if kind == "unsettled":
        failure.storage_operation_unsettled = True
    monkeypatch.setattr(previews, "encode", AsyncMock(side_effect=failure))
    result = await register(db_session, w)
    assert result["preview_failed"]
    assert await totals(test_engine, w[5].id, "preview_generate") == (0, 0)


async def test_explicit_preview_write_rejection_counts_failed_published_attempt(
    rate_world, db_session, test_engine, monkeypatch
):
    w = rate_world
    w[5].cache_policy = "preview"
    failure = ContentError("thumbnail_write_rejected", 503)
    failure.thumbnail_write_refused = True
    monkeypatch.setattr(previews.outputs, "create", AsyncMock(side_effect=failure))
    result = await register(db_session, w)
    assert result["preview_failed"]
    assert await totals(test_engine, w[5].id, "preview_generate") == (0, 1)


async def test_preview_cancellation_propagates_without_result(
    rate_world, db_session, test_engine, monkeypatch
):
    w = rate_world
    w[5].cache_policy = "preview"
    monkeypatch.setattr(
        previews, "encode", AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await register(db_session, w)
    assert await totals(test_engine, w[5].id, "preview_generate") == (0, 0)
