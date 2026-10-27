from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
from io import BytesIO
import json
import mimetypes
from pathlib import PurePosixPath
from types import SimpleNamespace
from uuid import uuid4
import zipfile

import pytest

from aap_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
    ManagedOriginalProof,
)
from aap_assets_source.ingest_contract import (
    ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES,
    IngestBatchPlan,
    IngestBatchResult,
    IngestCommand,
    IngestContext,
    IngestDescriptor,
    IngestEntryCompletion,
    IngestFailure,
    IngestLayoutItem,
    IngestLayoutPage,
    IngestPlannedEntry,
    IngestProgress,
    IngestReceipt,
    SCAN_REQUEST_DIGEST,
    decode_ingest_reply,
)
from aap_assets_source_runtime.ingest_storage import (
    IngestEntryFailure,
    IngestStorageFacts,
)
from aap_assets_source_runtime.ingest_workflow import IngestWorkflow
from aap_assets_source_runtime.managed_storage import ManagedStorage
from aap_assets_source_runtime.runtime import SourceRuntime
from aap_assets_source_runtime.ingest_workflow import _validate_context
from aap_assets_source_runtime.database import NativeIngestData
from aap_assets_images.contract import ObjectIdentity
from aap_assets_images.policy import ImagePolicy


def _command() -> IngestCommand:
    return IngestCommand(operation_id=uuid4(), actor_id=uuid4(), dataset_id=uuid4())


def _managed_target(request: dict) -> ManagedOriginalMaterial:
    from uuid import UUID

    file_path = request["file_path"]
    publication = request.get("authority", {}).get("publication_id")
    return ManagedOriginalMaterial(
        creation_id=uuid4(),
        dataset_id=UUID(request["dataset_id"]),
        location_id=UUID("4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7"),
        bucket="datasets",
        object_key=file_path,
        key_prefix="",
        endpoint_url="http://s3",
        storage_identity="a" * 64,
        config_revision=1,
        binding_generation=1,
        max_bytes=request["max_bytes"],
        content_type=request["content_type"],
        origin=request["origin"],
        mode=request["mode"],
        publication_id=UUID(publication) if publication else None,
        file_path=file_path,
    )


def _managed_proof(size: int, digest: str, *, sha256: str, version="v1"):
    return ManagedOriginalProof.verified(
        mode="write",
        sha256=sha256,
        md5=digest,
        size_bytes=size,
        etag=f'"{digest}"',
        version_id=version,
        request_id="request",
        prepared={"mode": "write"},
        verification={
            "sha256": sha256,
            "md5": digest,
            "size_bytes": size,
            "etag": f'"{digest}"',
            "version_id": version,
        },
    )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    target = BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return target.getvalue()


async def test_zip_directory_record_budget_is_checked_before_any_data_write():
    data = _Data()
    target = BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for index in range(ZIP_MAX_CENTRAL_DIRECTORY_ENTRIES):
            archive.writestr(f"directory-{index}/", b"")
        archive.writestr("frame.bin", b"point cloud")
    target.seek(0)
    with pytest.raises(IngestFailure, match="resource_budget_exceeded") as error:
        await IngestWorkflow(
            data=data, storage=None, image_admission=_ImageAdmission()
        ).upload_zip(_command(), target)
    assert error.value.detail.budget == "max_central_directory_entries"
    assert data.calls == []


