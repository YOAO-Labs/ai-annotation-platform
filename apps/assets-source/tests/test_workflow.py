from __future__ import annotations

from uuid import uuid4

import pytest
from aap_assets_images import contract as image_contract

from aap_assets_source.contract import (
    SourceFailure,
    SourceDeletionRequest,
    UploadCompleteRequest,
    UploadInitRequest,
)
from aap_assets_source.deletion_contract import DatasetSourceDeletionReceipt
from aap_assets_source.legacy_upload_contract import (
    LegacyUploadCompletion,
    LegacyUploadLockRequest,
    LegacyUploadState,
)
from aap_assets_source.upload_contract import (
    DatasetUploadAcceptance,
    DatasetUploadCompletedResult,
    DatasetUploadCompletion,
    DatasetUploadDiscard,
    DatasetUploadDiscardedResult,
    DatasetUploadDuplicate,
    DatasetUploadFailure,
    DatasetUploadIntent,
    DatasetUploadReserve,
)
from aap_assets_source_runtime.workflow import UploadWorkflow


class _Uploads:
    def __init__(self, events):
        self.events = events
        self.intent = None
        self.lock_error = None
        self.duplicate_of = None
        self.acceptance = None
        self.discarded = None

    async def reserve(self, request: DatasetUploadReserve):
        self.events.append("reserve")
        return self.intent

    async def lock(self, *, actor_id, dataset_id, item_id):
        self.events.append("lock")
        if self.lock_error:
            raise self.lock_error
        return self.intent

    async def find_duplicate(self, *, actor_id, dataset_id, item_id, content_hash):
        self.events.append("find_duplicate")
        return DatasetUploadDuplicate(duplicate_of=self.duplicate_of)

    async def complete(self, request: DatasetUploadCompletion):
        self.events.append("complete")
        return self.acceptance

    async def discard(self, request: DatasetUploadDiscard):
        self.events.append("discard")
        return self.discarded


class _LegacyUploads:
    def __init__(self, events):
        self.events = events
        self.state = None
        self.completed = None

    async def lock(self, request: LegacyUploadLockRequest):
        self.events.append("legacy_lock")
        return self.state

    async def read(self, request: LegacyUploadLockRequest):
        self.events.append("legacy_read")
        return self.state

    async def complete(self, request: LegacyUploadCompletion):
        self.events.append("legacy_complete")
        return self.completed


class _Deletions:
    def __init__(self):
        self.command = None

    async def delete(self, command):
        self.command = command
        return DatasetSourceDeletionReceipt(
            operation_id=command.operation_id,
            actor_id=command.actor_id,
            dataset_id=command.dataset_id,
            item_id=command.item_id,
            cleanup_bucket=command.cleanup_bucket,
            audit_id=1,
            deleted_datasets=int(command.item_id is None),
            deleted_items=int(command.item_id is not None),
            deleted_scenes=0,
            deleted_associations=0,
            discarded_uploads=0,
            queued_storage_deletions=0,
            cancelled_creation_jobs=0,
        )


class _Storage:
    def __init__(self, events):
        self.events = events
        self.metadata = {
            "ContentLength": 10,
            "ETag": '"0123456789abcdef0123456789abcdef"',
        }
        self.dimensions = (20, 10)

    async def generate_upload_url(self, key, content_type):
        self.events.append("presign")
        return "https://storage.example.test/signed-put"

    async def verify_staging_upload(self, key):
        self.events.append("head_staging")
        return self.metadata

    async def verify_legacy_source(self, key):
        self.events.append("head_legacy")
        return self.metadata

    async def read_staging_image_dimensions(self, key, *, if_match):
        self.events.append("dimensions_staging")
        return self.dimensions

    async def read_legacy_image_dimensions(self, key, *, if_match):
        self.events.append("dimensions_legacy")
        return self.dimensions

    async def publish_dataset_upload(self, staging_key, final_key, *, etag):
        self.events.append("copy")


