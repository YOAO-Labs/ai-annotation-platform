"""Video consumers accept saved owners and explicit producer authority only."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import HTTPException, Request
import pytest

from app.api.v1 import datasets, storage
from app.api.v1.tasks import _shared as shared
from app.cli.video import rebuild_timetable as cli
from app.services import video_derivative_originals as originals
from app.services import video_derivatives as derivatives
from app.services.content_snapshot import ContentError
from app.services.dataset import DatasetService
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
    await shared._attach_image_pyramids_batch(db, [value], dimensions)
    assert value._video_derivative_thumbnail == (PATH, "owned-url")
    await shared._attach_image_pyramids_batch(db, [value], dimensions)
    assert value._video_derivative_thumbnail is None
    await shared._attach_image_pyramids_batch(db, [value], dimensions)
    assert value._video_derivative_thumbnail is None
    checked.assert_awaited_with(db, task_id=value.id)


@pytest.mark.parametrize(
    "checked,expected",
    [(None, None), (("other.webp", "stale"), None), ((PATH, "owned-url"), "owned-url")],
)
async def test_dataset_list_never_guesses_a_video_thumbnail_bucket(
    monkeypatch, checked, expected
):
    from app.services import dataset as dataset_service, directory_policy

    value = SimpleNamespace(
        id=uuid.uuid4(),
        file_type="video",
        file_path="original.mp4",
        thumbnail_path=PATH,
    )
    count, rows = MagicMock(), MagicMock()
    count.scalar.return_value = 1
    rows.scalars.return_value.all.return_value = [value]
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[count, rows]), commit=AsyncMock()
    )
    monkeypatch.setattr(dataset_service, "_item_dict", lambda _item: {})
    monkeypatch.setattr(directory_policy, "owns_media", AsyncMock(return_value=False))
    checked_mock = AsyncMock(return_value=checked)
    monkeypatch.setattr(derivatives, "thumbnail", checked_mock)
    denied = deny_current_storage(monkeypatch)
    results, total = await DatasetService(db).list_items(uuid.uuid4())
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
    await DatasetService(db).enqueue_media_for_items([item_id])
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
        await DatasetService(db).enqueue_media_for_items([uuid.uuid4()])
    dispatch.assert_not_called()


@pytest.mark.parametrize("blocked", [False, True])
async def test_http_backfill_freezes_exact_request_and_commits_before_dispatch(
    monkeypatch, blocked
):
    from app.services import reference_access

    dataset_id, item_id, authority, actor = (
        uuid.uuid4(),
        uuid.uuid4(),
        object(),
        object(),
    )
    request = Request({"type": "http", "method": "POST", "path": "/backfill"})
    events = []
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=[item_id]),
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
    )
    monkeypatch.setattr(datasets, "recheck_directory_write", AsyncMock())
    monkeypatch.setattr(
        datasets,
        "DatasetService",
        lambda _db: SimpleNamespace(get=AsyncMock(return_value=object())),
    )
    monkeypatch.setattr(reference_access, "reject_reference_scope", AsyncMock())
    authorize = AsyncMock(
        side_effect=ContentError("location_unavailable") if blocked else None,
        return_value=authority,
    )
    monkeypatch.setattr(originals, "authorize_policy", authorize)
    delegation = {"mode": "http", "request": "exact-key-fingerprint"}
    monkeypatch.setattr(originals, "authorization", MagicMock(return_value=delegation))
    dispatch = MagicMock(side_effect=lambda *_a, **_k: events.append("dispatch"))
    monkeypatch.setattr(media.backfill_media, "delay", dispatch)
    if blocked:
        with pytest.raises(HTTPException) as failure:
            await datasets.backfill_media_endpoint(request, dataset_id, db, actor)
        assert failure.value.detail == {"code": "location_unavailable"}
        dispatch.assert_not_called()
        assert events == []
    else:
        await datasets.backfill_media_endpoint(request, dataset_id, db, actor)
        authorize.assert_awaited_once_with(db, "dataset", item_id, actor, request)
        dispatch.assert_called_once_with(
            str(dataset_id), video_delegations={str(item_id): delegation}
        )
        assert events == ["commit", "dispatch"]


async def test_storage_retry_persists_exact_authority_before_dispatch(monkeypatch):
    from app.services import video_asset_retry_jobs

    value = SimpleNamespace(
        id=uuid.uuid4(), file_type="video", video_building_output_id=None
    )
    authority, source, actor = object(), object(), SimpleNamespace(id=uuid.uuid4())
    request = Request({"type": "http", "method": "POST", "path": "/retry"})
    output_id, events = uuid.uuid4(), []
    db = SimpleNamespace(
        get=AsyncMock(return_value=value),
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
    )
    monkeypatch.setattr(storage, "_assert_media_asset_manager", AsyncMock())
    monkeypatch.setattr(
        originals, "authorize_policy", AsyncMock(return_value=authority)
    )
    monkeypatch.setattr(originals, "check", AsyncMock(return_value=source))
    prepare = AsyncMock(return_value=(SimpleNamespace(id=output_id), True))
    monkeypatch.setattr(derivatives, "prepare", prepare)
    job_id = uuid.uuid4()
    observe = AsyncMock(return_value=SimpleNamespace(id=job_id))
    monkeypatch.setattr(video_asset_retry_jobs, "observe", observe)
    dispatch = MagicMock(side_effect=lambda _ids: events.append("dispatch"))
    monkeypatch.setattr(derivatives, "dispatch", dispatch)
    result = await storage.retry_video_asset(
        storage.VideoAssetRetryRequest(asset_type="poster", dataset_item_id=value.id),
        request,
        db,
        actor,
    )
    assert result.status == "queued"
    assert result.job_id == job_id
    observe.assert_awaited_once_with(db, prepare.return_value[0], actor.id, "poster")
    originals.authorize_policy.assert_awaited_once_with(
        db, "storage", value.id, actor, request
    )
    prepare.assert_awaited_once_with(
        db, value.id, authority, "metadata", spec=None, force=True
    )
    dispatch.assert_called_once_with([output_id])
    assert events == ["commit", "dispatch"]


async def test_storage_frame_retry_prepares_metadata_first(monkeypatch):
    value = SimpleNamespace(id=uuid.uuid4(), file_type="video")
    authority, source, actor = object(), object(), object()
    request = Request({"type": "http", "method": "POST", "path": "/retry"})
    events, output_id = [], uuid.uuid4()
    db = SimpleNamespace(
        get=AsyncMock(return_value=value),
        scalar=AsyncMock(return_value=object()),
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
    )
    monkeypatch.setattr(storage, "_assert_media_asset_manager", AsyncMock())
    monkeypatch.setattr(
        originals, "authorize_policy", AsyncMock(return_value=authority)
    )
    monkeypatch.setattr(originals, "check", AsyncMock(return_value=source))
    monkeypatch.setattr(
        derivatives,
        "metadata",
        AsyncMock(side_effect=ContentError("video_metadata_unpinned")),
    )
    prepare = AsyncMock(return_value=(SimpleNamespace(id=output_id), True))
    monkeypatch.setattr(derivatives, "prepare", prepare)
    monkeypatch.setattr(
        derivatives,
        "dispatch",
        MagicMock(side_effect=lambda _ids: events.append("dispatch")),
    )
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
    assert failure.value.detail == {"code": "video_metadata_preparing"}
    prepare.assert_awaited_once_with(db, value.id, authority, "metadata")
    assert events == ["commit", "dispatch"]


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
        get=AsyncMock(side_effect=[value, output]),
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
    )
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
                db, value, sparse_stride=2, storage=object()
            )
            == 2
        )
    else:
        with pytest.raises(ContentError) as failure:
            await cli.rebuild_item_timetable(
                db, value, sparse_stride=2, storage=object()
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
    assert await cli.rebuild_item_timetable(db, value, dry_run=True) == 0
    prepare.assert_not_awaited()
    run.assert_not_awaited()
    db.commit.assert_not_awaited()
    denied.assert_not_called()


async def test_video_catalog_validates_original_and_poster_without_current_bucket_fallback(
    monkeypatch,
):
    from app.services.screenshot_seed_catalog import verified_video_seed_digest

    value, authority, db = (
        SimpleNamespace(id=uuid.uuid4(), thumbnail_path=PATH),
        object(),
        object(),
    )
    source = SimpleNamespace(sha256="a" * 64)
    events = []
    monkeypatch.setattr(originals, "pin", AsyncMock(return_value=source))
    monkeypatch.setattr(
        originals, "system_authorization", MagicMock(return_value={"mode": "system"})
    )
    monkeypatch.setattr(originals, "delegated", AsyncMock(return_value=authority))

    @asynccontextmanager
    async def checked_media(actual_db, actual_authority, actual_source):
        assert (actual_db, actual_authority, actual_source) == (db, authority, source)
        events.append("verified-open")
        yield object()
        events.append("drained-close")

    monkeypatch.setattr(originals, "media", checked_media)
    thumbnail = AsyncMock(return_value=(PATH, "saved-url"))
    monkeypatch.setattr(derivatives, "thumbnail", thumbnail)
    denied = deny_current_storage(monkeypatch)
    assert await verified_video_seed_digest(db, value) == source.sha256
    assert events == ["verified-open", "drained-close"]
    thumbnail.assert_awaited_once_with(db, item_id=value.id)
    denied.assert_not_called()


async def test_storage_project_denial_precedes_any_original_authorization(monkeypatch):
    value, request, actor = (
        SimpleNamespace(id=uuid.uuid4(), file_type="video"),
        object(),
        object(),
    )
    db = SimpleNamespace(get=AsyncMock(return_value=value))
    monkeypatch.setattr(
        storage,
        "_assert_media_asset_manager",
        AsyncMock(side_effect=HTTPException(403, "manager required")),
    )
    authorize, dispatch = AsyncMock(), MagicMock()
    monkeypatch.setattr(originals, "authorize_policy", authorize)
    monkeypatch.setattr(derivatives, "dispatch", dispatch)
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
    dispatch.assert_not_called()


async def test_fresh_video_seed_registers_only_actual_put_bytes_and_defers_dispatch(
    monkeypatch, tmp_path
):
    import hashlib
    from app.db.models.dataset import DatasetItem
    from app.services import dataset as dataset_service, dataset_assets
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
        dataset_service, "build_tasks_for_link", AsyncMock(return_value=1)
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