class _Data:
    def __init__(self, *, wrong_prepare_scope: bool = False) -> None:
        self.wrong_prepare_scope = wrong_prepare_scope
        self.command = None
        self.descriptor = None
        self.staged = self.settled = self.added = 0
        self.deduped = self.skipped = self.error_count = self.linked_tasks = 0
        self.planned: dict[object, IngestPlannedEntry] = {}
        self.items: list[IngestLayoutItem] = []
        self.errors: list[tuple[str, str]] = []
        self.finish_request = None
        self.calls: list[str] = []

    def _progress(self) -> IngestProgress:
        return IngestProgress(
            operation_id=self.command.operation_id,
            actor_id=self.command.actor_id,
            dataset_id=self.command.dataset_id,
            staged=self.staged,
            settled=self.settled,
            added=self.added,
            deduped=self.deduped,
            skipped=self.skipped,
            error_count=self.error_count,
            linked_tasks=self.linked_tasks,
        )

    async def begin(self, command: IngestCommand, descriptor: IngestDescriptor):
        self.calls.append("begin")
        self.command, self.descriptor = command, descriptor
        return IngestContext(
            operation_id=command.operation_id,
            actor_id=command.actor_id,
            dataset_id=command.dataset_id,
            descriptor=descriptor,
            state="preparing",
            dataset_name="dataset",
            is_temporal=False,
            source_prefix="dataset/",
            next_index=0,
            result=None,
        )

    async def allocate(self, request):
        self.calls.append("allocate")
        return _managed_target(request)

    async def open_operation(self, creation_id):
        from uuid import uuid4

        from aap_assets_source.managed_original_contract import (
            ManagedOriginalOperation,
        )

        self.calls.append("open_operation")
        return ManagedOriginalOperation(operation_id=uuid4(), mode="write")

    async def close_operation(
        self, creation_id, operation_id, physical_settled
    ) -> None:
        self.calls.append("close_operation")

    async def original_prepared(self, creation_id, prepared) -> None:
        self.calls.append("original_prepared")

    async def original_writing(self, creation_id) -> None:
        self.calls.append("original_writing")

    async def original_written(self, creation_id, receipt) -> None:
        self.calls.append("original_written")

    async def original_verified(self, creation_id, verification) -> None:
        self.calls.append("original_verified")

    async def fail_original(self, creation_id, outcome, error_code, evidence) -> None:
        self.calls.append("fail_original")

    async def prepare_batch(self, command, batch_id, entries):
        self.calls.append("prepare")
        scope = command
        if self.wrong_prepare_scope:
            scope = command.model_copy(update={"actor_id": uuid4()})
        plans = []
        for offset, entry in enumerate(entries):
            index = self.staged + offset
            if entry.error_code:
                planned = IngestPlannedEntry(
                    **entry.model_dump(),
                    entry_id=uuid4(),
                    index=index,
                    status="error",
                    item_id=None,
                    publication_id=None,
                    file_name=None,
                    file_path=None,
                )
                self.error_count += 1
                self.settled += 1
                self.errors.append((entry.source_name, entry.error_code))
            else:
                item_id = uuid4()
                publication_id = uuid4() if entry.source_key is None else None
                file_name = (
                    PurePosixPath(entry.relpath).name
                    if entry.source_key is None
                    else entry.relpath
                )
                file_path = (
                    f"dataset-sources/{command.dataset_id}/{item_id}/{publication_id}/{entry.relpath}"
                    if entry.source_key is None
                    else entry.source_key
                )
                planned = IngestPlannedEntry(
                    **entry.model_dump(),
                    entry_id=uuid4(),
                    index=index,
                    status="pending",
                    item_id=item_id,
                    publication_id=publication_id,
                    file_name=file_name,
                    file_path=file_path,
                )
            self.planned[planned.entry_id] = planned
            plans.append(planned)
        self.staged += len(entries)
        progress = self._progress()
        return IngestBatchPlan(
            operation_id=scope.operation_id,
            actor_id=scope.actor_id,
            dataset_id=scope.dataset_id,
            batch_id=batch_id,
            entries=plans,
            progress=progress.model_copy(
                update={
                    "operation_id": scope.operation_id,
                    "actor_id": scope.actor_id,
                    "dataset_id": scope.dataset_id,
                }
            ),
        )

    async def complete_batch(
        self,
        command: IngestCommand,
        batch_id,
        entries: list[IngestEntryCompletion],
    ) -> IngestBatchResult:
        self.calls.append("complete")
        for completion in entries:
            planned = self.planned[completion.entry_id]
            self.settled += 1
            if completion.status == "ready":
                self.added += 1
                self.linked_tasks += 1
                self.items.append(
                    IngestLayoutItem(
                        id=planned.item_id,
                        file_name=planned.file_name,
                        file_path=planned.file_path,
                        file_type=planned.file_type,
                        scene_id=None,
                        frame_index=None,
                        logical_path=planned.relpath
                        if planned.source_key is None
                        else None,
                    )
                )
            else:
                self.error_count += 1
                self.errors.append((planned.source_name, completion.error_code))
        return IngestBatchResult(
            operation_id=command.operation_id,
            actor_id=command.actor_id,
            dataset_id=command.dataset_id,
            batch_id=batch_id,
            progress=self._progress(),
        )

    async def read_layout(self, command, *, after_item_id, limit=500):
        self.calls.append("layout")
        items = sorted(self.items, key=lambda item: item.id)
        if after_item_id is not None:
            items = [item for item in items if item.id > after_item_id]
        page_items = items[:limit]
        next_id = page_items[-1].id if len(items) > limit else None
        return IngestLayoutPage(
            operation_id=command.operation_id,
            actor_id=command.actor_id,
            dataset_id=command.dataset_id,
            snapshot_digest=hashlib.sha256(b"layout").hexdigest(),
            scene_count=0,
            assigned_count=0,
            total_items=len(self.items),
            items=page_items,
            next_item_id=next_id,
        )

    async def finish(self, command, finish):
        self.calls.append("finish")
        self.finish_request = finish
        errors = []
        if self.descriptor.kind == "zip":
            messages = {
                "invalid_source_object": "Invalid source object path",
                "archive_entry_read_failed": "Archive entry could not be read",
                "storage_write_failed": "Source object could not be stored",
                "source_changed": "Source object changed during ingest",
                "source_missing": "Source object is missing",
                "source_read_failed": "Source object could not be read",
            }
            from aap_assets_source.ingest_contract import IngestFileError

            errors = [
                IngestFileError(name=name, error=messages[code])
                for name, code in self.errors
            ]
        skipped = self.skipped
        if self.descriptor.kind == "zip":
            skipped += self.descriptor.hidden_count + self.descriptor.unsafe_count
        return IngestReceipt(
            operation_id=command.operation_id,
            actor_id=command.actor_id,
            dataset_id=command.dataset_id,
            kind=self.descriptor.kind,
            request_digest=self.descriptor.request_digest,
            added=self.added,
            deduped=self.deduped,
            skipped=skipped,
            errors=errors,
            error_count=self.error_count,
            total_in_zip=self.descriptor.total_in_zip,
            linked_tasks=self.linked_tasks,
            scene_inference_notes=finish.scene_inference_notes,
        )


