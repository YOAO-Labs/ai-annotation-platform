from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from aap_assets_media.contract import (
    AcceptedWork,
    AssetsRequest,
    BackfillPage,
    FrameSelection,
    InternalSourceRequest,
    MediaAsset,
    MediaAssets,
    MediaFailure,
    MediaSource,
    ScheduleWorkRequest,
    TimetableSpec,
    WorkReference,
    WorkStatus,
    WorkStatusRequest,
)
from app.application.video_timetable_rebuild import VideoTimetableRebuildWorkflow
from app.api.v1 import files as files_api
from app.api.v1.tasks._shared import _linked_source_references, _task_with_url
from app.services.video_context import VideoContext
from app.services.video_frame_service import AssetsVideoContextReader, retry_frames
from app.schemas.task import VideoMetadata
from aap_assets_media.transport import MediaReferences, SourceReference
from app.workers import media as media_workers
from app.workers.celery_app import celery_app


def _source(item_id: UUID, dataset_id: UUID) -> MediaSource:
    return MediaSource(
        dataset_item_id=item_id,
        dataset_id=dataset_id,
        source_generation=1,
        source_snapshot="a" * 64,
        timetable_revision=0,
        materialized_generation=0,
        object_identity=None,
        source_drifted=False,
        file_name=f"{item_id}.mp4",
        file_path=f"media/{item_id}.mp4",
        file_type="video",
        file_size=None,
        content_hash=None,
        width=None,
        height=None,
        thumbnail_path=None,
        blurhash=None,
        video_metadata={},
    )


class _GatewayContext:
    def __init__(self, call):
        self.call = call

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_timetable_selection_validates_complete_owner_pages():
    dataset_id = uuid4()
    item_ids = sorted((uuid4(), uuid4()), key=str)
    sources = [_source(item_id, dataset_id) for item_id in item_ids]
    requests = []

    async def call(operation, request):
        assert operation == "scan_video_backfill"
        requests.append(request)
        if request.after_item_id is None:
            return BackfillPage(
                items=[sources[0]],
                snapshot_digest="b" * 64,
                total=2,
                accepted_before=request.selection.accepted_before,
                after_item_id=None,
                next_item_id=item_ids[0],
                complete=False,
            )
        return BackfillPage(
            items=[sources[1]],
            snapshot_digest="b" * 64,
            total=2,
            accepted_before=request.selection.accepted_before,
            after_item_id=request.after_item_id,
            next_item_id=None,
            complete=True,
        )

    workflow = VideoTimetableRebuildWorkflow(
        gateway_factory=lambda: _GatewayContext(call)
    )
    selected = await workflow.select_items(
        dataset_item_ids=(), dataset_id=dataset_id, all_items=False, limit=None
    )

    assert [source.dataset_item_id for source in selected] == item_ids
    assert len(requests) == 2
    assert requests[0].selection.only_missing is False
    assert (
        requests[0].selection.accepted_before == requests[1].selection.accepted_before
    )
    assert requests[1].after_item_id == item_ids[0]


@pytest.mark.asyncio
async def test_timetable_selection_rejects_page_scope_change():
    dataset_id = uuid4()
    item_id = uuid4()
    source = _source(item_id, dataset_id)

    async def call(_operation, request):
        return BackfillPage(
            items=[source],
            snapshot_digest="c" * 64,
            total=1,
            accepted_before=request.selection.accepted_before,
            after_item_id=uuid4(),
            next_item_id=None,
            complete=True,
        )

    workflow = VideoTimetableRebuildWorkflow(
        gateway_factory=lambda: _GatewayContext(call)
    )
    with pytest.raises(MediaFailure):
        await workflow.select_items(
            dataset_item_ids=(), dataset_id=dataset_id, all_items=False, limit=None
        )


