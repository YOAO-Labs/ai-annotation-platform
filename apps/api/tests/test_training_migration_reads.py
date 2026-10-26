"""Saved training reads translate only their admitted physical transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from copy import deepcopy
import io as binary_io
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


@pytest.mark.parametrize("resume", [False, True])
async def test_assignment_routes_each_saved_input_with_its_content(monkeypatch, resume):
    from app.services import training_runs
    from app.db.models.training_dataset import TrainingDatasetVersion
    from tests.test_training_run_storage_flow import owned

    world = owned.__wrapped__(monkeypatch)
    world.dataset.package_sha256, world.dataset.size_bytes = "a" * 64, 17
    parent = SimpleNamespace(id=uuid.uuid4(), run_id=world.run.id)
    checkpoint = {
        "role": "checkpoint",
        "sha256": "b" * 64,
        "size_bytes": 19,
        "key": world.run.storage_ref["prefix"] + f"attempts/{parent.id}/last.pt",
    }
    parent.artifacts = {"last.pt": checkpoint}
    if resume:
        world.attempt.resume_from_id = parent.id
    calls = []

    async def read(db, saved, purpose, callback, **kwargs):
        calls.append((saved.ref, kwargs))
        return callback(io.storage_service)

    async def get(model, identifier):
        return world.dataset if model is TrainingDatasetVersion else parent

    monkeypatch.setattr(io, "read", read)
    before = deepcopy(world.run.config)
    result = await training_runs.assignment(
        SimpleNamespace(get=get, commit=AsyncMock()),
        world.run,
        world.attempt,
        "lease",
    )
    assert calls[0] == (
        world.dataset.storage_ref,
        {
            "object_key": world.dataset.package_key,
            "sha256": "a" * 64,
            "size_bytes": 17,
        },
    )
    if resume:
        assert calls[1] == (
            world.run.storage_ref,
            {
                "object_key": checkpoint["key"],
                "sha256": "b" * 64,
                "size_bytes": 19,
            },
        )
        assert checkpoint["key"] in result["resume"]["url"]
    assert len(calls) == (2 if resume else 1)
    assert world.run.config == before


async def test_initialization_keeps_weight_and_report_reads_separate(monkeypatch):
    from app.services import training_initialization as initialization
    from app.services import training_artifacts, training_runs, training_run_storage
    from app.services.training_dataset_bridge import content_digest
    from tests.test_training_run_storage_flow import owned, artifact

    world = owned.__wrapped__(monkeypatch)
    weights, metrics = (
        artifact(world),
        artifact(world, "metrics.json", "metrics", b"{}"),
    )
    saved = training_run_storage.resolve(world.run)
    report = {"score": 1}
    pin = {
        "artifact": weights,
        "attempt_id": str(world.attempt.id),
        "task": "detect",
        "evaluation": {"report_digest": content_digest(report)},
    }
    calls = []

    async def read(db, owner, purpose, callback, **kwargs):
        assert owner == saved
        calls.append(kwargs)
        return callback(io.storage_service)

    monkeypatch.setattr(io, "read", read)
    monkeypatch.setattr(training_runs, "verify_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        training_artifacts, "read_json_artifact", lambda *args, **kwargs: report
    )
    await initialization.read_storage(None, pin, metrics, owner=saved, full=True)
    assert calls == [
        {
            "object_key": spec["key"],
            "sha256": spec["sha256"],
            "size_bytes": spec["size_bytes"],
        }
        for spec in (weights, metrics)
    ]


async def test_preview_image_reads_two_objects_and_owns_undelivered_file(monkeypatch):
    from app.api.v1 import training_previews as api
    from app.services import training_previews as previews
    from datetime import datetime, timezone, timedelta

    saved = owner()
    report_spec = {"key": "report.json", "sha256": "a" * 64, "size_bytes": 11}
    image_spec = {"key": "image.png", "sha256": "b" * 64, "size_bytes": 13}
    preview = SimpleNamespace(
        status="succeeded",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        spec={"count": 1},
        artifacts={"report.json": report_spec, "image.png": image_spec},
    )
    output = SimpleNamespace(index=0, image="image.png")
    captured = binary_io.BytesIO(b"png")
    calls = []

    async def read(db, owner, purpose, callback, **kwargs):
        assert owner is saved
        calls.append(kwargs)
        return callback(io.storage_service)

    monkeypatch.setattr(api, "lock_owner", AsyncMock())
    monkeypatch.setattr(api, "load", AsyncMock(return_value=preview))
    monkeypatch.setattr(previews, "storage_owner", lambda value: saved)
    monkeypatch.setattr(
        previews,
        "read_report",
        lambda *args, **kwargs: SimpleNamespace(outputs=[output]),
    )
    monkeypatch.setattr(previews, "read_image", lambda *args, **kwargs: captured)
    monkeypatch.setattr(io, "read", read)
    response = await api.image(
        uuid.uuid4(),
        0,
        project=SimpleNamespace(id=uuid.uuid4()),
        user=object(),
        db=SimpleNamespace(commit=AsyncMock()),
    )
    try:
        assert [
            {key: value for key, value in call.items() if key != "close_result"}
            for call in calls
        ] == [
            {
                "object_key": spec["key"],
                "sha256": spec["sha256"],
                "size_bytes": spec["size_bytes"],
            }
            for spec in (report_spec, image_spec)
        ]
        assert callable(calls[1]["close_result"])
        calls[1]["close_result"](captured)
        assert captured.closed
    finally:
        await response.background()


async def test_mapped_active_learning_png_never_opens_original_write_target(
    monkeypatch,
):
    from app.services import active_learning as learning

    saved, route = owner(), object()
    saved.key = lambda relative: "saved/object"
    sample = {"ordinal": 0, "derived_sha256": "a" * 64}
    context = SimpleNamespace(proof=None, key=None)
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(learning.owners, "resolve", lambda value: saved)
    monkeypatch.setattr(
        learning.originals, "delegated", AsyncMock(return_value=context)
    )
    check = AsyncMock()
    monkeypatch.setattr(learning.originals, "check", check)
    monkeypatch.setattr(learning.originals.access, "check_clock", lambda *args: None)
    resolve = AsyncMock(return_value=route)
    monkeypatch.setattr(io.artifact_reads, "resolve", resolve)
    recheck = AsyncMock()
    monkeypatch.setattr(io.artifact_reads.publications, "recheck", recheck)
    monkeypatch.setattr(
        io, "prepare", AsyncMock(side_effect=AssertionError("A target opened"))
    )
    monkeypatch.setattr(
        learning, "read_retained_png", lambda *args, **kwargs: b"retained PNG"
    )
    seen = []

    async def read(database, owner, purpose, callback, **kwargs):
        assert database is db and owner is saved
        seen.append((purpose, kwargs))
        return callback(io.storage_service)

    monkeypatch.setattr(io, "read", read)
    assert await learning.prepare_image(db, object(), sample) == b"retained PNG"
    assert seen == [
        (
            "active_learning_media",
            {
                "object_key": "saved/object",
                "sha256": "a" * 64,
            },
        )
    ]
    resolve.assert_awaited_once_with(db, saved.ref, "saved/object", "a" * 64)
    assert check.await_count == 2
    recheck.assert_awaited_once_with(db, route)
    db.commit.assert_awaited_once()
