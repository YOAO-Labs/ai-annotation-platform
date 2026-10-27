"""Current training transports, captured separately from immutable owner facts."""

from contextlib import contextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import storage_access, training_dataset_storage as owners
from app.services import storage_migration_artifact_reads as artifact_reads
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.storage import StorageService, storage_service
from app.services.training_dataset_bridge import TrainingDatasetError


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
        # Deployment proxy/ML origins describe A. A registered B signs its own
        # endpoint; rewriting that URL through A would change its physical owner.
        if not self._legacy:
            kwargs["audience"] = "internal"
        return super().generate_download_url(*args, **kwargs)


@contextmanager
def service(target, *, accounting=None):
    """Open only inside an admitted operation; never replace the global client."""
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


def physical(target, callback, *args, accounting=None, **kwargs):
    """Run synchronous I/O under a caller-owned permit, including client close."""
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

    async def acquired(current):
        nonlocal permit
        permit = current
        if on_acquired is not None:
            await on_acquired(current)

    try:
        result = await storage_access.run(
            db,
            owner.location_id,
            purpose,
            physical,
            target,
            callback,
            *args,
            expected_revision=expected_revision,
            node_id=node_id,
            timeout=timeout,
            on_acquired=acquired if accounting is not None else on_acquired,
            **({"accounting": accounting} if accounting is not None else {}),
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
