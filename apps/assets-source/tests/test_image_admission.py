from __future__ import annotations

from uuid import uuid4, uuid5

import pytest

from aap_assets_images import contract as c
from aap_assets_images.policy import ImagePolicy
from aap_assets_source_runtime.image_admission import (
    AsyncpgImageAdmissionData,
    ImageAdmission,
    object_identity_from_head,
)


def _source(
    *,
    item_id=None,
    width: int | None = 100,
    height: int | None = 80,
    file_path: str = "dataset-sources/dataset/item/publication/photo.png",
    content_hash: str | None = "0123456789abcdef0123456789abcdef",
):
    item_id = item_id or uuid4()
    return c.ImageSource(
        owner={"kind": "dataset_item", "id": item_id},
        task_id=None,
        project_id=None,
        dataset_id=uuid4(),
        item_id=item_id,
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity=None,
        source_drifted=False,
        upload_ready=True,
        file_path=file_path,
        file_name="photo.png",
        file_type="image",
        bucket_kind="datasets",
        file_size=12,
        content_hash=content_hash,
        width=width,
        height=height,
        thumbnail_path=None,
        blurhash=None,
        thumbnail_source_generation=None,
        thumbnail_error=None,
    )


def _task_source(task_id):
    return c.ImageSource(
        owner={"kind": "task", "id": task_id},
        task_id=task_id,
        project_id=uuid4(),
        dataset_id=None,
        item_id=None,
        source_generation=1,
        source_snapshot="b" * 64,
        object_identity=None,
        source_drifted=False,
        upload_ready=True,
        file_path=f"{task_id}/photo.png",
        file_name="photo.png",
        file_type="image",
        bucket_kind="legacy",
        file_size=None,
        content_hash=None,
        width=None,
        height=None,
        thumbnail_path=None,
        blurhash=None,
        thumbnail_source_generation=None,
        thumbnail_error=None,
    )


class _Native:
    def __init__(self, source: c.ImageSource) -> None:
        self.source = source
        self.calls: list[tuple[str, object]] = []
        self.missing = False
        self.bound_source = source

    async def call(self, operation, request):
        self.calls.append((operation, request))
        if operation == "resolve_internal_image_source":
            if self.missing:
                raise c.ImageFailure("asset_missing")
            if request.object_identity is None:
                return self.source
            self.bound_source = self.source.model_copy(
                update={"object_identity": request.object_identity}
            )
            return self.bound_source
        assert operation == "schedule_image_media"
        return c.AcceptedImageWork(
            operation_id=request.operation_id,
            source=self.bound_source,
            work=c.ImageWorkReference(
                job_id=uuid4(),
                generation=1,
                owner=self.source.owner,
                source_generation=1,
                kind=request.spec.kind,
                status="pending",
            ),
            spec=request.spec,
        )


@pytest.mark.asyncio
async def test_dataset_admission_binds_observed_bytes_before_scheduling_small_thumbnail():
    item_id = uuid4()
    publication_id = uuid4()
    source = _source(item_id=item_id)
    identity = c.ObjectIdentity(etag='"final-etag"', byte_size=12, version_id="v1")
    native = _Native(source)

    accepted = await ImageAdmission(native, ImagePolicy()).admit_dataset_item(
        item_id=item_id,
        publication_id=publication_id,
        file_path=source.file_path,
        file_size=12,
        content_hash=source.content_hash,
        object_identity=identity,
    )

    assert accepted is not None
    assert [operation for operation, _ in native.calls] == [
        "resolve_internal_image_source",
        "resolve_internal_image_source",
        "schedule_image_media",
    ]
    observe = native.calls[1][1]
    assert observe.expected_source_snapshot == source.source_snapshot
    assert observe.object_identity == identity
    schedule = native.calls[2][1]
    assert schedule.operation_id == uuid5(publication_id, "aap-image:admission")
    assert schedule.spec.kind == "thumbnail"
    assert accepted.work.kind == "thumbnail"


@pytest.mark.asyncio
async def test_dataset_admission_preserves_pyramid_policy_and_known_large_dimensions():
    item_id = uuid4()
    source = _source(item_id=item_id, width=5000, height=5000)
    native = _Native(source)
    policy = ImagePolicy(auto_generate_pyramids=True)

    await ImageAdmission(native, policy).admit_dataset_item(
        item_id=item_id,
        publication_id=uuid4(),
        file_path=source.file_path,
        file_size=12,
        content_hash=source.content_hash,
        object_identity=c.ObjectIdentity(etag="etag", byte_size=12),
    )

    assert native.calls[-1][1].spec.kind == "pyramid"