class _ImageAdmission:
    def __init__(self):
        self.calls = []

    async def admit_dataset_item(self, **request):
        self.calls.append(request)
        return None


class _Storage:
    def __init__(self, *, fail_after_publish: bool = False) -> None:
        self.fail_after_publish = fail_after_publish
        self.published: list[tuple[str, bytes, str, str]] = []

    async def publish(
        self,
        source,
        target,
        *,
        content_type,
        expected_size,
        expected_hash,
        image,
        on_prepared=None,
        on_writing=None,
        on_written=None,
    ):
        content = source.read()
        digest = hashlib.md5(content, usedforsecurity=False).hexdigest()
        self.published.append((target.object_key, content, content_type, expected_hash))
        if self.fail_after_publish:
            raise IngestEntryFailure("storage_write_failed")
        assert len(content) == expected_size
        assert digest == expected_hash
        return (
            IngestStorageFacts(
                file_size=len(content),
                content_hash=digest,
                width=None,
                height=None,
                object_identity=(
                    ObjectIdentity(etag=f'"{digest}"', byte_size=len(content))
                    if image
                    else None
                ),
            ),
            _managed_proof(
                len(content), digest, sha256=hashlib.sha256(content).hexdigest()
            ),
        )

    async def adopt(self, target, *, expected_size, image):
        return (
            IngestStorageFacts(
                file_size=expected_size,
                content_hash=None,
                width=None,
                height=None,
                object_identity=None,
            ),
            ManagedOriginalProof.verified(
                mode="adopt",
                sha256=hashlib.sha256(str(expected_size).encode()).hexdigest(),
                md5=hashlib.md5(
                    str(expected_size).encode(), usedforsecurity=False
                ).hexdigest(),
                size_bytes=expected_size,
                etag='"adopted"',
                version_id="v1",
                request_id=None,
                prepared={"mode": "adopt"},
                verification={
                    "sha256": hashlib.sha256(str(expected_size).encode()).hexdigest(),
                    "md5": hashlib.md5(
                        str(expected_size).encode(), usedforsecurity=False
                    ).hexdigest(),
                    "size_bytes": expected_size,
                    "etag": '"adopted"',
                    "version_id": "v1",
                },
            ),
        )


