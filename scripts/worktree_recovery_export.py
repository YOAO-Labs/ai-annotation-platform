"""Synthetic export output retention and cache linkage in offline recovery.

The tiny ZIPs qualify physical relocation and immutable receipts, not the
application's export packaging or an actual source job's execution history.
Media ZIP bytes retain their historical signed URLs after bucket relocation.
Their cache indexes are invalidated; usable delivery requires a new bundle/ZIP,
not ZIP mutation, signing renewal or extension of the saved delivery deadline.
"""

from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import io
import json
import uuid
import zipfile
from urllib.parse import unquote, urlsplit

from worktree_env import WorktreeError

STATES = ("pending", "ready", "retired")
CACHE_KEY = hashlib.sha256(b"aap:isolated-recovery-export-cache").hexdigest()
MEDIA_STATES = ("building", "ready")
MEDIA_CACHE_KEY = hashlib.sha256(
    b"aap:isolated-recovery-export-media-cache"
).hexdigest()


def identifier(kind, state):
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"aap:isolated-recovery-export:{kind}:{state}"
    )


def content_for(state):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("fixture.json", (2020, 1, 1, 0, 0, 0)),
            json.dumps({"fixture": "offline-recovery-export", "state": state}).encode(),
        )
    return stream.getvalue()


async def seed_export_outputs(db, *, project, storage, now):
    """Keep physical responsibility without creating matching source Job rows."""
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_output import ExportOutput
    from app.services import export_storage

    for state in STATES:
        raw = content_for(state)
        created = now - timedelta(days=8) if state == "retired" else now
        row = ExportOutput(
            id=identifier("output", state),
            project_id=project.id,
            job_id=identifier("absent-job", state),
            media="image",
            format="aap_json",
            file_count=1,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            created_at=created,
            expires_at=created + timedelta(days=7),
            state=state,
            retired_at=now if state == "retired" else None,
        )
        row.storage_ref = await export_storage.allocate(db, row)
        db.add(row)
        await db.flush()
        owner = export_storage.resolve(row)
        storage.client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=raw,
            ContentType="application/zip",
        )
        if state == "ready":
            db.add(
                ExportArtifact(
                    cache_key=CACHE_KEY,
                    project_id=project.id,
                    batch_id=None,
                    format=row.format,
                    object_key=owner.object_key,
                    file_count=row.file_count,
                    size_bytes=row.size_bytes,
                    expires_at=row.expires_at,
                    output_id=row.id,
                )
            )
    await db.flush()
    await seed_media_bundles(db, project=project, storage=storage)


async def verify_export_outputs(db, *, storage):
    from sqlalchemy import select
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_output import ExportOutput
    from app.services import export_storage

    for state in STATES:
        row = await db.get(ExportOutput, identifier("output", state))
        raw = content_for(state)
        assert row is not None and row.state == state
        assert row.job_id == identifier("absent-job", state)
        assert row.expires_at - row.created_at == timedelta(days=7)
        assert (row.retired_at is not None) == (state == "retired")
        assert row.sha256 == hashlib.sha256(raw).hexdigest()
        assert row.size_bytes == len(raw) and row.file_count == 1
        owner = export_storage.resolve(row)
        assert owner.bucket == storage.export_bucket
        response = storage.client.get_object(Bucket=owner.bucket, Key=owner.object_key)
        try:
            assert response["Body"].read(row.size_bytes + 1) == raw
        finally:
            response["Body"].close()
        caches = list(
            await db.scalars(
                select(ExportArtifact).where(ExportArtifact.output_id == row.id)
            )
        )
        assert len(caches) == (1 if state == "ready" else 0)
        if caches:
            cache = caches[0]
            assert cache.cache_key == CACHE_KEY and cache.project_id == row.project_id
            assert (
                cache.object_key == owner.object_key
                and cache.expires_at == row.expires_at
            )
            assert (
                cache.size_bytes == row.size_bytes
                and cache.file_count == row.file_count
            )
            assert cache.format == row.format
    return {
        "pinned_export_outputs": 3,
        "verified_export_zip_objects": 3,
        "export_cache_links": 1,
        **(await verify_media_bundles(db, storage=storage)),
    }