def _workflow(events):
    uploads = _Uploads(events)
    legacy = _LegacyUploads(events)
    deletions = _Deletions()
    storage = _Storage(events)

    class _ImageAdmission:
        def __init__(self):
            self.calls = []

        async def read_dataset_item(self, item_id):
            events.append("image_read")
            assert legacy.state is not None
            return image_contract.ImageSource(
                owner={"kind": "dataset_item", "id": item_id},
                task_id=None,
                project_id=None,
                dataset_id=legacy.state.dataset_id,
                item_id=item_id,
                source_generation=1,
                source_snapshot="a" * 64,
                object_identity=None,
                source_drifted=False,
                upload_ready=True,
                file_path=legacy.state.file_path,
                file_name=legacy.state.file_path.rsplit("/", 1)[-1],
                file_type="image",
                bucket_kind="datasets",
                file_size=None,
                content_hash=None,
                width=legacy.state.width,
                height=legacy.state.height,
                thumbnail_path=None,
                blurhash=None,
                thumbnail_source_generation=None,
                thumbnail_error=None,
            )

        async def admit_dataset_item(self, **request):
            self.calls.append(request)
            events.append("image_admit")
            return None

    image_admission = _ImageAdmission()
    workflow = UploadWorkflow(
        upload_data=uploads,
        legacy_upload_data=legacy,
        deletion_data=deletions,
        storage=storage,
        image_admission=image_admission,
        cleanup_bucket="media-cache",
    )
    return workflow, uploads, legacy, deletions, storage


def _intent(actor_id, dataset_id, item_id, *, state="pending", result=None):
    publication_id = uuid4() if state == "pending" else uuid4()
    file_name = "photo.png"
    return DatasetUploadIntent(
        item_id=item_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        file_name=file_name,
        file_path=(
            f"dataset-sources/{dataset_id}/{item_id}/{publication_id}/{file_name}"
            if publication_id
            else None
        ),
        file_type="image",
        content_type="image/png",
        staging_key=f"dataset-uploads/{item_id}/source",
        publication_id=publication_id,
        state=state,
        result=result,
    )


def _acceptance(intent, *, duplicate_of=None):
    is_duplicate = duplicate_of is not None
    values = intent.model_dump()
    values.update(
        disposition="duplicate" if is_duplicate else "accepted",
        duplicate_of=duplicate_of,
        state="discarded" if is_duplicate else "completed",
        result=(
            DatasetUploadDiscardedResult(duplicate_of=duplicate_of)
            if is_duplicate
            else DatasetUploadCompletedResult(item_id=intent.item_id, linked_tasks=2)
        ),
    )
    return DatasetUploadAcceptance.model_validate(values)


def _complete_request(actor_id, dataset_id, item_id):
    return UploadCompleteRequest(
        operation_id=uuid4(),
        actor_id=actor_id,
        dataset_id=dataset_id,
        item_id=item_id,
    )


@pytest.mark.asyncio
async def test_upload_init_reserves_before_signing_and_echoes_frozen_scope() -> None:
    actor_id, dataset_id, item_id = uuid4(), uuid4(), uuid4()
    request = UploadInitRequest(
        actor_id=actor_id,
        dataset_id=dataset_id,
        item_id=item_id,
        file_name="folder/photo.png",
        content_type="image/png",
    )
    events = []
    workflow, uploads, _legacy, _deletions, _storage = _workflow(events)
    uploads.intent = DatasetUploadIntent(
        item_id=item_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        file_name=request.file_name,
        file_path=None,
        file_type="image",
        content_type=request.content_type,
        staging_key=f"dataset-uploads/{item_id}/source",
        publication_id=None,
        state="pending",
        result=None,
    )

    result = await workflow.init_upload(request)

    assert events == ["reserve", "presign"]
    assert (result.actor_id, result.dataset_id, result.item_id) == (
        actor_id,
        dataset_id,
        item_id,
    )
    assert result.expires_in == 900