@pytest.mark.asyncio
async def test_zip_skips_hidden_and_unsafe_paths_and_uses_mime_and_immutable_targets(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mimetypes, "guess_type", lambda _name: ("application/octet-stream", None)
    )
    command = _command()
    data = _Data()
    storage = _Storage()
    source = BytesIO(
        _zip_bytes(
            {
                "scene/cloud.pcd": b"point cloud",
                ".DS_Store": b"hidden",
                "__MACOSX/._cloud.pcd": b"metadata",
                "../escape.txt": b"unsafe",
            }
        )
    )

    receipt = await IngestWorkflow(
        data=data, storage=storage, image_admission=_ImageAdmission()
    ).upload_zip(command, source)

    assert data.descriptor.total_in_zip == 3
    assert data.descriptor.planned_entries == 1
    assert data.descriptor.hidden_count == 2
    assert data.descriptor.unsafe_count == 1
    assert receipt.skipped == 3
    assert receipt.added == 1
    assert data.planned[next(iter(data.planned))].file_type == "other"
    key, content, content_type, content_hash = storage.published[0]
    planned = next(iter(data.planned.values()))
    assert key == planned.file_path
    assert key.startswith(
        f"dataset-sources/{command.dataset_id}/{planned.item_id}/{planned.publication_id}/"
    )
    assert key.endswith("/scene/cloud.pcd")
    assert content == b"point cloud"
    assert content_type == "application/octet-stream"
    assert content_hash == hashlib.md5(content, usedforsecurity=False).hexdigest()
    assert (
        data.finish_request.layout_snapshot_digest
        == hashlib.sha256(b"layout").hexdigest()
    )
    assert "layout" in data.calls


@pytest.mark.asyncio
async def test_uncertain_publication_settles_per_file_without_deleting_object() -> None:
    command = _command()
    data = _Data()
    storage = _Storage(fail_after_publish=True)
    receipt = await IngestWorkflow(
        data=data, storage=storage, image_admission=_ImageAdmission()
    ).upload_zip(command, BytesIO(_zip_bytes({"nested/photo.png": b"image bytes"})))

    assert receipt.added == 0
    assert receipt.error_count == 1
    assert receipt.errors[0].name == "nested/photo.png"
    assert receipt.errors[0].error == "Source object could not be stored"
    assert len(storage.published) == 1
    assert data.calls == [
        "begin",
        "prepare",
        "allocate",
        "open_operation",
        "fail_original",
        "complete",
        "finish",
    ]


@pytest.mark.asyncio
async def test_pending_same_hash_zip_entries_follow_the_native_plan() -> None:
    data = _Data()
    storage = _Storage()
    receipt = await IngestWorkflow(
        data=data, storage=storage, image_admission=_ImageAdmission()
    ).upload_zip(
        _command(),
        BytesIO(_zip_bytes({"first.bin": b"same", "nested/second.bin": b"same"})),
    )

    assert receipt.added == 2
    assert len(storage.published) == 2
    assert storage.published[0][3] == storage.published[1][3]


class _TransactionDatabase:
    def __init__(self, ports) -> None:
        self.ports = ports
        self.events: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        self.events.append("begin")
        try:
            yield self.ports
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")


class _UnusedS3:
    def __init__(self) -> None:
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))


class _RecordingManaged:
    """Record the one publication attempt a runtime makes before its reply."""

    def __init__(self) -> None:
        self.published = []

    async def publish_stream(
        self,
        source,
        target,
        *,
        content_type,
        expected_size,
        expected_hash,
        image,
        before_io=None,
        on_prepared=None,
        on_writing=None,
        on_written=None,
    ):
        from aap_assets_source_runtime.managed_storage import ManagedWrite

        content = source.read()
        digest = hashlib.md5(content, usedforsecurity=False).hexdigest()
        self.published.append((target.object_key, content, content_type, expected_hash))
        return ManagedWrite(
            file_size=len(content),
            content_hash=digest,
            dimensions=None,
            object_identity=(
                ObjectIdentity(etag="published", byte_size=len(content))
                if image
                else None
            ),
            proof=_managed_proof(
                len(content), digest, sha256=hashlib.sha256(content).hexdigest()
            ),
        )