def validate_media_pairs(bundles, outputs):
    """Missing historical ZIPs retain their owner; existing associations must be exact."""
    by_bundle = {row.id: row for row in bundles}
    by_output = {row.id: row for row in outputs}
    if len(by_bundle) != len(bundles) or len(by_output) != len(outputs):
        raise WorktreeError("导出媒体恢复含重复责任身份")
    for output in outputs:
        identifier = getattr(output, "media_bundle_id", None)
        if identifier is None:
            continue
        bundle = by_bundle.get(identifier)
        if bundle is None or (
            output.id != bundle.zip_output_id
            or output.project_id != bundle.project_id
            or output.job_id != bundle.job_id
            or output.expires_at != bundle.expires_at
            or output.media
            != ("video" if bundle.input_snapshot.get("schema") == 4 else "image")
            or (output.state == "ready" and bundle.published_at is None)
            or (output.state == "pending" and bundle.state == "ready")
        ):
            raise WorktreeError("导出媒体与ZIP关联、发布时间或共同期限不一致")
    for bundle in bundles:
        output = by_output.get(bundle.zip_output_id)
        if output is not None and getattr(output, "media_bundle_id", None) != bundle.id:
            raise WorktreeError("导出媒体的预分配ZIP身份指向其他责任")


async def media_cache_ids(db):
    """Only disposable indexes are excluded from protected, not full DB evidence."""
    from sqlalchemy import select
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_output import ExportOutput

    rows = list(
        await db.scalars(
            select(ExportArtifact.id)
            .join(ExportOutput, ExportOutput.id == ExportArtifact.output_id)
            .where(ExportOutput.media_bundle_id.is_not(None))
            .limit(10_001)
        )
    )
    if len(rows) > 10_000:
        raise WorktreeError("导出媒体恢复索引超过10000行预算")
    return {str(identifier) for identifier in rows}


async def typed_media_cache_indexes(db, bundles, outputs):
    from sqlalchemy import select
    from app.db.models.export_artifact import ExportArtifact
    from app.services import export_storage

    validate_media_pairs(bundles, outputs)
    media = {
        row.id: row
        for row in outputs
        if getattr(row, "media_bundle_id", None) is not None
    }
    if not media:
        return []
    owners = {row.id: row for row in bundles}
    result = []
    for cache in await db.scalars(
        select(ExportArtifact).where(ExportArtifact.output_id.in_(media))
    ):
        output = media.get(cache.output_id)
        if output is None:
            continue
        bundle = owners[output.media_bundle_id]
        owner = export_storage.resolve(output)
        batch = bundle.input_snapshot.get("batch_id")
        if (
            cache.project_id != output.project_id
            or cache.object_key != owner.object_key
            or cache.format != output.format
            or cache.expires_at != output.expires_at
            or type(cache.file_count) is not int
            or cache.file_count != output.file_count
            or type(cache.size_bytes) is not int
            or cache.size_bytes != output.size_bytes
            or (str(cache.batch_id) if cache.batch_id else None) != batch
            or not isinstance(cache.cache_key, str)
            or len(cache.cache_key) != 64
            or any(char not in "0123456789abcdef" for char in cache.cache_key)
        ):
            raise WorktreeError("导出媒体缓存索引与冻结交付元数据不一致")
        result.append(cache)
    return result


def media_bytes():
    from PIL import Image

    pcd = (
        b"VERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
        b"WIDTH 2\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS 2\nDATA ascii\n1 2 3\nnan 5 6\n"
    )
    with io.BytesIO() as output, Image.new("RGB", (2, 2), "navy") as image:
        image.save(output, format="PNG")
        return {"primary_lidar": pcd, "camera_front": output.getvalue()}


def _json_source(source):
    return json.loads(
        json.dumps(
            asdict(source),
            default=lambda value: (
                value.isoformat() if isinstance(value, datetime) else str(value)
            ),
        )
    )


def media_inputs(
    project_id, sources, source_export_bucket, *, source_public_path_prefix=""
):
    """Historical synthetic Task/Job context, not execution qualification."""
    task_id = str(identifier("historical-media-task", "retained"))
    media, logical = [], {}
    for role, source in sources.items():
        identity = {**source, "relation": None}
        key = hashlib.sha256(
            json.dumps(
                identity, sort_keys=True, ensure_ascii=False, allow_nan=False
            ).encode()
        ).hexdigest()
        media.append(
            {
                "task_id": task_id,
                "role": role,
                "link_id": None
                if role == "primary_lidar"
                else str(identifier("historical-camera-link", "retained")),
                "source_key": key,
                "source": source,
            }
        )
        logical[
            "pointclouds/fixture.pcd" if role == "primary_lidar" else "images/front.png"
        ] = key
    frozen = {
        "schema": 1,
        "project_id": str(project_id),
        "batch_id": None,
        "targets": ["pointmask"],
        "options": {},
        "tasks": [{"id": task_id, "project_id": str(project_id)}],
        "media": media,
        "fixture": {
            "qualification": "offline-retained-bytes-only",
            "source_export_bucket": source_export_bucket,
            "source_public_path_prefix": source_public_path_prefix,
        },
    }
    return frozen, {"pointmask": logical}


