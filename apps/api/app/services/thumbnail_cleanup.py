"""Collect only unreferenced, settled thumbnail owners at their saved targets."""

from contextlib import contextmanager
from types import SimpleNamespace
import uuid

from botocore.exceptions import ClientError
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import object_removal_accounting as removal
from app.services import original_sources, storage_access, thumbnail_outputs as outputs
from app.services.content_snapshot import ContentError


def absent(exc):
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get(
        "Code"
    ) in {
        "NoSuchKey",
        "NoSuchVersion",
        "NotFound",
        "404",
    }


def remove(target, output, *, accounting=None, fixture=None):
    """Read back the receipt, then delete and prove absence of that exact version."""
    saved = outputs.owner(output)
    if fixture is not None:
        retirement, permit, versions = fixture
        with target.client(read_timeout=30, single_attempt=True, owned=True) as client:
            return outputs.media_cache_storage.retire_fixture_versions(
                client,
                saved,
                versions,
                lambda identifier, state: retirement.physical_checkpoint(
                    uuid.UUID(identifier), permit, state
                ),
            )
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
            raise storage_access.mark_unsettled(exc)
    return 1


async def retire_fixture(db, control, identifier):
    """Use this thumbnail owner for one sealed, exactly born fixture output."""
    from app.services.fixture_media_retirement import _retire_output

    def retire(target, output, fixture):
        return remove(target, output, fixture=fixture)

    await _retire_output(db, control, "thumbnail", identifier, retire)


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
    removed = 0
    async with factory() as current:
        identifiers = list(
            await current.scalars(
                text(
                    "SELECT id FROM yoao_assets_v1.legacy_thumbnail_gc_candidates("
                    ":limit, :now, :grace)"
                ),
                {"limit": limit, "now": current_time, "grace": grace_seconds},
            )
        )
        await current.commit()
        for identifier in identifiers:
            try:
                params = {"id": identifier, "now": current_time, "grace": grace_seconds}
                async with original_sources._reads(current, True):
                    claim = await current.scalar(
                        text(
                            "SELECT yoao_assets_v1.legacy_thumbnail_gc_start("
                            ":id, :now, :grace)"
                        ),
                        params,
                    )
                if claim is None:
                    await current.rollback()
                    continue
                if claim["action"] == "checkpoint":
                    await current.commit()
                    continue
                output = outputs._facts(claim["output"])
                saved = outputs.owner(output)
                params["receipt"] = outputs._receipt(output)
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
                        admitted_output = await current.scalar(
                            text(
                                "SELECT yoao_assets_v1.legacy_thumbnail_gc_admit("
                                ":id, :receipt, :permit, :now, :grace)"
                            ).bindparams(bindparam("receipt", type_=JSONB)),
                            {**params, "permit": permit.id},
                        )
                    if admitted_output is None:
                        raise ContentError("thumbnail_output_changed")
                    output = outputs._facts(admitted_output)
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
                    finished = await current.scalar(
                        text(
                            "SELECT yoao_assets_v1.legacy_thumbnail_gc_finish("
                            ":id, :receipt, :now, :grace)"
                        ).bindparams(bindparam("receipt", type_=JSONB)),
                        params,
                    )
                if not finished:
                    raise ContentError("thumbnail_output_changed")
                await current.commit()
                removed += count
            except (
                ContentError,
                outputs.media_cache_storage.MediaCacheStorageError,
                ClientError,
            ):
                await current.rollback()
    return removed
