from __future__ import annotations

from uuid import uuid4

import pytest

from aap_assets_media import contract as c
from aap_assets_media.transport import MediaReferences
from app.services import assets_media


def source():
    return c.MediaSource(
        dataset_item_id=uuid4(),
        dataset_id=uuid4(),
        source_generation=1,
        source_snapshot="a" * 64,
        timetable_revision=1,
        materialized_generation=1,
        object_identity=None,
        source_drifted=False,
        file_name="clip.mp4",
        file_path="registered/clip.mp4",
        file_type="video",
        file_size=12,
        content_hash=None,
        width=64,
        height=48,
        thumbnail_path=None,
        blurhash=None,
        video_metadata={"frame_count": 2, "fps": 2},
    )


@pytest.mark.asyncio
async def test_waiter_admits_once_and_reuses_one_executor_until_gateway_closes(
    monkeypatch,
):
    s = source()
    metadata_job, frame_job = uuid4(), uuid4()
    admissions, execution, closed = [], [], []

    class Executor:
        async def execute_once(self, job_id):
            execution.append(job_id)
            return True

        async def close(self):
            closed.append(True)

    executor = Executor()
    monkeypatch.setattr(assets_media, "_executor", lambda: executor)

    class Provider:
        reads = 0

        async def internal_frame_reference(self, request):
            self.reads += 1
            current = (
                s
                if self.reads > 1
                else s.model_copy(update={"materialized_generation": 0})
            )
            assets = []
            if self.reads > 1:
                assets = [
                    c.MediaAsset(
                        row_id=uuid4(),
                        dataset_item_id=s.dataset_item_id,
                        kind="frame",
                        source_generation=1,
                        timetable_revision=1,
                        status="ready" if self.reads > 2 else "pending",
                        storage_key="media-assets/frame.webp"
                        if self.reads > 2
                        else None,
                        frame_index=0,
                        width=512,
                        format="webp",
                        job_id=frame_job,
                        generation=1,
                        url="http://s3.local/frame.webp" if self.reads > 2 else None,
                    )
                ]
            return MediaReferences(
                state=c.MediaAssets(source=current, assets=assets),
                video_url="http://s3.local/source.mp4",
                expires_in=3600,
            )

        async def call(self, operation, request):
            if operation == "schedule_video_media":
                admissions.append(request)
                return c.AcceptedWork(
                    operation_id=None,
                    source=s,
                    spec=request.spec,
                    work=c.WorkReference(
                        job_id=metadata_job,
                        generation=1,
                        dataset_item_id=s.dataset_item_id,
                        source_generation=1,
                        kind="video_metadata",
                        status="pending",
                    ),
                )
            assert operation == "read_video_work"
            kind = "video_metadata" if request.job_id == metadata_job else "frame"
            return c.WorkStatus(
                work=c.WorkReference(
                    job_id=request.job_id,
                    generation=1,
                    dataset_item_id=s.dataset_item_id,
                    source_generation=1,
                    kind=kind,
                    status="pending",
                ),
                spec=c.MetadataSpec()
                if kind == "video_metadata"
                else c.FrameSpec(frame_index=0),
                progress={},
                error_code=None,
                error_detail=None,
                lease_expires_at=None,
            )

    async with assets_media.MediaGateway(Provider()) as gateway:
        result = await gateway.ensure_internal_frame(
            c.InternalFrameRequest(dataset_item_id=s.dataset_item_id, frame_index=0)
        )
        assert result.state.assets[0].status == "ready"
        assert closed == []
    assert closed == [True]
    assert execution == [metadata_job, frame_job]
    assert len(admissions) == 1
    assert admissions[0].operation_id is None


@pytest.mark.asyncio
async def test_caller_transaction_cannot_wait_or_sign_references():
    gateway = assets_media.MediaGateway(None, caller_transaction=True)
    with pytest.raises(c.MediaFailure, match="invalid_request"):
        await gateway.ensure_internal_frame(
            c.InternalFrameRequest(dataset_item_id=uuid4(), frame_index=0)
        )
    with pytest.raises(c.MediaFailure, match="invalid_request"):
        gateway.source_reference


@pytest.mark.asyncio
async def test_selected_remote_provider_never_runs_local_executor(monkeypatch):
    monkeypatch.setattr(assets_media.settings, "assets_media_url", "http://media.local")
    monkeypatch.setattr(
        assets_media,
        "_executor",
        lambda: pytest.fail("remote execution used local worker"),
    )
    assert await assets_media.run_media_job(uuid4()) is False
    assert await assets_media.maintain_video_media() is None


@pytest.mark.asyncio
async def test_remote_cli_dry_run_never_constructs_api_s3_storage(monkeypatch):
    from aap_assets_media_runtime import processing
    from aap_assets_media_runtime.http_source import HttpMediaSourceStorage

    monkeypatch.setattr(assets_media.settings, "assets_media_url", "http://media.local")
    monkeypatch.setattr(assets_media.settings, "assets_media_token", "t" * 32)
    monkeypatch.setattr(
        assets_media,
        "media_storage",
        lambda: pytest.fail("remote CLI used API-local S3 credentials"),
    )

    async def probe(current, storage, **kwargs):
        assert isinstance(storage, HttpMediaSourceStorage)
        assert kwargs["sparse_stride"] == 3
        return []

    monkeypatch.setattr(processing, "probe_source_timetable", probe)
    assert await assets_media.probe_video_timetable(source(), sparse_stride=3) == []