@pytest.mark.asyncio
async def test_wrong_scope_prepare_reply_rolls_back_before_any_s3_publication() -> None:
    command = _command()
    data = _Data(wrong_prepare_scope=True)
    client = _UnusedS3()
    database = _TransactionDatabase(
        SimpleNamespace(
            ingest=data,
            uploads=object(),
            legacy_uploads=object(),
            deletions=object(),
            images=object(),
        )
    )
    config = SimpleNamespace(max_response_bytes=16_777_216, image_policy=ImagePolicy())
    storage = SimpleNamespace(
        client=client, datasets_bucket="datasets", managed=ManagedStorage(client)
    )
    runtime = SourceRuntime(config, database, storage)

    body = await runtime.upload_zip(
        command, BytesIO(_zip_bytes({"image.png": b"body"}))
    )

    with pytest.raises(IngestFailure) as error:
        decode_ingest_reply(json.loads(body), IngestReceipt)
    assert error.value.code == "invalid_response"
    assert database.events == ["begin", "rollback"]
    assert data.calls == ["begin", "prepare"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_malformed_native_begin_reply_rolls_back_before_any_s3_publication() -> (
    None
):
    class _MalformedConnection:
        async def fetchval(self, _statement, *_arguments):
            return {"schema": "wrong", "ok": True, "data": {}}

    command = _command()
    client = _UnusedS3()
    database = _TransactionDatabase(
        SimpleNamespace(
            ingest=NativeIngestData(_MalformedConnection(), 16_777_216),
            uploads=object(),
            legacy_uploads=object(),
            deletions=object(),
            images=object(),
        )
    )
    runtime = SourceRuntime(
        SimpleNamespace(max_response_bytes=16_777_216, image_policy=ImagePolicy()),
        database,
        SimpleNamespace(
            client=client, datasets_bucket="datasets", managed=ManagedStorage(client)
        ),
    )

    body = await runtime.upload_zip(
        command, BytesIO(_zip_bytes({"image.png": b"body"}))
    )

    with pytest.raises(IngestFailure) as error:
        decode_ingest_reply(json.loads(body), IngestReceipt)
    assert error.value.code == "invalid_response"
    assert database.events == ["begin", "rollback"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_encoded_response_budget_failure_rolls_back_after_publication() -> None:
    command = _command()
    data = _Data()
    client = _UnusedS3()
    database = _TransactionDatabase(
        SimpleNamespace(
            ingest=data,
            uploads=object(),
            legacy_uploads=object(),
            deletions=object(),
            images=object(),
        )
    )
    config = SimpleNamespace(max_response_bytes=128, image_policy=ImagePolicy())
    managed = _RecordingManaged()
    runtime = SourceRuntime(
        config,
        database,
        SimpleNamespace(client=client, datasets_bucket="datasets", managed=managed),
    )

    body = await runtime.upload_zip(
        command, BytesIO(_zip_bytes({"scene/frame.txt": b"body"}))
    )

    with pytest.raises(IngestFailure) as error:
        decode_ingest_reply(json.loads(body), IngestReceipt)
    assert error.value.code == "service_unavailable"
    assert database.events == ["begin", "rollback"]
    assert len(managed.published) == 1
    assert managed.published[0][1] == b"body"


def test_completed_replay_compares_exact_body_digest_not_descriptor_order() -> None:
    command = _command()
    request_digest = hashlib.sha256(b"exact zip body").hexdigest()
    accepted = IngestDescriptor(
        kind="zip",
        request_digest=request_digest,
        total_in_zip=1,
        planned_entries=1,
        top_level_dirs=["scene-b", "scene-a"],
    )
    current = accepted.model_copy(update={"top_level_dirs": ["scene-a", "scene-b"]})
    receipt = IngestReceipt(
        operation_id=command.operation_id,
        actor_id=command.actor_id,
        dataset_id=command.dataset_id,
        kind="zip",
        request_digest=request_digest,
        added=0,
        deduped=0,
        skipped=0,
        errors=[],
        error_count=0,
        total_in_zip=1,
        linked_tasks=0,
        scene_inference_notes=[],
    )
    context = IngestContext(
        operation_id=command.operation_id,
        actor_id=command.actor_id,
        dataset_id=command.dataset_id,
        descriptor=accepted,
        state="completed",
        dataset_name="dataset",
        is_temporal=False,
        source_prefix="dataset/",
        next_index=1,
        result=receipt,
    )

    _validate_context(command, current, context)


def test_scan_fingerprint_is_the_frozen_cross_language_value() -> None:
    assert (
        SCAN_REQUEST_DIGEST
        == "f5f47f290df98f65d1879eb9896f8bc6e16b4b0c805a834cca38c44a44faf065"
    )