@pytest.mark.asyncio
async def test_missing_staging_object_is_frozen_without_source_copy() -> None:
    actor_id, dataset_id, item_id = uuid4(), uuid4(), uuid4()
    request = _complete_request(actor_id, dataset_id, item_id)
    events = []
    workflow, uploads, _legacy, _deletions, storage = _workflow(events)
    uploads.intent = _intent(actor_id, dataset_id, item_id)
    storage.metadata = None
    uploads.discarded = _intent(
        actor_id,
        dataset_id,
        item_id,
        state="discarded",
        result=DatasetUploadDiscardedResult(reason="missing"),
    )

    result = await workflow.complete_upload(request)

    assert result.result is None
    assert result.duplicate_of is None
    assert events == ["lock", "head_staging", "discard"]


@pytest.mark.asyncio
async def test_explicit_source_deletion_is_not_reported_as_missing_upload() -> None:
    actor, dataset, item = uuid4(), uuid4(), uuid4()
    events = []
    workflow, uploads, *_ = _workflow(events)
    uploads.intent = _intent(
        actor,
        dataset,
        item,
        state="discarded",
        result=DatasetUploadDiscardedResult(reason="source_deleted"),
    )
    with pytest.raises(SourceFailure) as failure:
        await workflow.complete_upload(_complete_request(actor, dataset, item))
    assert failure.value.code == "upload_not_pending"
    assert events == ["lock"]


@pytest.mark.asyncio
async def test_modern_duplicate_is_committed_without_image_read_or_copy() -> None:
    actor_id, dataset_id, item_id, existing_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    request = _complete_request(actor_id, dataset_id, item_id)
    events = []
    workflow, uploads, _legacy, _deletions, _storage = _workflow(events)
    intent = _intent(actor_id, dataset_id, item_id)
    uploads.intent = intent
    uploads.duplicate_of = existing_id
    uploads.acceptance = _acceptance(intent, duplicate_of=existing_id)

    result = await workflow.complete_upload(request)

    assert result.duplicate_of == existing_id
    assert result.result is None
    assert events == ["lock", "head_staging", "find_duplicate", "complete"]


@pytest.mark.asyncio
async def test_modern_upload_copies_conditionally_before_native_acceptance() -> None:
    actor_id, dataset_id, item_id = uuid4(), uuid4(), uuid4()
    request = _complete_request(actor_id, dataset_id, item_id)
    events = []
    workflow, uploads, _legacy, _deletions, _storage = _workflow(events)
    intent = _intent(actor_id, dataset_id, item_id)
    uploads.intent = intent
    uploads.acceptance = _acceptance(intent)

    result = await workflow.complete_upload(request)

    assert result.result == DatasetUploadCompletedResult(
        item_id=item_id, linked_tasks=2
    )
    assert events == [
        "lock",
        "head_staging",
        "find_duplicate",
        "dimensions_staging",
        "copy",
        "head_legacy",
        "complete",
        "image_admit",
    ]
    assert len(workflow._image_admission.calls) == 1
    admission = workflow._image_admission.calls[0]
    assert admission["item_id"] == item_id
    assert admission["publication_id"] == intent.publication_id
    assert admission["file_path"] == intent.file_path
    assert admission["object_identity"].etag == '"0123456789abcdef0123456789abcdef"'


@pytest.mark.asyncio
async def test_completed_legacy_operation_replays_without_rechecking_storage() -> None:
    actor_id, dataset_id, item_id = uuid4(), uuid4(), uuid4()
    request = _complete_request(actor_id, dataset_id, item_id)
    events = []
    workflow, uploads, legacy, _deletions, _storage = _workflow(events)
    uploads.lock_error = DatasetUploadFailure("upload_missing")
    legacy.state = LegacyUploadState(
        operation_id=request.operation_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        item_id=item_id,
        file_path="old-name/photo.png",
        file_type="image",
        width=None,
        height=None,
        state="completed",
        result=DatasetUploadCompletedResult(item_id=item_id, linked_tasks=1),
        duplicate_of=None,
    )

    result = await workflow.complete_upload(request)

    assert result.result == DatasetUploadCompletedResult(
        item_id=item_id, linked_tasks=1
    )
    assert events == ["lock", "legacy_read"]


