"""Source-owned, measured WebP derivatives behind current manager authority."""

import hashlib
import asyncio
from dataclasses import dataclass, replace
from types import SimpleNamespace
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.db.models.dataset_source import DatasetSourcePreview, DatasetSourceItem
from app.db.models.dataset_asset import DatasetAsset, StorageLocation
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services import directory_policy as policy
from app.services import storage_access, storage_fault_events, storage_location
from app.services import storage_observation_rates as rates
from app.services import thumbnail_outputs as outputs
from app.services import original_sources
from app.services.content_snapshot import ContentError, capture
from app.services.dataset_content import key_authority, stored_content
from app.services.storage import storage_service

SPEC = "webp-256-exif-q80"


async def finish_io(call, *args, **kwargs):
    """The caller owns files and keys until the non-cancellable thread finishes."""
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Consume an I/O failure without masking cancellation.
        raise


async def encode(snapshot):
    from app.workers.media import _thumbnail_from_path

    snapshot.file.seek(0)
    try:
        body, _ = await finish_io(_thumbnail_from_path, snapshot.file)
        return body
    finally:
        snapshot.file.seek(0)


def cached_bytes_match(row):
    response = storage_service.client.get_object(Bucket=row.bucket, Key=row.object_key)
    body = response["Body"]
    try:
        content = body.read(2 * 1024 * 1024 + 1)
        return (
            len(content) == row.size_bytes
            and len(content) <= 2 * 1024 * 1024
            and hashlib.sha256(content).hexdigest() == row.sha256
        )
    finally:
        body.close()


async def delete_owned(objects):
    for bucket, key in objects:
        if bucket != storage_service.media_cache_bucket or not key.startswith(
            "directory-previews/"
        ):
            continue
        try:
            await finish_io(storage_service.delete_object, key, bucket=bucket)
        except Exception:
            # Retain the original failure. Failed cleanup requires operator
            # reconciliation; never broaden deletion to another owner prefix.
            import logging

            logging.getLogger(__name__).warning(
                "Directory object cleanup deferred: %s", key
            )


def directory_facts(source_id, path_digest, sha256):
    return {
        "source_id": str(source_id),
        "path_digest": path_digest,
        "source_sha256": sha256,
        "spec": SPEC,
    }


def preview_facts(row):
    if row is None:
        return None
    return (
        row.generation,
        row.thumbnail_output_id,
        row.state,
        row.asset_id,
        row.source_sha256,
        row.spec,
        row.location_id,
        row.bucket,
        row.object_key,
        row.sha256,
        row.size_bytes,
    )


@dataclass(frozen=True)
class PreparedPreview:
    directory: dict
    revisions: tuple
    baseline: tuple | None
    reuse: bool = False
    output_id: object = None
    rate_attempt: object = None


async def prepare(db, source, entry, snapshot):
    """Seal candidates before the caller takes source and business locks."""
    location = await db.get(StorageLocation, source.location_id, populate_existing=True)
    if location is None or not policy.permits_preview(
        policy.effective_cache_policy(source, location)
    ):
        return None
    row = await db.get(
        DatasetSourcePreview, (source.id, entry.path_digest), populate_existing=True
    )
    prepared = PreparedPreview(
        directory_facts(source.id, entry.path_digest, snapshot.sha256),
        (source.config_revision, source.policy_revision, location.config_revision),
        preview_facts(row),
    )
    if (
        row
        and row.state == "ready"
        and row.source_sha256 == snapshot.sha256
        and row.spec == SPEC
    ):
        try:
            if row.thumbnail_output_id is not None:
                await output_for_row(db, row)
                await outputs.read_bytes(db, row.thumbnail_output_id)
                return PreparedPreview(
                    prepared.directory,
                    prepared.revisions,
                    prepared.baseline,
                    reuse=True,
                )
            from app.services.dataset_assets import LEGACY_LOCATION_ID

            if (
                row.location_id == LEGACY_LOCATION_ID
                and row.bucket == storage_service.media_cache_bucket
            ):
                from sqlalchemy.ext.asyncio import async_sessionmaker

                factory = async_sessionmaker(
                    storage_access.engine(db), expire_on_commit=False
                )
                async with factory() as current:
                    matches = await storage_access.run(
                        current,
                        LEGACY_LOCATION_ID,
                        "directory_preview_verify",
                        cached_bytes_match,
                        row,
                    )
                if matches:
                    return PreparedPreview(
                        prepared.directory,
                        prepared.revisions,
                        prepared.baseline,
                        reuse=True,
                    )
        except ContentError as exc:
            if exc.code.startswith("read_publication_"):
                raise
        except Exception:
            pass
    prepared = replace(
        prepared,
        rate_attempt=rates.begin(location.id, source.id, "preview_generate"),
    )
    try:
        body = await encode(snapshot)
        output = await outputs.create(
            db,
            owner_kind="directory_preview",
            owner_id=source.id,
            path_digest=entry.path_digest,
            input_ref={"directory": prepared.directory},
            body=body,
        )
        return PreparedPreview(
            prepared.directory,
            prepared.revisions,
            prepared.baseline,
            output_id=output.id,
            rate_attempt=prepared.rate_attempt,
        )
    except Exception as exc:
        # The verified original can still be registered. The durable output owns
        # any uncertain physical write independently of this business transaction.
        if (
            getattr(exc, "storage_operation_unsettled", False)
            or isinstance(
                exc,
                (
                    HTTPException,
                    SQLAlchemyError,
                    outputs.media_cache_storage.MediaCacheStorageError,
                ),
            )
            or isinstance(exc, ContentError)
            and (
                exc.code.startswith("storage_")
                or exc.code in {"configuration_changed", "thumbnail_output_changed"}
            )
        ):
            await rates.finish(db, prepared.rate_attempt, None)
        return prepared
    except BaseException:
        await rates.finish(db, prepared.rate_attempt, None)
        raise


