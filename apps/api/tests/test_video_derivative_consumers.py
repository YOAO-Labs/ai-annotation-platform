"""Video consumers accept saved owners and explicit producer authority only."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import HTTPException, Request
import pytest

from app.services.dataset_item_display import list_items
from app.api.v1 import storage
from app.api.v1.tasks import _shared as shared
from app.application.dataset_ingest import enqueue_media_for_items
from app.cli.video import rebuild_timetable as cli
from app.services import video_derivative_originals as originals
from app.services import video_derivatives as derivatives
from app.services.content_snapshot import ContentError
from app.services.storage import storage_service
from app.workers import media, media_derivatives


PATH = "video-derivatives/objects/owner/poster.webp"


def task():
    return SimpleNamespace(
        id=uuid.uuid4(),
        dataset_item_id=uuid.uuid4(),
        file_type="video",
        file_path="videos/original.mp4",
    )


def deny_current_storage(monkeypatch):
    denied = MagicMock(
        side_effect=AssertionError("current-bucket video I/O is forbidden")
    )
    for method in ("generate_download_url", "bucket_for_cache_key", "verify_upload"):
        monkeypatch.setattr(storage_service, method, denied)
    return denied


@pytest.mark.parametrize("checked", [None, ("other-poster.webp", "stale-url")])
def test_task_serializer_never_signs_unknown_or_stale_video_poster(
    monkeypatch, checked
):
    value = task()
    value._video_derivative_thumbnail = checked
    value._image_pyramid_thumbnail = (PATH, "unrelated-pyramid-url")
    monkeypatch.setattr(
        shared.TaskOut, "model_validate", lambda *_a, **_k: SimpleNamespace()
    )
    denied = deny_current_storage(monkeypatch)
    assert shared._task_with_url(value, thumbnail_path=PATH).thumbnail_url is None
    denied.assert_not_called()


def test_task_serializer_consumes_only_the_verified_poster_pair(monkeypatch):
    value = task()
    value._video_derivative_thumbnail = (PATH, "saved-owner-url")
    monkeypatch.setattr(
        shared.TaskOut, "model_validate", lambda *_a, **_k: SimpleNamespace()
    )
    denied = deny_current_storage(monkeypatch)
    assert (
        shared._task_with_url(value, thumbnail_path=PATH).thumbnail_url
        == "saved-owner-url"
    )
    denied.assert_not_called()


async def test_task_batch_clears_previous_poster_and_rejects_a_different_path(
    monkeypatch,
):
    from app.services import image_pyramid

    value, db = task(), object()
    checked = AsyncMock(
        side_effect=[(PATH, "owned-url"), None, ("other.webp", "stale")]
    )
    monkeypatch.setattr(derivatives, "thumbnail", checked)
    monkeypatch.setattr(
        image_pyramid, "task_pyramid_assets", AsyncMock(return_value={})
    )
    dimensions = {value.id: (320, 240, PATH, None, None)}
    actor_id = uuid.uuid4()
    await shared._attach_image_pyramids_batch(
        db, [value], dimensions, actor_id=actor_id
    )
    assert value._video_derivative_thumbnail == (PATH, "owned-url")
    await shared._attach_image_pyramids_batch(
        db, [value], dimensions, actor_id=actor_id
    )
    assert value._video_derivative_thumbnail is None
    await shared._attach_image_pyramids_batch(
        db, [value], dimensions, actor_id=actor_id
    )
    assert value._video_derivative_thumbnail is None
    checked.assert_awaited_with(db, task_id=value.id)


@pytest.mark.parametrize(
    "checked,expected",
    [(None, None), (("other.webp", "stale"), None), ((PATH, "owned-url"), "owned-url")],
)
async def test_dataset_list_never_guesses_a_video_thumbnail_bucket(
    monkeypatch, checked, expected
):
    from app.services import dataset_item_display as dataset_service, directory_policy

    value = SimpleNamespace(
        id=uuid.uuid4(),
        file_type="video",
        file_path="original.mp4",
        thumbnail_path=PATH,
        directory_owned=False,
    )
    db = SimpleNamespace(
        autoflush=False,
        scalar=AsyncMock(return_value=1),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [value])),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(dataset_service, "_item_dict", lambda _item: {})
    monkeypatch.setattr(directory_policy, "owns_media", AsyncMock(return_value=False))
    checked_mock = AsyncMock(return_value=checked)
    monkeypatch.setattr(derivatives, "thumbnail", checked_mock)
    denied = deny_current_storage(monkeypatch)
    results, total = await list_items(db, uuid.uuid4())
    assert total == 1
    assert results[0]["thumbnail_url"] == expected
    checked_mock.assert_awaited_once_with(db, item_id=value.id)
    denied.assert_not_called()
    db.commit.assert_not_awaited()


async def test_import_dispatch_uses_an_explicit_pinned_delegation_without_committing_caller(
    monkeypatch,
):
    from app.services import reference_access

    item_id, source = uuid.uuid4(), object()
    rows = MagicMock()
    rows.all.return_value = [(item_id, "video")]
    db = SimpleNamespace(execute=AsyncMock(return_value=rows), commit=AsyncMock())
    monkeypatch.setattr(reference_access, "reject_reference_scope", AsyncMock())
    monkeypatch.setattr(originals, "pin", AsyncMock(return_value=source))
    delegation = {"mode": "system", "original": "frozen-exact-source"}
    encode = MagicMock(return_value=delegation)
    monkeypatch.setattr(originals, "system_authorization", encode)
    dispatch = MagicMock()
    monkeypatch.setattr(media.generate_video_metadata, "delay", dispatch)
    await enqueue_media_for_items(db, [item_id])
    originals.pin.assert_awaited_once_with(db, "dataset_item", item_id)
    encode.assert_called_once_with("dataset_item", item_id, source)
    dispatch.assert_called_once_with(str(item_id), delegation=delegation)
    db.commit.assert_not_awaited()


async def test_import_unknown_original_cannot_dispatch(monkeypatch):
    from app.services import reference_access

    rows = MagicMock()
    rows.all.return_value = [(uuid.uuid4(), "video")]
    db = SimpleNamespace(execute=AsyncMock(return_value=rows))
    monkeypatch.setattr(reference_access, "reject_reference_scope", AsyncMock())
    monkeypatch.setattr(
        originals, "pin", AsyncMock(side_effect=ContentError("content_unverified"))
    )
    dispatch = MagicMock()
    monkeypatch.setattr(media.generate_video_metadata, "delay", dispatch)
    with pytest.raises(ContentError, match="content_unverified"):
        await enqueue_media_for_items(db, [uuid.uuid4()])
    dispatch.assert_not_called()


async def test_storage_retry_persists_exact_authority_before_dispatch(monkeypatch):
    from contextlib import asynccontextmanager

    value = SimpleNamespace(id=uuid.uuid4(), file_type="video")
    actor = SimpleNamespace(id=uuid.uuid4())
    request = Request({"type": "http", "method": "POST", "path": "/retry"})
    job_id, events, calls = uuid.uuid4(), [], []
    db = SimpleNamespace(
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
    )

    class FakeGateway:
        async def call(self, operation, media_request):
            events.append("gateway")
            calls.append((operation, media_request))
            return SimpleNamespace(work=SimpleNamespace(job_id=job_id))

    @asynccontextmanager
    async def gateway_factory(actual_db):
        assert actual_db is db
        yield FakeGateway()

    monkeypatch.setattr(storage, "media_gateway", gateway_factory)
    result = await storage.retry_video_asset(
        storage.VideoAssetRetryRequest(asset_type="poster", dataset_item_id=value.id),
        request,
        db,
        actor,
    )
    assert result.status == "queued"
    assert result.job_id == job_id
    assert len(calls) == 1
    operation, media_request = calls[0]
    assert operation == "retry_video_media"
    assert media_request.actor_id == actor.id
    assert media_request.spec.asset_type == "poster"
    assert media_request.spec.dataset_item_id == value.id
    assert events == ["gateway", "commit"]


async def test_storage_frame_retry_prepares_metadata_first(monkeypatch):
    from contextlib import asynccontextmanager
    from yoao_assets_media.contract import MediaFailure

    value = SimpleNamespace(id=uuid.uuid4(), file_type="video")
    actor = SimpleNamespace(id=uuid.uuid4())
    request = Request({"type": "http", "method": "POST", "path": "/retry"})
    calls = []
    db = SimpleNamespace(commit=AsyncMock())

    class FakeGateway:
        async def call(self, operation, media_request):
            calls.append((operation, media_request))
            raise MediaFailure("metadata_not_ready")

    @asynccontextmanager
    async def gateway_factory(actual_db):
        assert actual_db is db
        yield FakeGateway()

    monkeypatch.setattr(storage, "media_gateway", gateway_factory)
    payload = storage.VideoAssetRetryRequest(
        asset_type="frame",
        dataset_item_id=value.id,
        frame_index=2,
        width=320,
        format="webp",
    )
    with pytest.raises(HTTPException) as failure:
        await storage.retry_video_asset(payload, request, db, actor)
    assert failure.value.status_code == 503
    assert failure.value.detail == "Video metadata not ready"
    assert len(calls) == 1
    assert calls[0][0] == "retry_video_media"
    assert calls[0][1].spec.frame_index == 2
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("published", [True, False])
async def test_cli_rebuild_uses_committed_new_generation_and_reports_only_real_publication(
    monkeypatch, published
):
    output_id, item_id = uuid.uuid4(), uuid.uuid4()
    value = SimpleNamespace(
        id=item_id, video_output_id=output_id if published else None
    )
    output = SimpleNamespace(id=output_id)
    source, authority, delegation, events = object(), object(), {"mode": "system"}, []
    db = SimpleNamespace(
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
    )
    monkeypatch.setattr(derivatives, "item_row", AsyncMock(return_value=value))
    monkeypatch.setattr(cli.objects, "read_attempt", AsyncMock(return_value=output))
    monkeypatch.setattr(originals, "pin", AsyncMock(return_value=source))
    monkeypatch.setattr(
        originals, "system_authorization", MagicMock(return_value=delegation)
    )
    monkeypatch.setattr(originals, "delegated", AsyncMock(return_value=authority))
    monkeypatch.setattr(originals, "check", AsyncMock(return_value=source))
    prepare = AsyncMock(return_value=(output, True))
    monkeypatch.setattr(derivatives, "prepare", prepare)
    run = AsyncMock(side_effect=lambda _ids: events.append("run"))
    monkeypatch.setattr(media_derivatives, "run_outputs", run)
    monkeypatch.setattr(derivatives, "metadata", AsyncMock(return_value=output))
    frames = [{"frame_index": i, "is_keyframe": i == 0} for i in range(3)]
    monkeypatch.setattr(
        derivatives,
        "metadata_result",
        lambda _output: (SimpleNamespace(frame_timetable_error=None), frames),
    )
    denied = deny_current_storage(monkeypatch)
    if published:
        assert (
            await cli.rebuild_item_timetable(
                db, item_id, sparse_stride=2, storage=object()
            )
            == 2
        )
    else:
        with pytest.raises(ContentError) as failure:
            await cli.rebuild_item_timetable(
                db, item_id, sparse_stride=2, storage=object()
            )
        assert failure.value.code == "video_metadata_rebuild_failed"
    prepare.assert_awaited_once_with(
        db,
        item_id,
        authority,
        "metadata",
        spec={"schema": 1, "timetable_stride": 2},
        force=True,
    )
    run.assert_awaited_once_with([str(output_id)])
    assert events == ["commit", "run"]
    denied.assert_not_called()


async def test_cli_dry_run_is_metadata_only_and_never_dispatches(monkeypatch):
    value, source, authority = SimpleNamespace(id=uuid.uuid4()), object(), object()
    monkeypatch.setattr(originals, "pin", AsyncMock(return_value=source))
    monkeypatch.setattr(
        originals, "system_authorization", MagicMock(return_value={"mode": "system"})
    )
    monkeypatch.setattr(originals, "delegated", AsyncMock(return_value=authority))
    monkeypatch.setattr(derivatives, "item_row", AsyncMock(return_value=value))
    monkeypatch.setattr(
        derivatives,
        "metadata",
        AsyncMock(side_effect=ContentError("video_metadata_unpinned")),
    )
    prepare, run = AsyncMock(), AsyncMock()
    monkeypatch.setattr(derivatives, "prepare", prepare)
    monkeypatch.setattr(media_derivatives, "run_outputs", run)
    db = SimpleNamespace(commit=AsyncMock())
    denied = deny_current_storage(monkeypatch)
    assert await cli.rebuild_item_timetable(db, value.id, dry_run=True) == 0
    prepare.assert_not_awaited()
    run.assert_not_awaited()
    db.commit.assert_not_awaited()
    denied.assert_not_called()


async def test_storage_project_denial_precedes_any_original_authorization(monkeypatch):
    from contextlib import asynccontextmanager
    from yoao_assets_media.contract import MediaFailure

    value, request, actor = (
        SimpleNamespace(id=uuid.uuid4(), file_type="video"),
        object(),
        SimpleNamespace(id=uuid.uuid4()),
    )
    db = SimpleNamespace(commit=AsyncMock())

    class FakeGateway:
        async def call(self, _operation, _media_request):
            raise MediaFailure("permission_denied")

    @asynccontextmanager
    async def gateway_factory(actual_db):
        assert actual_db is db
        yield FakeGateway()

    monkeypatch.setattr(storage, "media_gateway", gateway_factory)
    authorize = AsyncMock()
    monkeypatch.setattr(originals, "authorize_policy", authorize)
    with pytest.raises(HTTPException) as failure:
        await storage.retry_video_asset(
            storage.VideoAssetRetryRequest(
                asset_type="poster", dataset_item_id=value.id
            ),
            request,
            db,
            actor,
        )
    assert failure.value.status_code == 403
    authorize.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_fresh_video_seed_registers_only_actual_put_bytes_and_defers_dispatch(
    monkeypatch, tmp_path
):
    import hashlib
    from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
    from app.db.models.dataset import DatasetItem
    from app.services import dataset_assets, workspace_seed_data
    from scripts import seed_video as seed

    payload = b"known bytes from this seed PUT"
    local = tmp_path / "fixture.mp4"
    local.write_bytes(payload)
    binding = SimpleNamespace(bucket="saved-upload-bucket")
    source, delegation, events, added = (
        object(),
        {"mode": "system", "original": "known-put"},
        [],
        [],
    )
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.commit = AsyncMock()
    db.add.side_effect = added.append

    async def flush():
        for value in added:
            if hasattr(value, "id") and value.id is None:
                value.id = uuid.uuid4()

    db.flush = AsyncMock(side_effect=flush)
    monkeypatch.setattr(
        workspace_seed_data,
        "create_media_seed_project",
        AsyncMock(return_value=uuid.uuid4()),
    )
    monkeypatch.setattr(
        dataset_assets, "ensure_legacy_bindings", AsyncMock(return_value=binding)
    )

    def put(**kwargs):
        assert kwargs["Bucket"] == binding.bucket
        assert kwargs["Body"] == payload
        events.append("put")
        return {"ETag": '"actual-put-etag"'}

    monkeypatch.setattr(storage_service.client, "put_object", put)
    monkeypatch.setattr(
        media,
        "probe_video_file",
        lambda _path: {"width": 320, "height": 240, "fps": 30, "frame_count": 3},
    )
    monkeypatch.setattr(
        DatasetTaskCreationWorkflow,
        "create_tasks_for_linked_dataset",
        AsyncMock(return_value={"created": 1, "total": 1}),
    )

    async def register(actual_db, value, **kwargs):
        assert actual_db is db
        assert events == ["put"]
        assert kwargs == {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
            "etag": "actual-put-etag",
            "binding": binding,
        }
        assert value.metadata_ == {}
        events.append("registered")

    monkeypatch.setattr(
        dataset_assets, "register_item_asset", AsyncMock(side_effect=register)
    )
    monkeypatch.setattr(originals, "pin", AsyncMock(return_value=source))
    monkeypatch.setattr(
        originals, "system_authorization", MagicMock(return_value=delegation)
    )
    dispatch = MagicMock()
    monkeypatch.setattr(media.generate_video_metadata, "delay", dispatch)
    result = await seed.seed_video(db, owner_id=uuid.uuid4(), video=local)
    value = next(value for value in added if isinstance(value, DatasetItem))
    assert result["item_id"] == str(value.id)
    assert result["video_delegation"] == delegation
    assert events == ["put", "registered"]
    originals.pin.assert_awaited_once_with(db, "dataset_item", value.id)
    originals.system_authorization.assert_called_once_with(
        "dataset_item", value.id, source
    )
    dispatch.assert_not_called()
    db.commit.assert_not_awaited()
