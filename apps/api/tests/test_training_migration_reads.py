"""Saved training reads translate only their admitted physical transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import training_dataset_storage as owners, training_storage_io as io
from app.services.content_snapshot import ContentError
from app.services.training_dataset_bridge import TrainingDatasetError


def owner():
    ref = {
        "location_id": str(uuid.uuid4()),
        "endpoint_url": "https://saved.example.test",
        "bucket": "saved-a",
    }
    return SimpleNamespace(
        ref=ref,
        bucket=ref["bucket"],
        contains=lambda key: key == "saved/object" or owners._reject(),
    )


async def test_route_keeps_logical_storage_and_forwards_result_cleanup(monkeypatch):
    saved, route, client = owner(), object(), object()
    resolve = AsyncMock(return_value=route)
    acquired, close = object(), object()
    seen = []

    async def routed(db, actual, purpose, callback, **kwargs):
        seen.append((db, actual, purpose, kwargs))
        return callback(client)

    monkeypatch.setattr(io.artifact_reads, "resolve", resolve)
    monkeypatch.setattr(io.artifact_reads, "run", routed)
    monkeypatch.setattr(io, "run", AsyncMock(side_effect=AssertionError("A read")))

    def read(bound, extra, *, flag):
        assert bound.client is client
        assert bound.bucket == bound.training_bucket == "saved-a"
        assert bound._legacy is False and bound._write_accounting is None
        assert extra == 7 and flag is True
        return "verified bytes"

    assert (
        await io.read(
            "db",
            saved,
            "training_owner_read",
            read,
            7,
            object_key="saved/object",
            sha256="a" * 64,
            on_acquired=acquired,
            close_result=close,
            timeout=12,
            flag=True,
        )
        == "verified bytes"
    )
    resolve.assert_awaited_once_with("db", saved.ref, "saved/object", "a" * 64, None)
    assert seen == [
        (
            "db",
            route,
            "training_owner_read",
            {
                "on_acquired": acquired,
                "close_result": close,
                "timeout": 12,
            },
        )
    ]


async def test_missing_route_retains_original_admission(monkeypatch):
    saved, callback, acquired = owner(), object(), object()
    monkeypatch.setattr(io.artifact_reads, "resolve", AsyncMock(return_value=None))
    original = AsyncMock(return_value="original")
    monkeypatch.setattr(io, "run", original)
    assert (
        await io.read(
            "db",
            saved,
            "read",
            callback,
            4,
            object_key="saved/object",
            sha256=None,
            size_bytes=9,
            on_acquired=acquired,
            timeout=10,
        )
        == "original"
    )
    original.assert_awaited_once_with(
        "db",
        saved,
        "read",
        callback,
        4,
        on_acquired=acquired,
        timeout=10,
    )


async def test_foreign_key_is_rejected_before_mapping_or_io(monkeypatch):
    resolve = AsyncMock()
    monkeypatch.setattr(io.artifact_reads, "resolve", resolve)
    with pytest.raises(TrainingDatasetError):
        await io.read(
            "db", owner(), "read", object(), object_key="foreign", sha256="a" * 64
        )
    resolve.assert_not_awaited()


@pytest.mark.parametrize("stage", ["resolve", "run"])
async def test_route_failures_keep_safe_training_error(monkeypatch, stage):
    failure = ContentError("read_publication_changed", 409)
    monkeypatch.setattr(
        io.artifact_reads,
        "resolve",
        AsyncMock(
            **(
                {"side_effect": failure}
                if stage == "resolve"
                else {"return_value": object()}
            )
        ),
    )
    monkeypatch.setattr(io.artifact_reads, "run", AsyncMock(side_effect=failure))
    with pytest.raises(TrainingDatasetError) as caught:
        await io.read(
            "db", owner(), "read", object(), object_key="saved/object", sha256="a" * 64
        )
    assert caught.value.code == failure.code and caught.value.status_code == 409