async def output_for_row(db, row):
    output = await db.get(
        ThumbnailOutput, row.thumbnail_output_id, populate_existing=True
    )
    if (
        output is None
        or output.state != "ready"
        or output.uncertain_write
        or output.owner_kind != "directory_preview"
        or output.owner_id != row.source_id
        or output.path_digest != row.path_digest
        or output.input_ref.get("directory")
        != directory_facts(row.source_id, row.path_digest, row.source_sha256)
    ):
        raise ContentError("preview_binding_changed")
    saved = outputs.owner(output)
    if (row.location_id, row.bucket, row.object_key, row.sha256, row.size_bytes) != (
        saved.location_id,
        saved.bucket,
        saved.key(outputs.RELATIVE_KEY),
        output.sha256,
        output.size_bytes,
    ):
        raise ContentError("preview_binding_changed")
    await outputs.qualify_read(db, output)
    return output


async def retire_output(db, output_id):
    if output_id is not None:
        try:
            async with original_sources._reads(db, True):
                output = await db.get(
                    ThumbnailOutput,
                    output_id,
                    with_for_update={"nowait": True},
                    populate_existing=True,
                )
        except ContentError as exc:
            if exc.code == "storage_original_busy":
                return
            raise
        if output is not None and output.state == "ready":
            output.state, output.retired_at = "retiring", outputs.now()
            output.next_check_at = outputs.now()


async def generate(
    db,
    location,
    source,
    mapping,
    snapshot,
    uploaded,
    retired,
    *,
    actor_id=None,
    run_id=None,
    job_id=None,
    registration_id=None,
    fault_node_id=None,
    fault_anchor=None,
    fault_maintenance_revision=1,
    fault_validate=None,
    prepared=None,
):
    if not policy.permits_preview(policy.effective_cache_policy(source, location)):
        return None

    async def observe(binding=None):
        dependencies = None
        if binding is not None:
            from app.services.storage_fault_state import digest

            dependencies = {
                "source_policy_revision": source.policy_revision,
                "source_policy": source.cache_policy,
                "location_policy": location.config.get("cache_policy", "preview"),
                "binding": [
                    str(binding.location_id),
                    binding.generation,
                    digest(binding.bucket),
                ],
            }
        return await storage_fault_events.prepare(
            db,
            location=location,
            source=source,
            operation="preview_generate",
            actor_id=actor_id,
            node_id=fault_node_id or storage_location.api_node_id(),
            anchor=fault_anchor,
            maintenance_revision=fault_maintenance_revision,
            path_digest=mapping.path_digest,
            expected=snapshot.sha256,
            asset_id=mapping.asset_id,
            preview_spec=SPEC,
            preview_dependencies=dependencies,
            item_id=mapping.dataset_item_id,
            run_id=run_id,
            job_id=job_id,
            registration_id=registration_id,
        )

    observation = await observe()
    row = await db.get(DatasetSourcePreview, (source.id, mapping.path_digest))
    valid = (
        isinstance(prepared, PreparedPreview)
        and prepared.directory
        == directory_facts(source.id, mapping.path_digest, snapshot.sha256)
        and prepared.revisions
        == (source.config_revision, source.policy_revision, location.config_revision)
    )
    if (
        valid
        and prepared.reuse
        and preview_facts(row) == prepared.baseline
        and row.asset_id == mapping.asset_id
    ):
        try:
            if row.thumbnail_output_id is not None:
                await output_for_row(db, row)
            else:
                from app.services.dataset_assets import LEGACY_LOCATION_ID

                await storage_access.check(db, LEGACY_LOCATION_ID)
            row._fault_observation = observation
            return row
        except Exception:
            pass
    if row and row.object_key:
        if row.thumbnail_output_id is not None:
            await retire_output(db, row.thumbnail_output_id)
        else:
            retired.append((row.bucket, row.object_key))
        row.thumbnail_output_id = None
        row.object_key = row.bucket = row.location_id = row.sha256 = row.size_bytes = (
            None
        )
    if row is None:
        row = DatasetSourcePreview(
            source_id=source.id, path_digest=mapping.path_digest, generation=0
        )
        db.add(row)
    row.asset_id, row.source_sha256, row.spec = mapping.asset_id, snapshot.sha256, SPEC
    row.policy_revision = source.policy_revision
    row.generation += 1
    row.measured_at = datetime.now(timezone.utc)
    row.state, row.error_code = "failed", "preview_generation_failed"
    try:
        if not valid or prepared.output_id is None:
            raise ContentError("preview_preparation_failed")
        async with original_sources._reads(db, True):
            output = await db.get(
                ThumbnailOutput,
                prepared.output_id,
                populate_existing=True,
                with_for_update={"read": True, "nowait": True},
            )
        if (
            output is None
            or output.state != "ready"
            or output.uncertain_write
            or output.owner_kind != "directory_preview"
            or output.owner_id != source.id
            or output.path_digest != mapping.path_digest
            or output.input_ref.get("directory") != prepared.directory
        ):
            raise ContentError("preview_preparation_changed")
        saved = outputs.owner(output)
        await outputs.media_cache_storage.target_for_ref(db, saved.ref)
        await storage_access.check(db, saved.location_id)
        observation = await observe(
            SimpleNamespace(
                location_id=saved.location_id,
                generation=saved.ref["binding_generation"],
                bucket=saved.bucket,
            )
        )
        row.thumbnail_output_id = output.id
        row.location_id, row.bucket, row.object_key = (
            saved.location_id,
            saved.bucket,
            saved.key(outputs.RELATIVE_KEY),
        )
        row.sha256, row.size_bytes = output.sha256, output.size_bytes
        row.state, row.error_code = "ready", None
    except Exception:
        # A derivative failure does not roll back an already verified original.
        if fault_validate is not None:
            try:
                await fault_validate()
            except Exception:
                return row
        await storage_fault_events.report(
            db,
            None,
            operation="preview_generate",
            observation=observation,
            preview=True,
            actor_id=actor_id,
            location_id=location.id,
            source_id=source.id,
            dataset_id=source.dataset_id,
            item_id=mapping.dataset_item_id,
            asset_id=mapping.asset_id,
            run_id=run_id,
            job_id=job_id,
            registration_id=registration_id,
        )
    row._fault_observation = observation
    return row