@pytest.mark.asyncio
async def test_local_reference_batches_split_at_provider_byte_budget():
    requests = [
        c.ReadSourceRequest(actor_id=uuid4(), dataset_item_id=uuid4()) for _ in range(4)
    ]
    calls = []

    class Provider:
        async def source_references(self, batch, *, raw_only):
            assert raw_only is True
            calls.append(len(batch))
            if len(batch) > 2:
                raise c.MediaFailure("resource_budget_exceeded")
            return [request.dataset_item_id for request in batch]

    async with assets_media.MediaGateway(Provider()) as gateway:
        result = await gateway.source_references(requests, raw_only=True)
    assert result == [request.dataset_item_id for request in requests]
    assert calls == [4, 2, 2]


@pytest.mark.asyncio
async def test_reference_singleton_budget_failure_is_not_retried():
    class Provider:
        calls = 0

        async def source_references(self, requests, *, raw_only):
            self.calls += 1
            raise c.MediaFailure("resource_budget_exceeded")

    provider = Provider()
    async with assets_media.MediaGateway(provider) as gateway:
        with pytest.raises(c.MediaFailure, match="resource_budget_exceeded"):
            await gateway.source_references(
                [c.ReadSourceRequest(actor_id=uuid4(), dataset_item_id=uuid4())]
            )
    assert provider.calls == 1


def test_local_browser_proxy_is_applied_only_at_response_edges(monkeypatch):
    monkeypatch.setattr(assets_media.settings, "assets_media_url", "")
    monkeypatch.setattr(assets_media.settings, "minio_public_url", "/minio")
    monkeypatch.setattr(assets_media.settings, "minio_endpoint", "localhost:9000")
    monkeypatch.setattr(assets_media.settings, "minio_use_ssl", False)
    url = "http://localhost:9000/cache/frame.webp?X-Amz-Signature=untouched"
    storage = assets_media.media_storage()
    assert storage._signer.meta.endpoint_url == "http://localhost:9000"
    assert (
        assets_media.browser_media_url(url)
        == "/minio/cache/frame.webp?X-Amz-Signature=untouched"
    )
    other = "https://objects.example/cache/frame.webp?X-Amz-Signature=untouched"
    assert assets_media.browser_media_url(other) == other
    monkeypatch.setattr(
        assets_media.settings, "assets_media_url", "http://media.example"
    )
    assert assets_media.browser_media_url(url) == url


@pytest.mark.asyncio
async def test_internal_frame_waiter_retries_busy_persistence_without_new_admission():
    s = source()

    class Provider:
        calls = 0

        async def internal_frame_reference(self, request):
            self.calls += 1
            if self.calls <= 2:
                raise c.MediaFailure("operation_busy")
            return MediaReferences(
                state=c.MediaAssets(
                    source=s,
                    assets=[
                        c.MediaAsset(
                            row_id=uuid4(),
                            dataset_item_id=s.dataset_item_id,
                            kind="frame",
                            source_generation=1,
                            timetable_revision=1,
                            status="ready",
                            storage_key="media-assets/frame.webp",
                            frame_index=0,
                            width=512,
                            format="webp",
                            url="http://s3.local/frame.webp",
                        )
                    ],
                ),
                video_url="http://s3.local/source.mp4",
                expires_in=3600,
            )

        async def call(self, operation, request):
            pytest.fail("busy persistence caused a replacement admission")

    provider = Provider()
    async with assets_media.MediaGateway(provider, local_execution=False) as gateway:
        result = await gateway.ensure_internal_frame(
            c.InternalFrameRequest(dataset_item_id=s.dataset_item_id, frame_index=0),
            timeout_seconds=5,
        )
    assert result.state.assets[0].status == "ready"
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_internal_frame_waiter_busy_exhaustion_is_bounded():
    class Provider:
        async def internal_frame_reference(self, request):
            raise c.MediaFailure("source_busy")

    async with assets_media.MediaGateway(Provider(), local_execution=False) as gateway:
        with pytest.raises(c.MediaFailure, match="source_busy"):
            await gateway.ensure_internal_frame(
                c.InternalFrameRequest(dataset_item_id=uuid4(), frame_index=0),
                timeout_seconds=0.01,
            )


@pytest.mark.asyncio
async def test_frame_media_contention_reaches_segment_retry_but_does_not_break_warmup(
    monkeypatch,
):
    from app.workers import frame_preannotate

    class Provider:
        async def ensure_internal_frame(self, request):
            raise c.MediaFailure("operation_busy")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(assets_media, "media_gateway", lambda *args: Provider())
    with pytest.raises(c.MediaFailure, match="operation_busy"):
        await frame_preannotate._read_frame_urls(None, uuid4(), [2])
    assert (
        await frame_preannotate._extract_frames_for_video(
            None, uuid4(), [2], job_id=None
        )
        == {}
    )
