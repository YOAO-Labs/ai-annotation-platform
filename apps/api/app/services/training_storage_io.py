"""Current training transports, captured separately from immutable owner facts."""

from contextlib import contextmanager

from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import storage_access
from app.services import storage_migration_artifact_reads as artifact_reads
from app.services import training_dataset_storage as owners
from app.services.content_snapshot import ContentError
from app.services.storage import StorageService, storage_service
from app.services.storage_identity import LEGACY_LOCATION_ID
from app.services.training_dataset_bridge import TrainingDatasetError
from app.services.training_saved_io import TrainingSavedIO


async def prepare(db, owner, *, require_enabled=True):
    """Close the independent metadata transaction before returning a sidecar."""
    async with async_sessionmaker(
        storage_access.engine(db), expire_on_commit=False
    )() as metadata:
        return await owners.target_for_ref(
            metadata, owner.ref, require_enabled=require_enabled
        )


class _Storage(StorageService):
    def generate_download_url(self, *args, **kwargs):
        if not self._legacy:
            kwargs["audience"] = "internal"
        return super().generate_download_url(*args, **kwargs)


@contextmanager
def service(target, *, accounting=None):
    """Open only inside an admitted operation; never replace the global client."""
    if getattr(target, "is_registered", False):
        raise ValueError("Registered training storage requires a Source admission")
    with target.client(**({"owned": True} if accounting is not None else {})) as client:
        yield _bound(
            client,
            target.bucket,
            legacy=target.location_id == LEGACY_LOCATION_ID,
            accounting=accounting,
        )


def _bound(client, bucket, *, legacy, accounting=None):
    bound = object.__new__(_Storage)
    bound.__dict__.update(
        (name, value)
        for name, value in storage_service.__dict__.items()
        if not callable(value)
    )
    bound.client = client
    bound.bucket = bound.training_bucket = bucket
    bound._write_accounting = accounting
    bound._legacy = legacy
    return bound


def head_object(transport, bucket, key):
    if isinstance(transport, TrainingSavedIO):
        return transport.head(key)
    return transport.client.head_object(Bucket=bucket, Key=key)


def get_object(transport, bucket, key, *, if_match=None, version=None):
    if isinstance(transport, TrainingSavedIO):
        return transport.download(key, if_match=if_match)
    params = {"Bucket": bucket, "Key": key}
    if if_match is not None:
        params["IfMatch"] = if_match
    if version is not None:
        params["VersionId"] = version
    return transport.client.get_object(**params)


def verify_upload(transport, key, *, bucket):
    if isinstance(transport, TrainingSavedIO):
        try:
            return transport.head(key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {
                "404",
                "NoSuchKey",
                "NoSuchVersion",
                "NotFound",
            }:
                return None
            raise
    return transport.verify_upload(key, bucket=bucket)


def sign_download(transport, key, **options):
    if isinstance(transport, TrainingSavedIO):
        return transport.sign_download(
            key,
            expires_in=options.get("expires_in", 3600),
            download_name=options.get("download_name"),
        )
    return transport.generate_download_url(key, **options)


def sign_upload(transport, bucket, key, *, size, metadata, expires_in):
    if isinstance(transport, TrainingSavedIO):
        return transport.sign_upload(key, expires_in=expires_in)
    return {
        "url": transport.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ContentLength": size,
                "Metadata": metadata,
            },
            ExpiresIn=expires_in,
        ),
        "headers": {"x-amz-meta-" + name: value for name, value in metadata.items()},
    }


def retire(transport, prefix, *, bucket):
    if isinstance(transport, TrainingSavedIO):
        return transport.retire(prefix)
    return transport.delete_prefix(prefix, bucket=bucket)


def discard_report(transport, bucket, key):
    if isinstance(transport, TrainingSavedIO):
        return transport.discard_report(key)
    return transport.client.delete_object(Bucket=bucket, Key=key)