@pytest.mark.asyncio
async def test_rebuild_job_binds_source_snapshot_and_returns_owner_count(monkeypatch):
    dataset_id = uuid4()
    source = _source(uuid4(), dataset_id)
    job_id = uuid4()
    captured = []

    async def call(operation, request):
        captured.append((operation, request))
        if operation == "schedule_video_media":
            return AcceptedWork(
                operation_id=request.operation_id,
                source=source,
                work=WorkReference(
                    job_id=job_id,
                    generation=1,
                    dataset_item_id=source.dataset_item_id,
                    source_generation=source.source_generation,
                    kind="video_timetable",
                    status="pending",
                ),
                spec=request.spec,
            )
        assert operation == "read_video_work"
        assert isinstance(request, WorkStatusRequest)
        return WorkStatus(
            work=WorkReference(
                job_id=job_id,
                generation=1,
                dataset_item_id=source.dataset_item_id,
                source_generation=source.source_generation,
                kind="video_timetable",
                status="ready",
            ),
            spec=TimetableSpec(
                sparse_stride=3,
                expected_source_snapshot=source.source_snapshot,
            ),
            progress={"timetable_rows": 11},
            error_code=None,
            error_detail=None,
            lease_expires_at=None,
        )

    monkeypatch.setattr(
        "app.application.video_timetable_rebuild.run_media_job",
        lambda _job_id: _false_async(),
    )
    workflow = VideoTimetableRebuildWorkflow(
        gateway_factory=lambda: _GatewayContext(call)
    )
    result = await workflow.rebuild_item(source, sparse_stride=3)

    operation, request = captured[0]
    assert operation == "schedule_video_media"
    assert isinstance(request, ScheduleWorkRequest)
    assert request.force is True
    assert request.spec.expected_source_snapshot == source.source_snapshot
    assert result == 11


async def _false_async() -> bool:
    return False


@pytest.mark.asyncio
async def test_worker_drain_reuses_one_executor_and_schedules_continuation(monkeypatch):
    job_id = uuid4()
    calls = []
    context_events = []

    class FakeExecutor:
        async def execute_once(self, *, job_id):
            calls.append(job_id)
            return True

    executor = FakeExecutor()

    @asynccontextmanager
    async def session():
        context_events.append("enter")
        yield executor
        context_events.append("exit")

    continuations = []
    monkeypatch.setattr(media_workers, "media_execution_session", session)
    monkeypatch.setattr(media_workers, "VIDEO_MEDIA_DRAIN_LIMIT", 3)
    monkeypatch.setattr(
        media_workers.execute_video_media,
        "delay",
        lambda value=None: continuations.append(value),
    )

    await media_workers._execute_video_media(str(job_id))

    assert calls == [job_id, None, None]
    assert context_events == ["enter", "exit"]
    assert continuations == [None]


@pytest.mark.asyncio
async def test_worker_drain_stops_on_empty_claim_without_continuation(monkeypatch):
    calls = []
    continuations = []

    class FakeExecutor:
        async def execute_once(self, *, job_id):
            calls.append(job_id)
            return False

    @asynccontextmanager
    async def session():
        yield FakeExecutor()

    monkeypatch.setattr(media_workers, "media_execution_session", session)
    monkeypatch.setattr(
        media_workers.execute_video_media,
        "delay",
        lambda value=None: continuations.append(value),
    )

    await media_workers._execute_video_media(None)

    assert calls == [None]
    assert continuations == []


def test_video_owner_has_periodic_local_recovery_and_media_queue_route():
    schedule = celery_app.conf.beat_schedule["execute-video-media-recovery"]
    assert schedule["task"] == "app.workers.media.execute_video_media"
    assert schedule["schedule"].total_seconds() == 60
    assert (
        celery_app.conf.task_routes["app.workers.media.execute_video_media"]["queue"]
        == "media"
    )


@pytest.mark.asyncio
async def test_linked_task_file_url_comes_from_assets_original_reference(monkeypatch):
    item_id = uuid4()
    task_id = uuid4()
    actor_id = uuid4()
    dataset_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        dataset_item_id=item_id,
        file_type="video",
        file_path="legacy/path.mp4",
    )
    source = _source(item_id, dataset_id)
    reference = SourceReference(
        source=source,
        original_url="https://assets.test/source?sig=owner",
        video_url="https://assets.test/playback?sig=owner",
        poster_url=None,
        expires_in=2700,
    )
    captured = []

    class FakeDb:
        async def get(self, _model, _task_id, **_kwargs):
            return task

    class FakeGateway(_GatewayContext):
        async def raw_source_reference(self, request):
            captured.append(request)
            return reference

    async def visible(_db, _task, user_id):
        assert user_id == actor_id

    monkeypatch.setattr(files_api.task_access, "assert_task_visible", visible)
    monkeypatch.setattr(
        files_api,
        "media_gateway",
        lambda _db: FakeGateway(lambda *_args: None),
    )
    monkeypatch.setattr(
        files_api.storage_service,
        "generate_download_url",
        lambda *_args, **_kwargs: pytest.fail("linked source bypassed Assets"),
    )

    response = await files_api.get_file_url(
        task_id,
        db=FakeDb(),
        current_user=SimpleNamespace(id=actor_id),
    )

    assert response.url == reference.original_url
    assert response.expires_in == reference.expires_in
    assert captured[0].actor_id == actor_id
    assert captured[0].dataset_item_id == item_id
    assert captured[0].task_id == task_id