@pytest.mark.asyncio
async def test_legacy_missing_source_does_not_mutate_or_delete_placeholder() -> None:
    actor_id, dataset_id, item_id = uuid4(), uuid4(), uuid4()
    request = _complete_request(actor_id, dataset_id, item_id)
    events = []
    workflow, uploads, legacy, _deletions, storage = _workflow(events)
    uploads.lock_error = DatasetUploadFailure("upload_missing")
    legacy.state = LegacyUploadState(
        operation_id=request.operation_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        item_id=item_id,
        file_path="historical/source.png",
        file_type="image",
        width=None,
        height=None,
        state="pending",
        result=None,
        duplicate_of=None,
    )
    storage.metadata = None

    result = await workflow.complete_upload(request)

    assert result.result is None and result.duplicate_of is None
    assert events == ["lock", "legacy_read", "image_read", "head_legacy"]


@pytest.mark.asyncio
async def test_legacy_image_is_headed_and_dimensioned_before_item_lock():
    actor_id, dataset_id, item_id = uuid4(), uuid4(), uuid4()
    request = _complete_request(actor_id, dataset_id, item_id)
    events = []
    workflow, uploads, legacy, _deletions, storage = _workflow(events)
    uploads.lock_error = DatasetUploadFailure("upload_missing")
    legacy.state = LegacyUploadState(
        operation_id=request.operation_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        item_id=item_id,
        file_path="historical/source.png",
        file_type="image",
        width=None,
        height=None,
        state="pending",
        result=None,
        duplicate_of=None,
    )
    legacy.completed = legacy.state.model_copy(
        update={
            "state": "completed",
            "result": DatasetUploadCompletedResult(item_id=item_id, linked_tasks=0),
        }
    )

    result = await workflow.complete_upload(request)

    assert result.result == legacy.completed.result
    assert events == [
        "lock",
        "legacy_read",
        "image_read",
        "head_legacy",
        "dimensions_legacy",
        "legacy_lock",
        "image_read",
        "legacy_complete",
        "image_admit",
    ]
    assert workflow._image_admission.calls[0]["object_identity"].byte_size == 10


@pytest.mark.asyncio
async def test_pending_nonimage_legacy_upload_keeps_lock_then_head_compatibility():
    actor_id, dataset_id, item_id = uuid4(), uuid4(), uuid4()
    request = _complete_request(actor_id, dataset_id, item_id)
    events = []
    workflow, uploads, legacy, _deletions, _storage = _workflow(events)
    uploads.lock_error = DatasetUploadFailure("upload_missing")
    legacy.state = LegacyUploadState(
        operation_id=request.operation_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        item_id=item_id,
        file_path="historical/clip.mp4",
        file_type="video",
        width=None,
        height=None,
        state="pending",
        result=None,
        duplicate_of=None,
    )
    legacy.completed = legacy.state.model_copy(
        update={
            "state": "completed",
            "result": DatasetUploadCompletedResult(item_id=item_id, linked_tasks=0),
        }
    )

    await workflow.complete_upload(request)

    assert events == [
        "lock",
        "legacy_read",
        "legacy_lock",
        "head_legacy",
        "legacy_complete",
    ]


@pytest.mark.asyncio
async def test_delete_uses_runtime_cleanup_bucket_not_public_request_input() -> None:
    actor_id, dataset_id, operation_id = uuid4(), uuid4(), uuid4()
    request = SourceDeletionRequest(
        operation_id=operation_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
    )
    workflow, _uploads, _legacy, deletions, _storage = _workflow([])

    result = await workflow.delete_source(request)

    assert result.operation_id == operation_id
    assert deletions.command.cleanup_bucket == "media-cache"
    assert not hasattr(request, "cleanup_bucket")