def source_for(target, owner, permit, **facts):
    if getattr(target, "is_registered", False):
        return TrainingSavedIO(owner, permit, **facts)


async def read(
    db,
    owner,
    purpose,
    callback,
    *args,
    object_key,
    sha256,
    size_bytes=None,
    on_acquired=None,
    close_result=None,
    timeout=None,
    **kwargs,
):
    """Read one saved artifact without changing its owner or write transport.

    A missing mapping retains the original Gate and address. The read-only
    client translates exactly one admitted address to a verified target version.
    """
    owner.contains(object_key)
    try:
        route = await artifact_reads.resolve(
            db, owner.ref, object_key, sha256, size_bytes
        )
        if route is None:
            options = {}
            if on_acquired is not None:
                options["on_acquired"] = on_acquired
            if timeout is not None:
                options["timeout"] = timeout
            if owner.key_prefix:
                options["source_key"] = object_key
            return await run(
                db,
                owner,
                purpose,
                callback,
                *args,
                **options,
                **kwargs,
            )
        return await artifact_reads.run(
            db,
            route,
            purpose,
            lambda client: callback(
                _bound(client, owner.bucket, legacy=False), *args, **kwargs
            ),
            on_acquired=on_acquired,
            close_result=close_result,
            timeout=timeout,
        )
    except ContentError as exc:
        raise TrainingDatasetError(
            exc.code, "训练文件的迁移读取位置已变化，请重新读取", status_code=exc.status
        ) from None


def physical(target, callback, *args, accounting=None, source=None, **kwargs):
    """Run synchronous I/O under a caller-owned permit, including client close."""
    if source is not None:
        return callback(source, *args, **kwargs)
    with service(target, accounting=accounting) as bound:
        return callback(bound, *args, **kwargs)


async def run(
    db,
    owner,
    purpose,
    callback,
    *args,
    target=None,
    on_acquired=None,
    expected_revision=None,
    node_id=None,
    timeout=None,
    accounting=None,
    source_attempt=None,
    source_boot=None,
    source_deletion=None,
    source_key=None,
    **kwargs,
):
    """Register I/O without committing the caller's business transaction.

    Owners/inventory must already be durable or explicitly committed by the
    caller's on_acquired callback. Callbacks close bodies with storage_access's
    registered closing helpers so uncertain close retains the permit.
    """
    target = target or await prepare(db, owner)
    ref = owner.ref
    if (target.location_id, target.endpoint_url, target.bucket, target.key_prefix) != (
        owner.location_id,
        ref["endpoint_url"],
        owner.bucket,
        owner.key_prefix,
    ):
        owners._reject()
    permit, failure, result = None, None, None
    registered = getattr(target, "is_registered", False)

    async def acquired(current):
        nonlocal permit
        permit = current
        if on_acquired is not None:
            await on_acquired(current)

    def invoke(destination, operation, *arguments, **options):
        return physical(
            destination,
            operation,
            *arguments,
            source=source_for(
                target,
                owner,
                permit,
                attempt=source_attempt,
                boot=source_boot,
                deletion=source_deletion,
                read_key=source_key,
                accounting=accounting,
                timeout=timeout,
            ),
            **options,
        )

    try:
        result = await storage_access.run(
            db,
            owner.location_id,
            purpose,
            invoke if registered else physical,
            target,
            callback,
            *args,
            expected_revision=expected_revision,
            node_id=node_id,
            timeout=timeout,
            on_acquired=acquired
            if accounting is not None or registered
            else on_acquired,
            **(
                {"accounting": accounting}
                if accounting is not None and not registered
                else {}
            ),
            **kwargs,
        )
    except BaseException as exc:
        failure = exc
    if accounting is not None:
        from app.services.training_write_accounting import finish

        failure = await finish(
            accounting,
            failure,
            settled=permit is not None and getattr(permit, "_settled", False) is True,
        )
    if failure is not None:
        raise failure
    return result