@pytest.mark.asyncio
async def test_linked_task_file_url_returns_conflict_for_drifted_source(monkeypatch):
    item_id = uuid4()
    task_id = uuid4()
    actor_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        dataset_item_id=item_id,
        file_type="video",
        file_path="legacy/path.mp4",
    )
    reference = SourceReference(
        source=_source(item_id, uuid4()).model_copy(update={"source_drifted": True}),
        original_url="https://assets.test/source?sig=owner",
        video_url="https://assets.test/playback?sig=owner",
        poster_url=None,
        expires_in=2700,
    )

    class FakeDb:
        async def get(self, _model, _task_id, **_kwargs):
            return task

    class FakeGateway(_GatewayContext):
        async def raw_source_reference(self, _request):
            return reference

    async def visible(_db, _task, _user_id):
        return None

    monkeypatch.setattr(files_api.task_access, "assert_task_visible", visible)
    monkeypatch.setattr(
        files_api,
        "media_gateway",
        lambda _db: FakeGateway(lambda *_args: None),
    )

    with pytest.raises(HTTPException) as error:
        await files_api.get_file_url(
            task_id,
            db=FakeDb(),
            current_user=SimpleNamespace(id=actor_id),
        )

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_task_list_uses_batched_raw_source_references(monkeypatch):
    actor_id, dataset_id = uuid4(), uuid4()
    tasks = [SimpleNamespace(id=uuid4(), dataset_item_id=uuid4()) for _ in range(2)]
    references = [
        SourceReference(
            source=_source(task.dataset_item_id, dataset_id),
            original_url=f"https://assets.test/{task.id}?sig=owner",
            video_url=f"https://assets.test/{task.id}?sig=owner",
            poster_url=None,
            expires_in=1800,
        )
        for task in tasks
    ]
    captured = []

    class FakeGateway(_GatewayContext):
        async def source_references(self, requests, *, raw_only=False):
            captured.append((requests, raw_only))
            return references

    monkeypatch.setattr(
        "app.api.v1.tasks._shared.media_gateway",
        lambda _db: FakeGateway(lambda *_args: None),
    )

    result = await _linked_source_references(object(), tasks, actor_id=actor_id)

    assert list(result) == [task.id for task in tasks]
    assert captured[0][1] is True
    assert [request.dataset_item_id for request in captured[0][0]] == [
        task.dataset_item_id for task in tasks
    ]


@pytest.mark.asyncio
async def test_standalone_task_file_url_keeps_storage_service(monkeypatch):
    task_id = uuid4()
    actor_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        dataset_item_id=None,
        file_type="video",
        file_path="legacy/standalone.mp4",
    )
    signed = []

    class FakeDb:
        async def get(self, _model, _task_id, **_kwargs):
            return task

    async def visible(_db, _task, _user_id):
        return None

    monkeypatch.setattr(files_api.task_access, "assert_task_visible", visible)
    monkeypatch.setattr(
        files_api,
        "media_gateway",
        lambda _db: pytest.fail("standalone source called Assets"),
    )
    monkeypatch.setattr(
        files_api.storage_service,
        "generate_download_url",
        lambda key: signed.append(key) or "https://storage.test/standalone",
    )

    response = await files_api.get_file_url(
        task_id,
        db=FakeDb(),
        current_user=SimpleNamespace(id=actor_id),
    )

    assert response.url == "https://storage.test/standalone"
    assert response.expires_in == 3600
    assert signed == [task.file_path]


