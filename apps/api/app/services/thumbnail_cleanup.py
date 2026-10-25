"""Collect only unreferenced, settled thumbnail owners at their saved targets."""

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

from botocore.exceptions import ClientError
from sqlalchemy import String, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_source import DatasetSourcePreview
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.task import Task
from app.db.models.thumbnail_output import ThumbnailOutput as Output
from app.services import object_removal_accounting as removal
from app.services import original_sources, storage_access, thumbnail_outputs as outputs
from app.services.content_snapshot import ContentError


def unreferenced(identifier):
    return (
        ~exists().where(DatasetItem.thumbnail_output_id == identifier)
        & ~exists().where(Task.thumbnail_output_id == identifier)
        & ~exists().where(DatasetSourcePreview.thumbnail_output_id == identifier)
    )


async def in_use(db, identifier, except_id=None):
    statement = select(
        exists().where(StorageOperation.purpose == outputs.purpose(identifier))
    )
    if except_id is not None:
        statement = select(
            exists().where(
                StorageOperation.purpose == outputs.purpose(identifier),
                StorageOperation.id != except_id,
            )
        )
    return bool(await db.scalar(statement))


async def eligible(db, identifier, *, except_id=None):
    return bool(await db.scalar(select(unreferenced(identifier)))) and not await in_use(
        db, identifier, except_id
    )


def absent(exc):
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get(
        "Code"
    ) in {
        "NoSuchKey",
        "NoSuchVersion",
        "NotFound",
        "404",
    }