async def preview_identity(db, source_id, path_digest, actor):
    from app.services.dataset_source_access import require_source
    from app.db.models.dataset_asset import StorageLocation
    from app.services.dataset_assets import LEGACY_LOCATION_ID

    source = await require_source(db, source_id, actor)
    location = await db.get(StorageLocation, source.location_id, populate_existing=True)
    if (
        not source.enabled
        or source.unregistered_at
        or location is None
        or not location.enabled
        or not policy.permits_preview(policy.effective_cache_policy(source, location))
    ):
        raise ContentError("preview_unavailable")
    mapping = await db.get(
        DatasetSourceItem, (source.id, path_digest), populate_existing=True
    )
    row = await db.get(
        DatasetSourcePreview, (source.id, path_digest), populate_existing=True
    )
    asset = (
        await db.get(DatasetAsset, mapping.asset_id, populate_existing=True)
        if mapping
        else None
    )
    if (
        not row
        or row.state != "ready"
        or not mapping
        or not asset
        or row.source_sha256 != mapping.content_sha256
        or asset.checksum_value != row.source_sha256
        or row.asset_id != asset.id
        or row.spec != SPEC
    ):
        raise ContentError("preview_unavailable")
    if row.thumbnail_output_id is not None:
        await output_for_row(db, row)
    elif (
        row.location_id != LEGACY_LOCATION_ID
        or row.bucket != storage_service.media_cache_bucket
    ):
        raise ContentError("preview_binding_changed")
    else:
        await storage_access.check(db, LEGACY_LOCATION_ID)
    # This deliberately does not touch the external source or its probe: the
    # derivative names an old SHA and is only a read-only management preview.
    return (
        row.bucket,
        row.object_key,
        row.size_bytes,
        row.sha256,
        row.generation,
        source.config_revision,
        location.config_revision,
        row.thumbnail_output_id,
    )


async def read_preview(db, source_id, path_digest, actor, api_key_id=None):
    identity = await preview_identity(db, source_id, path_digest, actor)
    proof = await key_authority(db, actor, api_key_id, "datasets:read")
    await db.commit()
    bucket, key, size, sha, *_ = identity
    if identity[-1] is not None:
        import io
        from contextlib import contextmanager

        body = await outputs.read_bytes(db, identity[-1])

        @contextmanager
        def content():
            with io.BytesIO(body) as stream:
                yield stream, {"size": len(body)}

        snapshot = await capture(content, sha)
    else:
        from app.services.dataset_assets import LEGACY_LOCATION_ID

        async with await storage_access.acquire(
            db, LEGACY_LOCATION_ID, "directory_preview_read"
        ):
            snapshot = await capture(lambda: stored_content(bucket, key, size), sha)
    try:
        if identity != await preview_identity(db, source_id, path_digest, actor):
            raise ContentError("preview_binding_changed")
        if proof != await key_authority(db, actor, api_key_id, "datasets:read"):
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        await db.commit()
        return snapshot
    except BaseException:
        snapshot.close()
        raise