def _read_exact(storage, bucket, key, sha256, size, *, metadata=None):
    assert type(size) is int and 0 <= size <= 1024**2
    params = {"Bucket": bucket, "Key": key}
    head = storage.client.head_object(**params)
    assert head["ContentLength"] == size and head.get("ETag")
    if metadata is not None:
        assert head.get("Metadata") == metadata
    condition = {"IfMatch": head["ETag"]}
    if head.get("VersionId") not in (None, "null"):
        condition["VersionId"] = head["VersionId"]
    response = storage.client.get_object(**params, **condition)
    try:
        raw = response["Body"].read(size + 1)
        assert response["Body"].read(1) == b""
    finally:
        response["Body"].close()
    assert len(raw) == size and hashlib.sha256(raw).hexdigest() == sha256
    after = storage.client.head_object(**params)
    fields = ("ETag", "ContentLength", "VersionId", "Metadata")
    assert all(
        response.get(field) == head.get(field) == after.get(field) for field in fields
    )
    return raw


async def seed_media_sources(db, *, project, storage):
    """Register only the exact bytes just PUT/read back in this owned fixture."""
    from app.db.models.dataset import Dataset, DatasetItem
    from app.services.dataset_assets import ensure_legacy_bindings, register_item_asset
    from app.services import original_sources

    dataset = Dataset(
        id=identifier("source-dataset", "media"),
        display_id="DS-REC-EXPORT",
        name="Synthetic retained export inputs",
        data_type="lidar",
        created_by=project.owner_id,
        file_count=2,
    )
    db.add(dataset)
    await db.flush()
    binding = await ensure_legacy_bindings(db)
    result = {}
    for role, raw in media_bytes().items():
        source_id = identifier("source-item", role)
        name = "fixture.pcd" if role == "primary_lidar" else "front.png"
        key = f"lidar-export-originals/{source_id}/{name}"
        item = DatasetItem(
            id=source_id,
            dataset_id=dataset.id,
            file_name=name,
            file_path=key,
            file_type="point_cloud" if role == "primary_lidar" else "image",
            file_size=len(raw),
            width=None if role == "primary_lidar" else 2,
            height=None if role == "primary_lidar" else 2,
        )
        db.add(item)
        await db.flush()
        sha = hashlib.sha256(raw).hexdigest()
        storage.client.put_object(
            Bucket=binding.bucket,
            Key=key,
            Body=raw,
            ContentLength=len(raw),
            IfNoneMatch="*",
            Metadata={"fixture": "recovery-export-original", "sha256": sha},
        )
        _read_exact(storage, binding.bucket, key, sha, len(raw))
        await register_item_asset(
            db, item, sha256=sha, size_bytes=len(raw), binding=binding
        )
        result[role] = _json_source(await original_sources.resolve_item(db, item.id))
    return result


def media_metadata(row, sha):
    return {
        "fixture": "recovery-export-media",
        "purpose": "export",
        "bundle-id": str(row.id),
        "sha256": sha,
    }


def media_zip(row, urls):
    document = {
        "fixture": "offline-recovery-export-media",
        "bundle_id": str(row.id),
        "expires_at": row.expires_at.isoformat(),
        "media": [],
    }
    for target, paths in row.logical_paths["targets"].items():
        for path, key in paths.items():
            document["media"].append(
                {
                    "target": target,
                    "path": path,
                    "key": key,
                    **row.inventory["objects"][key],
                    "url": urls[key],
                }
            )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("manifest.json", (2020, 1, 1, 0, 0, 0)),
            json.dumps(document, sort_keys=True, ensure_ascii=False).encode(),
        )
    return output.getvalue()