def test_task_out_uses_original_owner_url_before_video_materialization(monkeypatch):
    task_id, item_id, project_id = uuid4(), uuid4(), uuid4()
    task = SimpleNamespace(
        id=task_id,
        project_id=project_id,
        dataset_item_id=item_id,
        display_id="T-video",
        file_name="source.mp4",
        file_path="legacy/source.mp4",
        file_type="video",
        status="pending",
        created_at=datetime.now(UTC),
    )
    source = _source(item_id, uuid4()).model_copy(
        update={
            "source_generation": 2,
            "materialized_generation": 1,
            "video_metadata": {"frame_count": 100, "fps": 30},
        }
    )
    reference = SourceReference(
        source=source,
        original_url="https://assets.test/original.mp4?sig=owner",
        video_url="https://assets.test/playback.mp4?sig=owner",
        poster_url="https://assets.test/poster.webp?sig=owner",
        expires_in=1800,
    )

    result = _task_with_url(
        task,
        width=640,
        height=360,
        thumbnail_path="legacy/poster.webp",
        video_metadata={"frame_count": 100},
        owner_source_reference=reference,
        owner_source_reference_loaded=True,
    )

    assert result.file_url == reference.original_url
    assert result.video_metadata is None
    assert result.image_width is None
    assert result.image_height is None
    assert result.thumbnail_url is None


def test_task_out_withholds_file_url_for_drifted_owner_source():
    task_id, item_id, project_id = uuid4(), uuid4(), uuid4()
    task = SimpleNamespace(
        id=task_id,
        project_id=project_id,
        dataset_item_id=item_id,
        display_id="T-drifted",
        file_name="source.mp4",
        file_path="legacy/source.mp4",
        file_type="video",
        status="pending",
        created_at=datetime.now(UTC),
    )
    reference = SourceReference(
        source=_source(item_id, uuid4()).model_copy(
            update={"source_drifted": True, "source_generation": 2}
        ),
        original_url="https://assets.test/original.mp4?sig=owner",
        video_url="https://assets.test/original.mp4?sig=owner",
        poster_url=None,
        expires_in=1800,
    )

    result = _task_with_url(
        task,
        video_metadata={"frame_count": 100},
        owner_source_reference=reference,
        owner_source_reference_loaded=True,
    )

    assert result.file_url is None
    assert result.video_metadata is None


def test_linked_image_task_keeps_thumbnail_while_source_url_comes_from_owner(
    monkeypatch,
):
    task_id, item_id, project_id = uuid4(), uuid4(), uuid4()
    task = SimpleNamespace(
        id=task_id,
        project_id=project_id,
        dataset_item_id=item_id,
        display_id="T-image",
        file_name="source.jpg",
        file_path="legacy/source.jpg",
        file_type="image",
        status="pending",
        created_at=datetime.now(UTC),
    )
    source = _source(item_id, uuid4()).model_copy(
        update={
            "file_name": "source.jpg",
            "file_path": "datasets/source.jpg",
            "file_type": "image",
        }
    )
    reference = SourceReference(
        source=source,
        original_url="https://assets.test/original.jpg?sig=owner",
        video_url="https://assets.test/original.jpg?sig=owner",
        poster_url=None,
        expires_in=1800,
    )
    signed = []
    monkeypatch.setattr(
        "app.api.v1.tasks._shared.storage_service.bucket_for_cache_key",
        lambda _key, default=None: "media-cache",
    )
    monkeypatch.setattr(
        "app.api.v1.tasks._shared.storage_service.generate_download_url",
        lambda key, *, bucket: (
            signed.append((key, bucket)) or "https://cache.test/poster"
        ),
    )

    result = _task_with_url(
        task,
        thumbnail_path="media-assets/owner/g1/a1/poster.webp",
        blurhash="seed",
        owner_source_reference=reference,
        owner_source_reference_loaded=True,
    )

    assert result.file_url == reference.original_url
    assert result.thumbnail_url == "https://cache.test/poster"
    assert signed == [("media-assets/owner/g1/a1/poster.webp", "media-cache")]


