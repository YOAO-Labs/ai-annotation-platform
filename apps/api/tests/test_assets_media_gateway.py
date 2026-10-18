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