def remove(target, output, *, accounting=None):
    """Read back the receipt, then delete and prove absence of that exact version."""
    saved = outputs.owner(output)
    params = {"Bucket": saved.bucket, "Key": saved.key(outputs.RELATIVE_KEY)}
    if output.version_id not in (None, "null"):
        params["VersionId"] = output.version_id
    with target.client(
        read_timeout=30, **({"owned": True} if accounting is not None else {})
    ) as client:
        try:
            head = client.head_object(**params, IfMatch=output.etag)
        except ClientError as exc:
            if absent(exc):
                return 0
            raise
        observed_version = head.get("VersionId")
        if (
            head.get("DeleteMarker")
            or head.get("ETag") != output.etag
            or head.get("ContentLength") != output.size_bytes
            or output.version_id in (None, "null")
            and observed_version not in (None, "null")
            or output.version_id not in (None, "null")
            and observed_version != output.version_id
        ):
            raise ContentError("thumbnail_content_changed")

        @contextmanager
        def borrowed(**kwargs):
            yield client

        outputs._verified_bytes(SimpleNamespace(client=borrowed), output)
        if observed_version is not None:
            params["VersionId"] = observed_version
        observation = (
            removal.Observation(
                accounting, client, params["Key"], params.get("VersionId")
            )
            if accounting is not None
            else None
        )

        def call():
            return client.delete_object(**params, IfMatch=output.etag)

        try:
            try:
                deleted = (
                    observation.submit(call) if observation is not None else call()
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {
                    "AccessDenied",
                    "InvalidAccessKeyId",
                    "SignatureDoesNotMatch",
                    "403",
                    "PreconditionFailed",
                    "412",
                }:
                    raise ContentError("thumbnail_cleanup_refused", 503) from None
                raise
            try:
                response = client.get_object(**params)
            except ClientError as exc:
                if not absent(exc):
                    raise
            else:
                storage_access.close_registered(response["Body"])
                raise ContentError("thumbnail_cleanup_unproven")
            if observation is not None:
                observation.complete(deleted)
        except ContentError as exc:
            if exc.code == "thumbnail_cleanup_refused":
                raise
            raise storage_access.mark_unsettled(exc)
        except BaseException as exc:
            # A lost delete response or failed absence proof cannot settle the
            # independent physical operation or discard this owner's receipt.
            raise storage_access.mark_unsettled(exc)
    return 1


async def reconcile(db, *, limit=100, grace_seconds=3600):
    if (
        type(limit) is not int
        or not 1 <= limit <= 1000
        or type(grace_seconds) is not int
        or grace_seconds < 3600
    ):
        raise ContentError("thumbnail_gc_invalid")
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    current_time = outputs.now()
    cutoff = current_time - timedelta(seconds=grace_seconds)
    removed = 0
    async with factory() as current:
        identifiers = list(
            await current.scalars(
                select(Output.id)
                .where(
                    Output.state.in_(["ready", "retiring", "failed"]),
                    Output.uncertain_write.is_(False),
                    ~exists().where(
                        StorageOperation.purpose
                        == "thumbnail_" + func.replace(cast(Output.id, String), "-", "")
                    ),
                    Output.next_check_at <= current_time,
                    or_(Output.state == "retiring", Output.updated_at <= cutoff),
                    unreferenced(Output.id),
                )
                .order_by(Output.next_check_at, Output.id)
                .limit(limit)
            )
        )
        await current.commit()
        for identifier in identifiers:
            try:
                async with original_sources._reads(current, True):
                    output = await current.get(
                        Output,
                        identifier,
                        populate_existing=True,
                        with_for_update={"nowait": True},
                    )
                if (
                    output is None
                    or output.state == "deleted"
                    or output.writer_until > current_time
                    or not await eligible(current, identifier)
                ):
                    await current.rollback()
                    continue
                if output.uncertain_write:
                    await current.rollback()
                    continue
                if output.sha256 is None:
                    # A refused conditional PUT never proved ownership of any
                    # pre-existing object at that UUID, so do not delete a key.
                    if output.state == "failed":
                        output.state = "deleted"
                        await current.commit()
                    else:
                        await current.rollback()
                    continue
                if output.state == "ready":
                    output.state, output.retired_at = "retiring", current_time
                    output.next_check_at = current_time + timedelta(
                        seconds=grace_seconds
                    )
                    await current.commit()
                    continue
                if (
                    output.state != "retiring"
                    or output.retired_at is None
                    or output.retired_at > cutoff
                ):
                    await current.rollback()
                    continue
                saved = outputs.owner(output)
                expected = (
                    output.storage_ref,
                    output.sha256,
                    output.size_bytes,
                    output.etag,
                    output.version_id,
                )
                revision = await storage_access.check(current, saved.location_id)
                await current.commit()
                recorder = removal.recorder(
                    current,
                    output,
                    producer="thumbnail_removals",
                    purpose="media_cache",
                )
                owned_permit = None

                async def admitted(permit):
                    nonlocal output, target, owned_permit
                    owned_permit = permit
                    async with original_sources._reads(current, True):
                        output = await current.get(
                            Output,
                            identifier,
                            populate_existing=True,
                            with_for_update={"nowait": True},
                        )
                    if (
                        output is None
                        or output.state != "retiring"
                        or output.uncertain_write
                        or (
                            output.storage_ref,
                            output.sha256,
                            output.size_bytes,
                            output.etag,
                            output.version_id,
                        )
                        != expected
                        or not await eligible(current, identifier, except_id=permit.id)
                    ):
                        raise ContentError("thumbnail_output_changed")
                    target = await outputs.media_cache_storage.target_for_ref(
                        current, saved.ref, require_enabled=False
                    )
                    await storage_access.check(
                        current,
                        saved.location_id,
                        expected_revision=permit.revision,
                        lock=True,
                    )
                    await current.commit()

                target = None
                failure, count = None, None
                try:
                    count = await storage_access.run(
                        current,
                        saved.location_id,
                        outputs.purpose(identifier),
                        lambda: remove(target, output, accounting=recorder),
                        expected_revision=revision,
                        on_acquired=admitted,
                        timeout=outputs.WRITE_SECONDS,
                    )
                except BaseException as exc:
                    failure = exc
                failure = await removal.finish(
                    recorder,
                    failure,
                    settled=owned_permit is not None
                    and getattr(owned_permit, "_settled", False) is True,
                )
                if failure is not None:
                    raise failure
                async with original_sources._reads(current, True):
                    output = await current.get(
                        Output,
                        identifier,
                        populate_existing=True,
                        with_for_update={"nowait": True},
                    )
                if (
                    output is None
                    or output.state != "retiring"
                    or not await eligible(current, identifier)
                    or (
                        output.storage_ref,
                        output.sha256,
                        output.size_bytes,
                        output.etag,
                        output.version_id,
                    )
                    != expected
                ):
                    raise ContentError("thumbnail_output_changed")
                output.state = "deleted"
                await current.commit()
                removed += count
            except (
                ContentError,
                outputs.media_cache_storage.MediaCacheStorageError,
                ClientError,
            ):
                await current.rollback()
    return removed