async def seed_media_bundles(db, *, project, storage):
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_media_bundle import ExportMediaBundle
    from app.db.models.export_output import ExportOutput
    from app.services import (
        export_media_bundles as bundles,
        export_media_storage as physical,
        export_storage,
    )

    sources = await seed_media_sources(db, project=project, storage=storage)
    from app.config import settings

    frozen, logical = media_inputs(
        project.id,
        sources,
        storage.export_bucket,
        source_public_path_prefix=urlsplit(settings.minio_public_url or "").path.rstrip(
            "/"
        ),
    )
    digest = hashlib.sha256(
        json.dumps(frozen, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()
    raw_sources = {
        entry["source_key"]: media_bytes()[entry["role"]] for entry in frozen["media"]
    }
    for state in MEDIA_STATES:
        allocated = await bundles.allocate(
            db,
            project_id=project.id,
            job_id=identifier("absent-media-job", state),
            input_snapshot=frozen,
            input_digest=digest,
            logical_sources=logical,
        )
        receipt = await bundles.claim(db, allocated.id)
        row = await db.get(ExportMediaBundle, receipt.id)
        owner = physical.resolve(receipt)
        urls = {}
        for source_key, key in bundles.source_objects(receipt, logical).items():
            raw = raw_sources[source_key]
            storage.client.put_object(
                Bucket=owner.bucket,
                Key=key,
                Body=raw,
                ContentLength=len(raw),
                IfNoneMatch="*",
                Metadata=media_metadata(row, hashlib.sha256(raw).hexdigest()),
            )
            urls[key] = physical.signed_url(
                storage.client, receipt, key[len(owner.prefix) :]
            )
        physical.verify_inventory(storage.client, receipt)
        raw = media_zip(row, urls)
        output = ExportOutput(
            id=receipt.zip_output_id,
            project_id=project.id,
            job_id=receipt.job_id,
            media_bundle_id=receipt.id,
            media="image",
            format="pointmask",
            file_count=1,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            state="pending",
            created_at=receipt.created_at,
            expires_at=receipt.expires_at,
        )
        output.storage_ref = await export_storage.allocate(db, output)
        db.add(output)
        await db.flush()
        zip_owner = export_storage.resolve(output)
        storage.client.put_object(
            Bucket=zip_owner.bucket,
            Key=zip_owner.object_key,
            Body=raw,
            ContentLength=len(raw),
            IfNoneMatch="*",
            ContentType="application/zip",
        )
        _read_exact(
            storage,
            zip_owner.bucket,
            zip_owner.object_key,
            output.sha256,
            output.size_bytes,
        )
        if state == "ready":
            await bundles.publish(db, receipt)
            output.state = "ready"
            db.add(
                ExportArtifact(
                    id=identifier("media-cache", state),
                    cache_key=MEDIA_CACHE_KEY,
                    project_id=project.id,
                    batch_id=None,
                    output_id=output.id,
                    format=output.format,
                    object_key=zip_owner.object_key,
                    file_count=output.file_count,
                    size_bytes=output.size_bytes,
                    expires_at=output.expires_at,
                )
            )
        await db.flush()


async def verify_media_bundles(db, *, storage):
    from sqlalchemy import select
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_media_bundle import ExportMediaBundle
    from app.db.models.export_output import ExportOutput
    from app.services import (
        export_media_bundles as bundles,
        export_media_storage as physical,
        export_storage,
    )

    for state in MEDIA_STATES:
        row = await db.scalar(
            select(ExportMediaBundle).where(
                ExportMediaBundle.job_id == identifier("absent-media-job", state)
            )
        )
        assert row is not None and row.state == state and row.lease_token is not None
        assert row.expires_at - row.created_at == timedelta(days=7)
        assert (row.published_at is not None) == (state == "ready")
        frozen = bundles.snapshot(row)
        owner = physical.resolve(frozen)
        assert (
            owner.bucket == storage.export_bucket and owner.ref["purpose"] == "export"
        )
        physical.verify_inventory(storage.client, frozen)
        for key, expected in frozen.inventory["objects"].items():
            _read_exact(
                storage,
                owner.bucket,
                key,
                expected["sha256"],
                expected["size_bytes"],
                metadata=media_metadata(row, expected["sha256"]),
            )
        output = await db.get(ExportOutput, row.zip_output_id)
        assert output is not None and output.state == (
            "ready" if state == "ready" else "pending"
        )
        validate_media_pairs([row], [output])
        zip_owner = export_storage.resolve(output)
        assert zip_owner.bucket == storage.export_bucket
        raw = _read_exact(
            storage,
            zip_owner.bucket,
            zip_owner.object_key,
            output.sha256,
            output.size_bytes,
        )
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert archive.namelist() == ["manifest.json"]
            document = json.loads(archive.read("manifest.json"))
        assert (
            document["bundle_id"] == str(row.id)
            and document["expires_at"] == row.expires_at.isoformat()
        )
        assert len(document["media"]) == row.object_count == 2
        old_bucket = row.input_snapshot["fixture"]["source_export_bucket"]
        public_prefix = row.input_snapshot["fixture"]["source_public_path_prefix"]
        assert old_bucket != owner.bucket
        for entry in document["media"]:
            assert {
                key: entry[key] for key in ("sha256", "size_bytes")
            } == row.inventory["objects"][entry["key"]]
            assert (
                entry["key"]
                == row.logical_paths["targets"][entry["target"]][entry["path"]]
            )
            assert (
                unquote(urlsplit(entry["url"]).path)
                == f"{public_prefix}/{old_bucket}/{entry['key']}"
            )
        assert not list(
            await db.scalars(
                select(ExportArtifact).where(ExportArtifact.output_id == output.id)
            )
        )
    return {
        "pinned_export_media_bundles": 2,
        "verified_export_media_objects": 4,
        "verified_export_media_zips": 2,
        "invalidated_export_media_cache_indexes": 1,
        "export_media_embedded_urls_remapped": False,
        "export_media_embedded_url_delivery": "requires-new-bundle-and-zip",
    }
