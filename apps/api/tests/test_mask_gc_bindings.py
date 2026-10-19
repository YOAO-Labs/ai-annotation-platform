"""API transaction ownership and controlled native fence errors."""

from contextlib import contextmanager, asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from sqlalchemy.exc import DBAPIError

from app import deps
from app.services import mask_gc
from app.services.raster_mask_storage import RasterMaskContractError


KEY = "raster-masks/sha256/aa/aa/" + "a" * 64 + ".json"


@pytest.mark.asyncio
async def test_write_gate_keeps_pending_roots_unflushed_in_caller_transaction():
    events = []

    @contextmanager
    def no_autoflush():
        events.append("no_autoflush")
        yield
        events.append("caller_keeps_transaction")

    async def execute(statement, parameters):
        assert events == ["no_autoflush"]
        assert "begin_mask_content_write" in str(statement)
        assert json.loads(parameters["payload"]) == {"object_keys": [KEY]}
        return SimpleNamespace(
            scalar_one=lambda: {
                "schema": "aap-labeling-mask-gc/v1",
                "ok": True,
                "data": {"items": [{"object_key": KEY, "epoch": 3}]},
            }
        )

    db = SimpleNamespace(
        no_autoflush=no_autoflush(),
        execute=execute,
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    assert await mask_gc.begin_mask_content_write(db, [KEY, KEY]) == {KEY: 3}
    assert events == ["no_autoflush", "caller_keeps_transaction"]
    db.flush.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_mismatched_native_epoch_echo_fails_closed():
    async def execute(*_args):
        return SimpleNamespace(
            scalar_one=lambda: {
                "schema": "aap-labeling-mask-gc/v1",
                "ok": True,
                "data": {"items": []},
            }
        )

    with pytest.raises(RasterMaskContractError) as rejected:
        await mask_gc.read_mask_write_epochs(SimpleNamespace(execute=execute), [KEY])
    assert rejected.value.status_code == 503
    assert rejected.value.detail["reason"] == "mask_storage_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["content_deleting", "content_epoch_changed", None])
async def test_request_dependency_maps_only_native_mask_fences(monkeypatch, code):
    class NativeError(Exception):
        sqlstate = "AM409" if code else "23505"
        message = code

    error = DBAPIError("sensitive SQL", {}, NativeError("sensitive detail"))
    db = SimpleNamespace(rollback=AsyncMock())

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(deps, "async_session", session)
    dependency = deps.get_db()
    assert await anext(dependency) is db
    if code:
        with pytest.raises(HTTPException) as rejected:
            await dependency.athrow(error)
        assert rejected.value.status_code == 409
        assert rejected.value.detail["reason"] == f"mask_{code}"
        assert rejected.value.detail["retryable"] is True
        assert "sensitive" not in str(rejected.value.detail)
        db.rollback.assert_awaited_once()
    else:
        with pytest.raises(DBAPIError) as rejected:
            await dependency.athrow(error)
        assert rejected.value is error
        db.rollback.assert_not_awaited()
