"""Public image responses project typed owner replies without private row access."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from aap_assets_images import contract as c
from aap_assets_images.geometry import build_manifest
from aap_assets_images.policy import ImagePolicy
from aap_assets_images.transport import (
    ImageGenerationReference,
    ImageSourceReference,
    ImageSourceReferences,
)
from app.schemas.image_pyramid import ImagePyramidAssetUrlsRequest
from app.services import image_pyramid as bridge
from app.services import image_source_references as references
from app.services.assets_images import ImageGateway
from app.services.image_pyramid_contract import (
    ImagePyramidFailure,
    ImagePyramidTaskSource,
)


def _source(task_id):
    return c.ImageSource(
        owner=c.ImageOwner(kind="task", id=task_id),
        task_id=task_id,
        project_id=uuid4(),
        dataset_id=None,
        item_id=None,
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity={"etag": "registered", "byte_size": 128},
        source_drifted=False,
        upload_ready=True,
        file_path="images/test.png",
        file_name="test.png",
        file_type="image",
        bucket_kind="legacy",
        file_size=None,
        content_hash=None,
        width=2,
        height=1,
        thumbnail_path=None,
        blurhash=None,
        thumbnail_source_generation=None,
        thumbnail_error=None,
    )


def _summary(**updates):
    return c.ImagePyramidSummary(
        status="ready",
        required=False,
        generation=2,
        building_generation=None,
        building_status=None,
        width=2,
        height=1,
        format="webp",
    ).model_copy(update=updates)


def _reference(source, **updates):
    manifest = c.ImagePyramidManifest.model_validate(
        build_manifest(
            generation=2,
            source_fingerprint="sha256:" + "b" * 64,
            width=2,
            height=1,
            overview_width=2,
            overview_height=1,
            overview_digest="sha256:" + "c" * 64,
            policy=ImagePolicy(),
        )
    )
    return ImageGenerationReference(
        source=source,
        pyramid=_summary(**updates),
        pyramid_source_generation=source.source_generation,
        pyramid_source_snapshot=source.source_snapshot,
        manifest=manifest,
        overview_url="https://images.test/overview.webp",
        expires_in=900,
    )


def _bind(monkeypatch, owner, *, module=bridge):
    @asynccontextmanager
    async def gateway(db):
        yield owner

    monkeypatch.setattr(module, "image_gateway", gateway)


@pytest.mark.asyncio
async def test_ready_manifest_etag_exposes_failed_and_pending_refresh(monkeypatch):
    task_id = uuid4()
    source = _source(task_id)
    current = _reference(source)
    requests = []

    async def generation(request):
        requests.append(request)
        return current

    _bind(monkeypatch, SimpleNamespace(generation_reference=generation))
    api = bridge.AssetsImagePyramidApi(None, actor_id=uuid4())
    task = ImagePyramidTaskSource(task_id, "image")
    first = await api.manifest(task, if_none_match=None)
    assert first.body.status == "ready" and first.body.manifest.generation == 2
    assert (first.body.width, first.body.height) == (2, 1)
    assert first.body.source_snapshot == source.source_snapshot
    assert first.body.profile_version == "pyramid-v1"
    assert first.cache_control == "private, max-age=60"
    unchanged = await api.manifest(task, if_none_match=first.etag)
    assert unchanged.body is None and unchanged.outcome == "not_modified"

    current = _reference(source, error_code="decode_failed", retryable=True)
    failed = await api.manifest(task, if_none_match=first.etag)
    assert failed.body.status == "ready" and failed.body.error_code == "decode_failed"
    assert failed.etag != first.etag and failed.body.overview is not None
    current = _reference(source, building_generation=3, building_status="pending")
    pending = await api.manifest(task, if_none_match=failed.etag)
    assert pending.body.building_generation == 3 and pending.etag != failed.etag
    assert all(
        request.actor_id == api.actor_id and request.task_id == task_id
        for request in requests
    )
    assert all(
        request.expires_in == 900 and request.profile_version == "pyramid-v1"
        for request in requests
    )


def test_same_pixels_do_not_share_etags_across_sources_or_profiles():
    source = _source(uuid4())
    initial = _reference(source)
    replacement = _reference(source.model_copy(update={"source_snapshot": "d" * 64}))
    profile = _reference(source, profile_version="another-profile")
    assert (
        len({bridge._etag(initial), bridge._etag(replacement), bridge._etag(profile)})
        == 3
    )


@pytest.mark.asyncio
async def test_required_missing_summary_survives_without_generation(monkeypatch):
    task_id = uuid4()
    source = _source(task_id)
    missing = _summary(status="missing", generation=None, required=True, retryable=True)

    async def call(operation, request):
        assert operation == "read_image_summaries"
        assert request.profile_version == "pyramid-v1"
        return c.ImageSummaries(
            items=[
                c.ImageSummaryItem(
                    selector=c.ImageSourceSelector(task_id=task_id),
                    source=source,
                    thumbnail_current=False,
                    pyramid=missing,
                )
            ]
        )

    _bind(monkeypatch, SimpleNamespace(call=call))
    api = bridge.AssetsImagePyramidApi(None, actor_id=uuid4())
    result = await api.task_summaries((ImagePyramidTaskSource(task_id, "image"),))
    assert result[task_id].status == "missing" and result[task_id].generation is None
    assert result[task_id].required and result[task_id].retryable
    assert result[task_id].source_snapshot == source.source_snapshot
    assert result[task_id].profile_version == "pyramid-v1"


@pytest.mark.asyncio
async def test_retry_uses_same_operation_and_native_receipt_before_readiness(
    monkeypatch,
):
    task_id, operation, job_id = uuid4(), uuid4(), uuid4()
    source = _source(task_id)
    calls = []

    async def call(name, request):
        calls.append((name, request))
        assert name == "retry_image_media"
        return c.AcceptedImageWork(
            operation_id=operation,
            source=source,
            spec=c.PyramidWorkSpec(expected_source_snapshot=source.source_snapshot),
            work=c.ImageWorkReference(
                job_id=job_id,
                generation=1,
                owner=source.owner,
                source_generation=1,
                kind="pyramid",
                status="pending",
            ),
        )

    _bind(monkeypatch, SimpleNamespace(call=call))
    api = bridge.AssetsImagePyramidApi(None, actor_id=uuid4())
    task = ImagePyramidTaskSource(task_id, "image")
    first = await api.retry(task, operation_id=operation)
    replay = await api.retry(task, operation_id=operation)
    assert first == replay and first.job_id == job_id and first.status == "queued"
    assert [request.operation_id for _, request in calls] == [operation, operation]


@pytest.mark.asyncio
async def test_mixed_generation_asset_batch_is_rejected_before_owner_call(monkeypatch):
    def no_gateway(db):
        raise AssertionError("invalid asset request reached owner")

    monkeypatch.setattr(bridge, "image_gateway", no_gateway)
    api = bridge.AssetsImagePyramidApi(None, actor_id=uuid4())
    payload = ImagePyramidAssetUrlsRequest(
        items=[
            {"kind": "overview", "generation": 1},
            {"kind": "overview", "generation": 2},
        ]
    )
    with pytest.raises(ImagePyramidFailure) as exc:
        await api.asset_urls(ImagePyramidTaskSource(uuid4(), "image"), payload)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_source_reference_failure_never_falls_back_to_raw_pointer(monkeypatch):
    actor = uuid4()
    task_ids = [uuid4() for _ in range(3)]

    async def source_references(batch):
        if any(request.task_id == task_ids[1] for request in batch.requests):
            raise c.ImageFailure("source_changed")
        return ImageSourceReferences(
            references=[
                ImageSourceReference(
                    actor_id=actor,
                    source=_source(
                        task_ids[0]
                        if request.task_id == task_ids[2]
                        else request.task_id
                    ),
                    original_url="https://images.test/source.png",
                    thumbnail_url=None,
                    expires_in=3600,
                )
                for request in batch.requests
            ]
        )

    _bind(
        monkeypatch,
        SimpleNamespace(source_references=source_references),
        module=references,
    )
    result = await references.image_source_references(
        None,
        [
            c.ReadImageSourceRequest(actor_id=actor, task_id=task_id)
            for task_id in task_ids
        ],
    )
    assert result[0].source.task_id == task_ids[0]
    assert result[1:] == [None, None]


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_image_references_batch_pages_without_amplifying_service_outages(
    monkeypatch, unavailable
):
    actor = uuid4()
    task_ids = [uuid4() for _ in range(c.BATCH_SIZE + 1)]
    batches = []

    async def source_references(batch):
        batches.append(len(batch.requests))
        if unavailable:
            raise c.ImageFailure("storage_unavailable")
        return ImageSourceReferences(
            references=[
                ImageSourceReference(
                    actor_id=actor,
                    source=_source(request.task_id),
                    original_url="https://images.test/source.png",
                    thumbnail_url=None,
                    expires_in=3600,
                )
                for request in batch.requests
            ]
        )

    _bind(
        monkeypatch,
        SimpleNamespace(source_references=source_references),
        module=references,
    )
    requests = [
        c.ReadImageSourceRequest(actor_id=actor, task_id=task_id)
        for task_id in task_ids
    ]
    if unavailable:
        with pytest.raises(HTTPException) as exc:
            await references.image_source_references(None, requests)
        assert exc.value.status_code == 503
        assert exc.value.detail == {"reason": "storage_unavailable"}
        assert exc.value.headers == {"Retry-After": "3"}
        assert batches == [c.BATCH_SIZE]
        return
    result = await references.image_source_references(None, requests)
    assert batches == [c.BATCH_SIZE, 1]
    assert len(result) == len(task_ids)
    assert [item.source.task_id for item in result] == task_ids


@pytest.mark.asyncio
async def test_gateway_closes_owned_provider_on_failure():
    closed = []

    async def close():
        closed.append(True)

    with pytest.raises(c.ImageFailure, match="source_changed"):
        async with ImageGateway(SimpleNamespace(close=close)):
            raise c.ImageFailure("source_changed")
    assert closed == [True]


@pytest.mark.asyncio
async def test_task_list_preserves_owner_unavailability_status():
    from app.api.v1.tasks._shared import _attach_image_pyramids_batch

    async def summaries(*args):
        raise ImagePyramidFailure(
            503, {"reason": "storage_unavailable"}, headers={"Retry-After": "3"}
        )

    with pytest.raises(HTTPException) as exc:
        await _attach_image_pyramids_batch(
            SimpleNamespace(task_summaries=summaries),
            [SimpleNamespace(id=uuid4(), file_type="image")],
        )
    assert exc.value.status_code == 503 and exc.value.headers == {"Retry-After": "3"}