@pytest.mark.asyncio
async def test_large_image_with_auto_pyramids_disabled_binds_without_preview_job():
    item_id = uuid4()
    source = _source(item_id=item_id, width=5000, height=5000)
    native = _Native(source)

    accepted = await ImageAdmission(native, ImagePolicy()).admit_dataset_item(
        item_id=item_id,
        publication_id=uuid4(),
        file_path=source.file_path,
        file_size=12,
        content_hash=source.content_hash,
        object_identity=c.ObjectIdentity(etag="etag", byte_size=12),
    )

    assert accepted is None
    assert [operation for operation, _ in native.calls] == [
        "resolve_internal_image_source",
        "resolve_internal_image_source",
    ]
    assert native.calls[1][1].object_identity.etag == "etag"


@pytest.mark.asyncio
async def test_unknown_dimensions_and_task_sources_request_native_preview_continuation():
    item_id = uuid4()
    source = _source(item_id=item_id, width=None, height=None)
    native = _Native(source)

    await ImageAdmission(native, ImagePolicy()).admit_dataset_item(
        item_id=item_id,
        publication_id=uuid4(),
        file_path=source.file_path,
        file_size=12,
        content_hash=source.content_hash,
        object_identity=c.ObjectIdentity(etag="etag", byte_size=12),
    )

    spec = native.calls[-1][1].spec
    assert spec.kind == "dimensions"
    assert spec.generate_preview is True


@pytest.mark.asyncio
async def test_task_admission_uses_generation_fenced_dimensions_preview():
    task_id = uuid4()
    source = _task_source(task_id)
    native = _Native(source)

    await ImageAdmission(native, ImagePolicy()).admit_task(
        task_id=task_id,
        source_generation=source.source_generation,
        file_path=source.file_path,
        object_identity=c.ObjectIdentity(etag="task-etag", byte_size=24),
    )

    assert native.calls[-1][1].spec.kind == "dimensions"
    assert native.calls[-1][1].spec.generate_preview is True
    assert native.calls[-1][1].operation_id == uuid5(task_id, "aap-image:admission:g1")


@pytest.mark.asyncio
async def test_delayed_old_publication_cannot_bind_a_new_item_descriptor():
    item_id = uuid4()
    source = _source(item_id=item_id, file_path="dataset-sources/new/photo.png")
    native = _Native(source)

    with pytest.raises(c.ImageFailure, match="source_changed"):
        await ImageAdmission(native, ImagePolicy()).admit_dataset_item(
            item_id=item_id,
            publication_id=uuid4(),
            file_path="dataset-sources/old/photo.png",
            file_size=12,
            content_hash=source.content_hash,
            object_identity=c.ObjectIdentity(etag="old-etag", byte_size=12),
        )

    assert len(native.calls) == 1


@pytest.mark.asyncio
async def test_deduped_ingest_item_can_skip_missing_item_without_scheduling():
    source = _source()
    native = _Native(source)
    native.missing = True

    accepted = await ImageAdmission(native, ImagePolicy()).admit_dataset_item(
        item_id=source.item_id,
        publication_id=uuid4(),
        file_path=source.file_path,
        file_size=12,
        content_hash=source.content_hash,
        object_identity=c.ObjectIdentity(etag="etag", byte_size=12),
        missing_is_skipped=True,
    )

    assert accepted is None
    assert len(native.calls) == 1


def test_object_identity_is_taken_from_final_head_and_checks_expected_size():
    identity = object_identity_from_head(
        {"ContentLength": 3, "ETag": '"final"', "VersionId": "v7"},
        expected_size=3,
    )
    assert identity == c.ObjectIdentity(etag="final", byte_size=3, version_id="v7")

    with pytest.raises(c.ImageFailure, match="source_changed"):
        object_identity_from_head(
            {"ContentLength": 4, "ETag": "final"}, expected_size=3
        )

    for invalid_version in (123, "", "v" * 256, "bad\nversion"):
        with pytest.raises(c.ImageFailure, match="invalid_response"):
            object_identity_from_head(
                {
                    "ContentLength": 3,
                    "ETag": '"final"',
                    "VersionId": invalid_version,
                }
            )


@pytest.mark.asyncio
async def test_asyncpg_binding_uses_only_published_sql_and_same_connection():
    class _Connection:
        def __init__(self):
            self.calls = []

        async def fetchval(self, statement, *arguments):
            self.calls.append((statement, arguments))
            return {
                "schema": c.IMAGE_SCHEMA,
                "ok": True,
                "data": _source(item_id=arguments[1]).model_dump(mode="json"),
            }

    connection = _Connection()
    source = _source()
    native = AsyncpgImageAdmissionData(connection, 1024 * 1024)
    result = await native.call(
        "resolve_internal_image_source",
        c.ResolveInternalImageSourceRequest(
            owner_kind="dataset_item", owner_id=source.item_id
        ),
    )

    assert result.owner.id == source.item_id
    assert connection.calls[0][0].startswith(
        "SELECT aap_assets_v1.resolve_internal_image_source("
    )
    assert connection.calls[0][1] == ("dataset_item", source.item_id, None, None)