@pytest.mark.asyncio
async def test_video_context_reader_uses_owner_source_and_hides_stale_metadata(
    monkeypatch,
):
    item_id, task_id, project_id, actor_id, dataset_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    current = _source(item_id, dataset_id).model_copy(
        update={
            "source_generation": 2,
            "materialized_generation": 2,
            "video_metadata": {"fps": 30, "frame_count": 90, "width": 640},
        }
    )
    stale = current.model_copy(
        update={"materialized_generation": 1, "video_metadata": {"fps": 120}}
    )
    references = [
        SourceReference(
            source=source,
            original_url="https://assets.test/source.mp4?sig=owner",
            video_url="https://assets.test/playback.mp4?sig=owner",
            poster_url=None,
            expires_in=1800,
        )
        for source in (current, stale)
    ]
    requests = []

    class FakeGateway(_GatewayContext):
        async def source_reference(self, request):
            requests.append(request)
            return references[len(requests) - 1]

    monkeypatch.setattr(
        "app.services.video_frame_service.media_gateway",
        lambda _db: FakeGateway(lambda *_args: None),
    )
    reader = AssetsVideoContextReader(object())
    facts = {
        "actor_id": actor_id,
        "task_id": task_id,
        "project_id": project_id,
        "file_type": "video",
        "dataset_item_id": item_id,
    }

    fresh_context = await reader.from_task(**facts)
    stale_context = await reader.from_task(**facts)

    assert fresh_context.metadata == VideoMetadata(fps=30, frame_count=90, width=640)
    assert fresh_context.source_snapshot == current.source_snapshot
    assert stale_context.metadata == VideoMetadata()
    assert requests[0].actor_id == actor_id
    assert requests[0].task_id == task_id
    assert requests[1].dataset_item_id == item_id


@pytest.mark.asyncio
async def test_trusted_task_summary_uses_internal_source_projection_with_generation_fence(
    monkeypatch,
):
    item_id, task_id, project_id = uuid4(), uuid4(), uuid4()
    source = _source(item_id, uuid4()).model_copy(
        update={
            "source_generation": 4,
            "materialized_generation": 3,
            "source_drifted": False,
            "video_metadata": {"fps": 60, "frame_count": 240},
        }
    )
    calls = []

    class FakeGateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call(self, operation, request):
            calls.append((operation, request))
            return source

    monkeypatch.setattr(
        "app.services.video_frame_service.media_gateway",
        lambda _db: FakeGateway(),
    )

    context = await AssetsVideoContextReader(object()).from_task_summary(
        actor_id=None,
        task_id=task_id,
        project_id=project_id,
        file_type="video",
        dataset_item_id=item_id,
    )

    assert calls == [
        (
            "resolve_internal_media_source",
            InternalSourceRequest(dataset_item_id=item_id),
        )
    ]
    assert context.metadata == VideoMetadata()
    assert context.source_snapshot == source.source_snapshot
    assert context.source_file_path == source.file_path


@pytest.mark.asyncio
async def test_task_frame_retry_sets_semantic_owner_intent(monkeypatch):
    item_id, task_id, project_id, actor_id, dataset_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    source = _source(item_id, dataset_id).model_copy(
        update={
            "materialized_generation": 1,
            "video_metadata": {"fps": 30, "frame_count": 10},
        }
    )
    requests = []

    class FakeDb:
        async def commit(self):
            return None

    class FakeGateway(_GatewayContext):
        async def assets_reference(self, request):
            assert isinstance(request, AssetsRequest)
            requests.append(request)
            asset = MediaAsset(
                row_id=uuid4(),
                dataset_item_id=item_id,
                kind="frame",
                source_generation=source.source_generation,
                timetable_revision=source.timetable_revision,
                status="failed",
                error="retryable",
                frame_index=0,
                width=512,
                format="webp",
            )
            return MediaReferences(
                state=MediaAssets(source=source, assets=[asset]),
                video_url="https://assets.test/playback.mp4?sig=owner",
                expires_in=1800,
            )

    monkeypatch.setattr(
        "app.services.video_frame_service.media_gateway",
        lambda _db: FakeGateway(lambda *_args: None),
    )
    ctx = VideoContext(
        metadata=VideoMetadata(fps=30, frame_count=10),
        dataset_item_id=item_id,
        source_file_path=source.file_path,
        thumbnail_path=None,
        source_width=None,
        source_height=None,
        task_id=task_id,
        project_id=project_id,
        actor_id=actor_id,
        source_snapshot=source.source_snapshot,
    )

    await retry_frames(FakeDb(), ctx, [0], 512, "webp", task_annotation_retry=True)

    selection = requests[0].selection
    assert isinstance(selection, FrameSelection)
    assert selection.intent == "task_frame_retry"
    assert selection.expected_source_snapshot == source.source_snapshot
    assert requests[0].actor_id == actor_id
    requests.clear()

    await retry_frames(FakeDb(), ctx, [0], 512, "webp")

    assert requests[0].selection.intent == "access"
