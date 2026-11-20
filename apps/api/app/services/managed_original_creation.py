"""Durable allocation, conditional creation and publication without final deletion."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from urllib.parse import urlsplit
import uuid

import boto3
from botocore.config import Config
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.managed_original_creation import ManagedOriginalCreation as Creation
from app.db.models.storage_maintenance import StorageOperation
from app.services import (
    annotations_storage,
    dataset_storage,
    storage_access,
    storage_usage_accounting as accounting,
)
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import (
    LEGACY_LOCATION_ID,
    PURPOSE_BUCKET_ATTRIBUTES,
    ensure_legacy_bindings,
    validate_legacy_bindings,
)
from app.services.dataset_storage_keys import (
    managed_dataset_key,
    normalize_dataset_relative_path,
)
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service

ORIGINS = {"direct", "zip", "connector", "directory_copy"}
TERMINAL = {"verified", "failed", "conflict", "unknown"}
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")
STAGING_PREFIX = "__aap_upload_staging__"
TASK_ORIGINAL_PREFIX = "__aap_task_originals__"
AUXILIARY_ORIGINAL_PREFIX = "__aap_auxiliary_originals__"
ORIGINAL_STORAGE_PURPOSES = frozenset({"datasets", "annotations"})


@dataclass(frozen=True)
class CreatedOriginal:
    creation_id: uuid.UUID
    sha256: str
    size_bytes: int
    md5: str
    etag: str
    version_id: str | None
    head_bytes: bytes


def now():
    return datetime.now(timezone.utc)


def reject(code, status=409):
    raise ContentError(code, status)


def _json(value):
    return json.loads(json.dumps(value, default=str))


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def create_client(*, timeout_seconds=60):
    """Each physical operation owns one nonretrying, same-origin client."""
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    target = urlsplit(endpoint)

    def guard(request, **kwargs):
        destination = urlsplit(request.url)
        if (destination.scheme, destination.netloc) != (target.scheme, target.netloc):
            reject("managed_original_redirect_denied")

    try:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            config=Config(
                connect_timeout=2,
                read_timeout=max(10, min(timeout_seconds, 3600)),
                retries={"total_max_attempts": 1, "mode": "standard"},
                s3={"addressing_style": "path"},
                proxies={},
                signature_version="s3v4",
            ),
        )
        client.meta.events.register("before-send.s3", guard)
        return client
    except BaseException:
        raise storage_access.mark_unsettled(
            ContentError("managed_original_client_unknown", 503)
        ) from None


async def _configuration(db, *, lock=False):
    if lock:
        await db.execute(
            select(StorageLocation.id)
            .where(StorageLocation.id == LEGACY_LOCATION_ID)
            .with_for_update(read=True)
        )
        await db.execute(
            select(StorageBinding.purpose)
            .where(StorageBinding.purpose.in_(ORIGINAL_STORAGE_PURPOSES))
            .order_by(StorageBinding.purpose)
            .with_for_update(read=True)
        )
    if await validate_legacy_bindings(db):
        reject("managed_original_configuration_changed")
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    if location is None or not location.enabled:
        reject("managed_original_configuration_changed")
    bindings = list(
        await db.scalars(
            select(StorageBinding)
            .where(StorageBinding.purpose.in_(ORIGINAL_STORAGE_PURPOSES))
            .order_by(StorageBinding.purpose)
            .execution_options(populate_existing=True)
        )
    )
    return _json(
        {
            "endpoint": f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
            "buckets": {
                purpose: getattr(storage_service, attr)
                for purpose, attr in PURPOSE_BUCKET_ATTRIBUTES.items()
                if purpose in ORIGINAL_STORAGE_PURPOSES
            },
            "location": {
                column.key: getattr(location, column.key)
                for column in StorageLocation.__table__.columns
            },
            "bindings": [
                {
                    column.key: getattr(row, column.key)
                    for column in StorageBinding.__table__.columns
                }
                for row in bindings
            ],
        }
    )


async def _read(db, creation_id):
    async with storage_access.engine(db).connect() as connection:
        row = (
            (
                await connection.execute(
                    select(Creation.__table__).where(Creation.id == creation_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row else None


async def _change(db, creation_id, expected, *, record_mutation=False, **values):
    """No independent ledger transaction spans executor or network I/O."""
    async with storage_access.engine(db).begin() as connection:
        row = (
            (
                await connection.execute(
                    update(Creation)
                    .where(
                        Creation.id == creation_id,
                        Creation.status.in_(
                            [expected] if isinstance(expected, str) else expected
                        ),
                    )
                    .values(**values)
                    .returning(*Creation.__table__.columns)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            reject("managed_original_creation_state_conflict")
        if record_mutation:
            handle = await accounting.begin_in_transaction(
                connection,
                endpoint_url=row["configuration"]["endpoint"],
                bucket=row["bucket"],
                object_key=row["object_key"],
                producer="managed_original_creation",
                owner_id=row["id"],
                purpose="managed_original",
                kind="create",
                nonce=str(row["id"]),
                new_request=True,
                evidence={"owner_kind": row["owner_kind"], "origin": row["origin"]},
            )
            return dict(row), handle
        return dict(row)


async def abandon_unstarted_fixture(db, *, run_id, admission_id, creation_id):
    """CAS an exact fixture allocation to failed only before any write can start."""
    from app.db.models.fixture_run import FixtureRunResource

    owner = (
        select(FixtureRunResource.resource_id)
        .where(
            FixtureRunResource.run_id == run_id,
            FixtureRunResource.admission_id == admission_id,
            FixtureRunResource.kind == "creation",
            FixtureRunResource.creation_id == creation_id,
        )
        .exists()
    )
    async with storage_access.engine(db).begin() as connection:
        result = await connection.execute(
            update(Creation)
            .where(
                Creation.id == creation_id,
                Creation.status == "allocated",
                Creation.operation_id.is_(None),
                Creation.prepared.is_(None),
                Creation.write_started_at.is_(None),
                Creation.receipt.is_(None),
                owner,
            )
            .values(
                status="failed",
                error_code="fixture_item_not_prepared",
                completed_at=now(),
            )
            .returning(Creation.id)
        )
        if result.scalar_one_or_none() is None:
            reject("fixture_creation_not_unstarted")


async def abandon_unstarted_fixture_auxiliary(db, *, run_id, admission_id, creation_id):
    """Abandon one exact auxiliary intent only while no physical write can exist."""
    from app.db.models.fixture_run import FixtureAuxiliaryIntent

    owner = (
        select(FixtureAuxiliaryIntent.creation_id)
        .where(
            FixtureAuxiliaryIntent.run_id == run_id,
            FixtureAuxiliaryIntent.admission_id == admission_id,
            FixtureAuxiliaryIntent.creation_id == creation_id,
        )
        .exists()
    )
    async with storage_access.engine(db).begin() as connection:
        result = await connection.execute(
            update(Creation)
            .where(
                Creation.id == creation_id,
                Creation.owner_kind == "dataset_auxiliary",
                Creation.status == "allocated",
                Creation.operation_id.is_(None),
                Creation.prepared.is_(None),
                Creation.write_started_at.is_(None),
                Creation.receipt.is_(None),
                owner,
            )
            .values(
                status="failed",
                error_code="fixture_auxiliary_not_started",
                completed_at=now(),
            )
            .returning(Creation.id)
        )
        if result.scalar_one_or_none() is None:
            reject("fixture_creation_not_unstarted")


def _original_configuration(configuration):
    """Project old and current snapshots onto this writer's actual dependencies."""
    return {
        **configuration,
        "buckets": {
            purpose: bucket
            for purpose, bucket in configuration.get("buckets", {}).items()
            if purpose in ORIGINAL_STORAGE_PURPOSES
        },
        "bindings": [
            binding
            for binding in configuration.get("bindings", [])
            if binding.get("purpose") in ORIGINAL_STORAGE_PURPOSES
        ],
    }


def original_ref(row):
    configuration = row["configuration"]
    return (
        configuration.get("original_ref") if configuration.get("schema") == 2 else None
    )


def _original_storage(row):
    return annotations_storage if row["owner_kind"] == "task" else dataset_storage


def original_locator(row):
    try:
        storage = _original_storage(row)
        return storage.resolve_ref(
            original_ref(row) or storage.legacy_ref(row["bucket"], row["object_key"])
        )
    except (
        dataset_storage.DatasetStorageError,
        annotations_storage.AnnotationsStorageError,
    ) as exc:
        raise ContentError(exc.code, exc.status_code) from None


async def _validate(db, row, *, lock=False):
    saved = row["configuration"]
    if _digest(saved) != row["input_fingerprint"]:
        reject("managed_original_configuration_changed")
    reference = original_ref(row)
    if reference is not None:
        if row["owner_kind"] not in {"dataset", "dataset_auxiliary", "task"}:
            reject("managed_original_configuration_changed")
        owner = original_locator(row)
        if any(
            getattr(owner, name) != row[name]
            for name in ("location_id", "bucket", "object_key", "storage_identity")
        ):
            reject("managed_original_configuration_changed")
        private_target = saved.get("fixture_original_target")
        if private_target is not None:
            from app.db.models.fixture_run import (
                FixtureOriginalTarget,
                FixtureRun,
                FixtureRunResource,
            )

            try:
                target_id = uuid.UUID(private_target["id"])
                generation = private_target["generation"]
                receipt = private_target["store_receipt_sha256"]
            except (KeyError, TypeError, ValueError):
                reject("fixture_original_storage_changed")
            target = await db.get(FixtureOriginalTarget, target_id)
            owner_resource = await db.scalar(
                select(FixtureRunResource).where(
                    FixtureRunResource.resource_id == row["id"],
                    FixtureRunResource.kind.in_(("creation", "auxiliary")),
                )
            )
            run = (
                await db.get(FixtureRun, owner_resource.run_id)
                if owner_resource
                else None
            )
            root = (
                await db.get(FixtureRun, run.parent_run_id or run.id) if run else None
            )
            if (
                target is None
                or root is None
                or root.original_target_id != target.id
                or target.id != target_id
                or target.generation != generation
                or target.store_receipt_sha256 != receipt
                or root.original_store_receipt_sha256 != receipt
                or target.location_id != owner.location_id
                or target.bucket != owner.bucket
                or target.storage_identity != owner.storage_identity
                or root.original_storage_identity != target.storage_identity
                or owner.binding_generation != generation
            ):
                reject("fixture_original_storage_changed")
        try:
            from app.services.original_sources import _reads

            # Publication may already own source/business rows. Recover a busy
            # target without reversing an administrator's Connection→Location
            # lock order or ending the caller's publication transaction.
            async with _reads(db, lock):
                if lock:
                    from app.db.models.storage_connection import StorageConnection

                    location = await db.get(
                        StorageLocation, owner.location_id, populate_existing=True
                    )
                    connection_id = location.connection_id if location else None
                    if connection_id is not None:
                        await db.execute(
                            select(StorageConnection.id)
                            .where(StorageConnection.id == connection_id)
                            .with_for_update(read=True, nowait=True)
                        )
                    await db.execute(
                        select(StorageLocation.id)
                        .where(StorageLocation.id == owner.location_id)
                        .with_for_update(read=True, nowait=True)
                    )
                await _original_storage(row).target_for_ref(db, reference)
        except (
            dataset_storage.DatasetStorageError,
            annotations_storage.AnnotationsStorageError,
        ) as exc:
            raise ContentError(exc.code, exc.status_code) from None
    else:
        if saved.get("schema") == 2:
            reject("managed_original_configuration_changed")
        purpose = "annotations" if row["owner_kind"] == "task" else "datasets"
        if lock:
            await db.execute(
                select(StorageLocation.id)
                .where(StorageLocation.id == LEGACY_LOCATION_ID)
                .with_for_update(read=True)
            )
        location = await db.get(
            StorageLocation, LEGACY_LOCATION_ID, populate_existing=True
        )
        selected = [
            binding
            for binding in saved.get("bindings", [])
            if binding.get("purpose") == purpose
        ]
        if (
            location is None
            or not location.enabled
            or row["location_id"] != LEGACY_LOCATION_ID
            or saved.get("location")
            != _json(
                {
                    column.key: getattr(location, column.key)
                    for column in StorageLocation.__table__.columns
                }
            )
            or saved.get("endpoint") != dataset_storage._runtime_endpoint()
            or saved.get("buckets", {}).get(purpose) != row["bucket"]
            or len(selected) != 1
            or selected[0].get("location_id") != str(LEGACY_LOCATION_ID)
            or selected[0].get("bucket") != row["bucket"]
            or selected[0].get("generation") != 1
            or row["storage_identity"]
            != managed_storage_identity(saved["endpoint"], row["bucket"])
        ):
            reject("managed_original_configuration_changed")
    if row["origin"] == "direct":
        staging_owner(row)
    await storage_access.check(
        db, row["location_id"], expected_revision=row["maintenance_revision"], lock=lock
    )


async def bootstrap(db):
    """Initialize committed registry facts before callers acquire business locks."""
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with factory() as current, current.begin():
        location = await current.get(StorageLocation, LEGACY_LOCATION_ID)
        purposes = set(await current.scalars(select(StorageBinding.purpose)))
        if location is None or not set(PURPOSE_BUCKET_ATTRIBUTES).issubset(purposes):
            await ensure_legacy_bindings(current)


def staging_owner(row):
    """Interpret old null locators only from their saved creation configuration."""
    from app.services import import_storage

    if row["origin"] != "direct":
        reject("managed_original_creation_input_changed")
    prefix = f"{STAGING_PREFIX}/{row['id']}/"
    reference = row.get("staging_ref")
    if reference is None:
        saved = row["configuration"]
        if (
            _digest(saved) != row["input_fingerprint"]
            or saved.get("buckets", {}).get("import") != row["staging_bucket"]
        ):
            reject("managed_original_configuration_changed")
        reference = _import_call(
            import_storage.legacy_ref,
            prefix,
            endpoint_url=saved.get("endpoint"),
            bucket=row["staging_bucket"],
        )
    owner = _import_call(import_storage.resolve_ref, reference, expected_prefix=prefix)
    if (
        owner.bucket != row["staging_bucket"]
        or owner.key("payload") != row["staging_key"]
    ):
        reject("managed_original_creation_input_changed")
    return owner


def _import_error(exc):
    error = ContentError(exc.code, exc.status_code)
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(error)
    return error


def _import_call(call, *args, **kwargs):
    from app.services import import_storage

    try:
        return call(*args, **kwargs)
    except import_storage.ImportStorageError as exc:
        raise _import_error(exc) from None


async def _import_async(call, *args, **kwargs):
    from app.services import import_storage

    try:
        return await call(*args, **kwargs)
    except import_storage.ImportStorageError as exc:
        raise _import_error(exc) from None


async def validate_staging(db, row, *, lock=False, expected_revision=None):
    from app.services import import_storage

    owner = staging_owner(row)
    await _import_async(import_storage.io_target, db, owner, lock=lock)
    return await import_storage.check_location(
        db, owner, expected_revision=expected_revision, lock=lock
    )


def upload_expires_in(allocation):
    deadline = allocation["allocated_at"] + timedelta(seconds=900)
    seconds = int((deadline - now()).total_seconds())
    if seconds <= 0:
        reject("managed_original_staging_expired")
    return min(seconds, 900)


async def prepare_upload_url(db, allocation):
    """Use the durable source owner before callers enter final authority locks."""
    from app.services import import_storage

    try:
        return await _import_async(
            import_storage.prepare_upload_url,
            db,
            staging_owner(allocation),
            "payload",
            content_type=allocation["content_type"],
            expires_in=upload_expires_in(allocation),
        )
    except ContentError:
        raise
    except Exception as exc:
        error = ContentError("managed_original_staging_unavailable", 503)
        if getattr(exc, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(error)
        raise error from None


async def validate_upload_url(db, allocation, url, *, lock=True):
    """Recheck the exact signing credentials and deadline without provider I/O."""
    from app.services import import_storage

    return await _import_async(
        import_storage.validate_upload_url,
        db,
        url,
        owner=staging_owner(allocation),
        relative="payload",
        content_type=allocation["content_type"],
        lock=lock,
    )


async def allocate(
    db,
    *,
    dataset_id,
    logical_path,
    origin,
    max_bytes,
    requested_by=None,
    authority=None,
    staging=False,
    content_type="application/octet-stream",
):
    return await _allocate(
        db,
        owner_kind="dataset",
        dataset_id=dataset_id,
        project_id=None,
        task_id=None,
        logical_path=logical_path,
        origin=origin,
        max_bytes=max_bytes,
        requested_by=requested_by,
        authority=authority,
        staging=staging,
        content_type=content_type,
    )


async def allocate_fixture(
    db,
    *,
    run_id,
    admission_id,
    dataset_id,
    item_id,
    item_fingerprint,
    logical_path,
    max_bytes,
    requested_by=None,
    content_type="application/octet-stream",
):
    """Allocate on the real binding and atomically record fixture ownership."""
    if (
        not all(
            isinstance(value, uuid.UUID)
            for value in (run_id, admission_id, dataset_id, item_id)
        )
        or not isinstance(item_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", item_fingerprint) is None
    ):
        reject("managed_original_invalid_allocation", 422)
    return await _allocate(
        db,
        owner_kind="dataset",
        dataset_id=dataset_id,
        project_id=None,
        task_id=None,
        logical_path=logical_path,
        origin="connector",
        max_bytes=max_bytes,
        requested_by=requested_by,
        authority=None,
        staging=False,
        content_type=content_type,
        fixture=(run_id, admission_id, item_id, item_fingerprint),
    )


async def allocate_fixture_auxiliary(
    db,
    *,
    run_id,
    admission_id,
    dataset_id,
    kind,
    source_contract,
    payload_sha256,
    max_bytes,
    requested_by,
    content_type,
):
    """Reserve a real map/raw-lidar owner intent on the private target."""
    from app.services.auxiliary_originals import contract

    if (
        not all(
            isinstance(value, uuid.UUID) for value in (run_id, admission_id, dataset_id)
        )
        or not isinstance(payload_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None
    ):
        reject("managed_original_invalid_allocation", 422)
    frozen = contract(kind, source_contract)
    return await _allocate(
        db,
        owner_kind="dataset_auxiliary",
        dataset_id=dataset_id,
        project_id=None,
        task_id=None,
        logical_path=f"{AUXILIARY_ORIGINAL_PREFIX}/{kind}",
        origin="connector",
        max_bytes=max_bytes,
        requested_by=requested_by,
        authority={"auxiliary": {"kind": kind, "source_contract": frozen}},
        staging=False,
        content_type=content_type,
        fixture_auxiliary=(run_id, admission_id, kind, frozen, payload_sha256),
    )


async def allocate_auxiliary(
    db,
    *,
    dataset_id,
    kind,
    source_contract,
    logical_path,
    max_bytes,
    authority,
    requested_by=None,
    content_type="application/octet-stream",
):
    from app.services.auxiliary_originals import contract

    if (
        not isinstance(dataset_id, uuid.UUID)
        or not isinstance(authority, dict)
        or not authority
    ):
        reject("managed_original_invalid_allocation", 422)
    frozen = contract(kind, source_contract)
    return await _allocate(
        db,
        owner_kind="dataset_auxiliary",
        dataset_id=dataset_id,
        project_id=None,
        task_id=None,
        logical_path=logical_path,
        origin="connector",
        max_bytes=max_bytes,
        requested_by=requested_by,
        authority={**authority, "auxiliary": {"kind": kind, "source_contract": frozen}},
        staging=False,
        content_type=content_type,
    )


async def allocate_task(
    db,
    *,
    project_id,
    task_id,
    logical_path,
    max_bytes,
    requested_by=None,
    authority=None,
    content_type="application/octet-stream",
):
    """Reserve an independent Task candidate, never a final browser PUT key."""
    if not isinstance(project_id, uuid.UUID) or not isinstance(task_id, uuid.UUID):
        reject("managed_original_invalid_allocation", 422)
    return await _allocate(
        db,
        owner_kind="task",
        dataset_id=None,
        project_id=project_id,
        task_id=task_id,
        logical_path=logical_path,
        origin="direct",
        max_bytes=max_bytes,
        requested_by=requested_by,
        authority=authority,
        staging=True,
        content_type=content_type,
    )


async def _allocate(
    db,
    *,
    owner_kind,
    dataset_id,
    project_id,
    task_id,
    logical_path,
    origin,
    max_bytes,
    requested_by,
    authority,
    staging,
    content_type,
    fixture=None,
    fixture_auxiliary=None,
):
    if (
        origin not in ORIGINS
        or type(max_bytes) is not int
        or not 0 <= max_bytes <= 2**63 - 1
        or staging != (origin == "direct")
    ):
        reject("managed_original_invalid_allocation", 422)
    if (
        not isinstance(content_type, str)
        or not content_type
        or len(content_type) > 255
        or any(c in content_type for c in "\r\n\x00")
    ):
        reject("managed_original_invalid_allocation", 422)
    try:
        logical_path = normalize_dataset_relative_path(logical_path)
    except (AttributeError, TypeError, ValueError):
        reject("managed_original_invalid_allocation", 422)
    fixture_scoped = fixture is not None or fixture_auxiliary is not None
    if not fixture_scoped:
        await bootstrap(db)
    creation_id = uuid.uuid4()
    staging_ref, staging_owner_value = None, None
    if staging:
        from app.services import import_storage

        prefix = f"{STAGING_PREFIX}/{creation_id}/"
        staging_ref = await _import_async(import_storage.allocate, db, prefix)
        staging_owner_value = _import_call(
            import_storage.resolve_ref, staging_ref, expected_prefix=prefix
        )
    object_key = (
        f"{TASK_ORIGINAL_PREFIX}/{task_id}/{creation_id}/payload"
        if owner_kind == "task"
        else (
            f"{AUXILIARY_ORIGINAL_PREFIX}/{dataset_id}/{creation_id}/payload"
            if owner_kind == "dataset_auxiliary"
            else managed_dataset_key(dataset_id, logical_path)
        )
    )
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with factory() as current, current.begin():
        if fixture_scoped:
            from app.db.models.fixture_run import (
                FixtureRunAdmission,
                FixtureRunResource,
            )
            from app.services import fixture_run_ownership as ownership

            if fixture is not None and fixture_auxiliary is not None:
                reject("fixture_original_admission_changed")
            if fixture is not None:
                run_id, admission_id, item_id, item_fingerprint = fixture
            else:
                run_id, admission_id, _kind, _contract, _sha256 = fixture_auxiliary
            _, root = await ownership.lock_original_scope(current, run_id)
            admitted = await current.scalar(
                select(FixtureRunAdmission.id).where(
                    FixtureRunAdmission.id == admission_id,
                    FixtureRunAdmission.run_id == run_id,
                    FixtureRunAdmission.state == "active",
                )
            )
            owned_dataset = await current.scalar(
                select(FixtureRunResource).where(
                    FixtureRunResource.run_id == run_id,
                    FixtureRunResource.kind == "dataset",
                    FixtureRunResource.resource_id == dataset_id,
                    FixtureRunResource.state == "published",
                )
            )
            if admitted is None or owned_dataset is None:
                reject("fixture_original_admission_changed")
            if fixture is not None:
                owned_item = await current.get(
                    FixtureRunResource, (run_id, "item", item_id)
                )
                if owned_item is None or owned_item.state != "reserved":
                    reject("fixture_original_admission_changed")
            binding, location = await ownership.original_target(current, root)
        else:
            purpose = "annotations" if owner_kind == "task" else "datasets"
            initial = await current.get(StorageBinding, purpose, populate_existing=True)
            if initial is None:
                reject("managed_original_configuration_changed")
            facts = initial.location_id, initial.bucket, initial.generation
            initial_location = await current.get(
                StorageLocation, facts[0], populate_existing=True
            )
            if initial_location is None:
                reject("managed_original_configuration_changed")
            from app.db.models.storage_connection import StorageConnection

            connection_id = initial_location.connection_id
            if connection_id is not None:
                await current.execute(
                    select(StorageConnection.id)
                    .where(StorageConnection.id == connection_id)
                    .with_for_update(read=True)
                )
            location = await current.scalar(
                select(StorageLocation)
                .where(StorageLocation.id == facts[0])
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            binding = await current.scalar(
                select(StorageBinding)
                .where(StorageBinding.purpose == purpose)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
            if (
                location is None
                or location.connection_id != connection_id
                or binding is None
                or (binding.location_id, binding.bucket, binding.generation) != facts
            ):
                reject("managed_original_configuration_changed")
        configuration = {} if fixture_scoped else await _configuration(current)
        from app.services.dataset_storage_target import target_for as dataset_target

        storage = annotations_storage if owner_kind == "task" else dataset_storage
        target_for = (
            annotations_storage.target_for if owner_kind == "task" else dataset_target
        )
        try:
            target = await target_for(current, location)
            if (
                binding.bucket != target.bucket
                or type(binding.generation) is not int
                or binding.generation < 1
                or (
                    not fixture_scoped
                    and binding.generation == 1
                    and location.id != LEGACY_LOCATION_ID
                )
            ):
                reject("managed_original_configuration_changed")
            object_key = target.key_prefix + object_key
            if not dataset_storage.valid_key(object_key):
                reject("managed_original_invalid_allocation", 422)
            reference = storage.make_ref(target, binding.generation, object_key)
        except (
            dataset_storage.DatasetStorageError,
            annotations_storage.AnnotationsStorageError,
        ) as exc:
            raise ContentError(exc.code, exc.status_code) from None
        configuration = {
            **configuration,
            "schema": 2,
            "endpoint": target.endpoint_url,
            "original_ref": reference,
        }
        if fixture_scoped:
            configuration["location"] = _json(
                {
                    column.key: getattr(location, column.key)
                    for column in StorageLocation.__table__.columns
                }
            )
            configuration["connection_revision"] = target.connection_revision
            configuration["fixture_original_target"] = {
                "id": str(binding.id),
                "generation": binding.generation,
                "store_receipt_sha256": binding.store_receipt_sha256,
            }
        if (
            not dataset_storage.valid_key(object_key)
            or len(binding.bucket.encode()) > 255
            or (
                staging_owner_value is not None
                and not dataset_storage.valid_key(staging_owner_value.key("payload"))
            )
        ):
            reject("managed_original_invalid_allocation", 422)
        if fixture_scoped:
            target_identity = managed_storage_identity(
                configuration["endpoint"], binding.bucket
            )
            if (
                owned_dataset.storage_identity != target_identity
                or root.original_storage_identity != target_identity
            ):
                reject("fixture_original_storage_changed")
            # Operator append receipts outlive their items. Serialize their
            # admission with this exact key before publishing fixture ownership.
            await current.execute(
                text(
                    "SELECT pg_catalog.pg_advisory_xact_lock("
                    "pg_catalog.hashtextextended('fixture-source:' || :key, 0))"
                ),
                {"key": object_key},
            )
            previous_operator_source = await current.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM public.dataset_operator_operations AS receipt "
                    "CROSS JOIN LATERAL pg_catalog.jsonb_array_elements("
                    "CASE WHEN receipt.action = 'append_items' "
                    "THEN receipt.intent -> 'entries' ELSE '[]'::jsonb END"
                    ") AS entry(value) "
                    "WHERE receipt.action = 'append_items' "
                    "AND entry.value ->> 'file_path' = :key)"
                ),
                {"key": object_key},
            )
            if previous_operator_source:
                reject("fixture_original_prior_operator_source")
        revision = await storage_access.check(current, location.id, lock=True)
        values = {
            "id": creation_id,
            "owner_kind": owner_kind,
            "dataset_id": dataset_id,
            "project_id": project_id,
            "task_id": task_id,
            "location_id": location.id,
            "storage_identity": managed_storage_identity(
                configuration["endpoint"], binding.bucket
            ),
            "bucket": binding.bucket,
            "object_key": object_key,
            "logical_path": logical_path,
            "origin": origin,
            "max_bytes": max_bytes,
            "timeout_seconds": max(60, min(3600, math.ceil(max_bytes / (8 * 1024**2)))),
            "content_type": content_type,
            "requested_by": requested_by,
            "authority": _json(authority or {}),
            "configuration": configuration,
            "input_fingerprint": _digest(configuration),
            "maintenance_revision": revision,
            "staging_ref": staging_ref,
            "staging_bucket": staging_owner_value.bucket if staging else None,
            "staging_key": staging_owner_value.key("payload") if staging else None,
            "status": "allocated",
        }
        row = (
            (
                await current.execute(
                    insert(Creation)
                    .values(**values)
                    .returning(*Creation.__table__.columns)
                )
            )
            .mappings()
            .one()
        )
        if fixture is not None:
            current.add(
                FixtureRunResource(
                    run_id=run_id,
                    kind="creation",
                    resource_id=creation_id,
                    logical_key=creation_id.hex,
                    creation_id=creation_id,
                    admission_id=admission_id,
                    item_id=item_id,
                    item_fingerprint=item_fingerprint,
                    state="published",
                )
            )
            await current.flush()
        elif fixture_auxiliary is not None:
            from app.db.models.fixture_run import FixtureAuxiliaryIntent

            current.add(
                FixtureRunResource(
                    run_id=run_id,
                    kind="auxiliary",
                    resource_id=creation_id,
                    logical_key=creation_id.hex,
                    state="published",
                )
            )
            await current.flush()
            current.add(
                FixtureAuxiliaryIntent(
                    creation_id=creation_id,
                    run_id=run_id,
                    admission_id=admission_id,
                    dataset_id=dataset_id,
                    kind=_kind,
                    source_contract=_contract,
                    sha256=_sha256,
                    size_bytes=max_bytes,
                )
            )
            await current.flush()
        if fixture_auxiliary is not None:
            material = await current.scalar(
                text(
                    "SELECT yoao_assets_v1._managed_original_facts(creation) "
                    "FROM public.managed_original_creations AS creation WHERE creation.id=:id"
                ),
                {"id": creation_id},
            )
            if not isinstance(material, dict) or material.get("creation_id") != str(
                creation_id
            ):
                reject("fixture_original_material_unavailable")
            return material
        return dict(row)


async def for_key(db, *, dataset_id, object_key):
    async with storage_access.engine(db).connect() as connection:
        row = (
            (
                await connection.execute(
                    select(Creation.__table__)
                    .where(
                        Creation.owner_kind == "dataset",
                        Creation.dataset_id == dataset_id,
                        Creation.object_key == object_key,
                    )
                    # A retried publication keeps the same reserved key, so the
                    # verified attempt is the current row; immutable failed
                    # evidence for earlier attempts may also exist.
                    .order_by(
                        (Creation.status == "verified").desc(),
                        Creation.allocated_at.desc(),
                        Creation.id.desc(),
                    )
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        row = dict(row) if row else None
    if row:
        await _validate(db, row)
    return row


async def for_task(db, *, task_id, object_key):
    """Find a Task-scoped allocation without borrowing a Dataset identity."""
    async with storage_access.engine(db).connect() as connection:
        row = (
            (
                await connection.execute(
                    select(Creation.__table__)
                    .where(
                        Creation.owner_kind == "task",
                        Creation.task_id == task_id,
                        Creation.object_key == object_key,
                    )
                    .order_by(
                        (Creation.status == "verified").desc(),
                        Creation.allocated_at.desc(),
                        Creation.id.desc(),
                    )
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        row = dict(row) if row else None
    if row:
        await _validate(db, row)
    return row


def _result(row, head_bytes=b""):
    return CreatedOriginal(
        row["id"],
        row["sha256"],
        row["size_bytes"],
        row["md5"],
        row["etag"],
        row["version_id"],
        head_bytes,
    )


async def _loaded(db, allocation):
    row = await _read(db, allocation["id"])
    if (
        row is None
        or row.get("staging_ref") != allocation.get("staging_ref")
        or any(
            field not in allocation or row[field] != allocation[field]
            for field in (
                "owner_kind",
                "dataset_id",
                "project_id",
                "task_id",
                "location_id",
                "storage_identity",
                "bucket",
                "object_key",
                "logical_path",
                "origin",
                "max_bytes",
                "timeout_seconds",
                "content_type",
                "requested_by",
                "authority",
                "configuration",
                "input_fingerprint",
                "maintenance_revision",
                "staging_bucket",
                "staging_key",
            )
        )
    ):
        reject("managed_original_creation_not_found", 404)
    await _validate(db, row)
    return row


def _proof(proof):
    return {"sha256": proof.sha256, "md5": proof.md5, "size_bytes": proof.size_bytes}


def _error(exc):
    code = getattr(exc, "code", None)
    return (
        code
        if isinstance(code, str) and _SAFE_CODE.fullmatch(code)
        else "managed_original_creation_failed"
    )


async def _drain(task):
    """Keep ownership through repeated cancellation and signal BaseExceptions."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except BaseException:
            continue
    return task.result()


class _Physical:
    def __init__(self):
        self.interrupted = None

    async def run(self, function, *args, **kwargs):
        future = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(future)
        except BaseException as exc:
            if future.done() and (future.cancelled() or future.exception() is not None):
                return future.result()
            # Adopt late client/preparation/receipt results in the caller so its
            # normal persistence and finally-close path still owns every handle.
            if self.interrupted is None:
                self.interrupted = exc
            return await _drain(future)


async def _physical(db, row, stream, *, source_target=None):
    from app.services import managed_original_writer as writer

    client, source_client, source_context, target_context = None, None, None, None
    prepared, completion_started = None, False
    observed_receipt = None
    physical = _Physical()
    failure = None
    mutation = None
    evidence = {"physical_outcome": "unknown"}
    settled = False
    try:
        if row["location_id"] == LEGACY_LOCATION_ID:
            client = await physical.run(
                create_client, timeout_seconds=row["timeout_seconds"]
            )
        else:
            target = await _original_storage(row).target_for_ref(db, original_ref(row))
            target_context = target.client(
                read_timeout=max(10, min(row["timeout_seconds"], 3600))
            )
            client = await physical.run(target_context.__enter__)
        if row["origin"] == "direct":
            if source_target is None:
                reject("managed_original_creation_input_changed")
            source_context = source_target.client()
            source_client = await physical.run(source_context.__enter__)
            source = await physical.run(
                writer.inspect_staging,
                source_client,
                row["staging_bucket"],
                row["staging_key"],
                max_bytes=row["max_bytes"],
                timeout_seconds=row["timeout_seconds"],
            )
            await _change(
                db,
                row["id"],
                "preparing",
                source={
                    **_proof(source),
                    "etag": source.etag,
                    "version_id": source.version_id,
                    "observation": _json(source.observation),
                },
            )
            prepared = await physical.run(
                writer.prepare_copy,
                client,
                row["staging_bucket"],
                row["staging_key"],
                row["bucket"],
                row["object_key"],
                source,
                content_type=row["content_type"],
                timeout_seconds=row["timeout_seconds"],
                source_client=source_client,
            )
        else:
            prepared = await physical.run(
                writer.prepare_stream,
                client,
                row["bucket"],
                row["object_key"],
                stream,
                content_type=row["content_type"],
                max_bytes=row["max_bytes"],
                timeout_seconds=row["timeout_seconds"],
            )
        if prepared.bucket != row["bucket"] or prepared.object_key != row["object_key"]:
            reject("managed_original_preparation_mismatch")
        await _validate(db, row)
        await _change(
            db,
            row["id"],
            "preparing",
            status="prepared",
            frozen_at=now(),
            **_proof(prepared),
            prepared={
                **_proof(prepared),
                "upload_id": prepared.upload_id,
                "parts": _json(prepared.parts),
                "bucket": prepared.bucket,
                "object_key": prepared.object_key,
            },
        )
        _, mutation = await _change(
            db,
            row["id"],
            "prepared",
            record_mutation=True,
            status="writing",
            write_started_at=now(),
        )
        completion_started = True
        receipt = await physical.run(
            writer.complete,
            client,
            row["bucket"],
            row["object_key"],
            prepared,
            content_type=row["content_type"],
        )
        observed_receipt = {
            "etag": receipt.etag,
            "version_id": receipt.version_id,
            "request_id": receipt.request_id,
            "response": _json(receipt.response),
        }
        evidence.update(
            physical_outcome="written",
            physical_size_bytes=prepared.size_bytes,
            returned_version=receipt.response.get("VersionId"),
        )
        try:
            await _change(
                db,
                row["id"],
                "writing",
                status="written",
                acknowledged_at=now(),
                receipt=observed_receipt,
                etag=receipt.etag,
                version_id=receipt.version_id,
                request_id=receipt.request_id,
            )
        except BaseException:
            raise storage_access.mark_unsettled(
                ContentError("managed_original_receipt_persistence_unknown", 503)
            ) from None
        verified = await physical.run(
            writer.verify,
            client,
            row["bucket"],
            row["object_key"],
            receipt,
            sha256=prepared.sha256,
            size_bytes=prepared.size_bytes,
            max_bytes=row["max_bytes"],
            timeout_seconds=row["timeout_seconds"],
        )
        if _proof(verified) != _proof(prepared):
            reject("managed_original_readback_mismatch")
        evidence["full_readback"] = True
        if receipt.version_id not in {None, "", "null"}:
            evidence["current_head_matches"] = await physical.run(
                accounting.current_matches,
                client,
                row["bucket"],
                row["object_key"],
                receipt.version_id,
                prepared.size_bytes,
            )
            if evidence["current_head_matches"]:
                evidence["current_delta_bytes"] = prepared.size_bytes
        try:
            await _change(
                db,
                row["id"],
                "written",
                verification={
                    **_proof(verified),
                    "observation": _json(verified.observation),
                },
                readback_at=now(),
            )
        except BaseException as exc:
            raise storage_access.mark_unsettled(
                ContentError("managed_original_readback_persistence_unknown", 503)
            ) from exc
        result = verified.head_bytes
    except BaseException as exc:
        failure = exc
        if (
            mutation is not None
            and evidence["physical_outcome"] != "written"
            and getattr(exc, "outcome", None) == "conflict"
        ):
            evidence.update(physical_outcome="not_written", current_delta_bytes=0)
        if prepared is not None and not completion_started:
            # A failed freeze/config fence may clean only its exact unfinished
            # MPU. This never deletes or retries the final object.
            try:
                await physical.run(
                    writer.abort_prepared,
                    client,
                    row["bucket"],
                    row["object_key"],
                    prepared,
                )
            except BaseException as abort_failure:
                failure = storage_access.mark_unsettled(
                    BaseExceptionGroup(
                        "Original creation and prepared-upload abort failed",
                        [
                            exc,
                            ContentError("managed_original_abort_unknown", 503),
                            abort_failure,
                        ],
                    )
                )
    finally:
        failures = []
        initial_interruption = physical.interrupted
        if initial_interruption is not None:
            failures.append(initial_interruption)
        if failure is not None and failure is not initial_interruption:
            failures.append(failure)
        close_errors = []
        for resource, context in (
            (source_client, source_context),
            (client, target_context),
        ):
            if resource is None:
                continue
            try:
                if context is not None:
                    await physical.run(context.__exit__, None, None, None)
                else:
                    await physical.run(storage_access.close_registered, resource)
            except BaseException as close_failure:
                close_errors.append(close_failure)
        if close_errors:
            failures.append(ContentError("managed_original_close_unknown", 503))
            failures.extend(close_errors)
        else:
            settled = True
        if physical.interrupted is not None and initial_interruption is None:
            failures.append(physical.interrupted)
        accounting_failed = False
        if mutation is not None:
            # Keep the permit until physical close and accounting both settle.
            publication = asyncio.create_task(
                accounting.publish(
                    async_sessionmaker(
                        storage_access.engine(db), expire_on_commit=False
                    ),
                    mutation,
                    evidence,
                    settled=settled,
                )
            )
            try:
                await asyncio.shield(publication)
            except BaseException as publication_failure:
                failures.append(publication_failure)
                try:
                    await _drain(publication)
                except BaseException as late_failure:
                    accounting_failed = True
                    if late_failure is not publication_failure:
                        failures.append(late_failure)
        if failures:
            if observed_receipt is not None:
                for physical_failure in failures:
                    physical_failure.physical_receipt = observed_receipt
            if len(failures) == 1:
                if close_errors or accounting_failed:
                    storage_access.mark_unsettled(failures[0])
                raise failures[0]
            combined = BaseExceptionGroup(
                "Original creation did not settle cleanly", failures
            )
            if observed_receipt is not None:
                combined.physical_receipt = observed_receipt
            if (
                close_errors
                or accounting_failed
                or getattr(failure, "storage_operation_unsettled", False)
            ):
                storage_access.mark_unsettled(combined)
            raise combined
    return result


async def _execute(db, allocation, stream):
    row = await _loaded(db, allocation)
    if row["status"] == "verified":
        return _result(row)
    if row["status"] != "allocated":
        reject(
            "managed_original_creation_active"
            if row["status"] not in TERMINAL
            else "managed_original_creation_terminal"
        )
    row = await _change(
        db, row["id"], "allocated", status="preparing", claimed_at=now()
    )
    try:
        async with await storage_access.acquire(
            db,
            row["location_id"],
            (
                f"task_original_write_{row['id'].hex}"
                if row["owner_kind"] == "task"
                else "managed_original_creation"
            ),
            expected_revision=row["maintenance_revision"],
        ) as permit:
            await _change(db, row["id"], "preparing", operation_id=permit.id)
            if row["origin"] == "direct":
                from app.services import import_storage

                owner = staging_owner(row)
                target = await _import_async(import_storage.io_target, db, owner)
                async with await storage_access.acquire(
                    db,
                    owner.location_id,
                    "managed_original_source_" + row["id"].hex,
                ) as source_permit:
                    await validate_staging(
                        db, row, expected_revision=source_permit.revision
                    )
                    head_bytes = await _physical(db, row, stream, source_target=target)
                    await validate_staging(
                        db, row, expected_revision=source_permit.revision
                    )
            else:
                head_bytes = await _physical(db, row, stream)
        await _validate(db, row)
        row = await _change(
            db,
            row["id"],
            "written",
            status="verified",
            settled_at=now(),
            completed_at=now(),
        )
        return _result(row, head_bytes)
    except BaseException as exc:
        try:
            current = await _read(db, row["id"])
            operation = None
            if current and current["operation_id"]:
                async with storage_access.engine(db).connect() as connection:
                    operation = await connection.scalar(
                        select(StorageOperation.id).where(
                            StorageOperation.id == current["operation_id"]
                        )
                    )
            if current and current["status"] not in TERMINAL:
                outcome = (
                    "unknown"
                    if operation or getattr(exc, "storage_operation_unsettled", False)
                    else (
                        "conflict"
                        if getattr(exc, "outcome", None) == "conflict"
                        else "failed"
                    )
                )
                settled = not (
                    operation or getattr(exc, "storage_operation_unsettled", False)
                )
                observed_receipt = current["receipt"] or getattr(
                    exc, "physical_receipt", None
                )
                evidence = {
                    "physical_settled": settled,
                    "physical_outcome": (
                        "unknown"
                        if not settled
                        else "written"
                        if observed_receipt is not None
                        else "not_written"
                    ),
                }
                if observed_receipt is not None:
                    evidence["receipt"] = observed_receipt
                await _change(
                    db,
                    row["id"],
                    current["status"],
                    status=outcome,
                    error_code=_error(exc),
                    failure_evidence=evidence,
                    settled_at=now() if settled else None,
                    completed_at=now(),
                )
            status = (
                503
                if operation or getattr(exc, "storage_operation_unsettled", False)
                else (
                    413
                    if _error(exc) == "creation_byte_budget_exceeded"
                    else getattr(exc, "status", 409)
                )
            )
        except BaseException as persistence_failure:
            raise storage_access.mark_unsettled(
                BaseExceptionGroup(
                    "Original creation and receipt persistence failed",
                    [
                        exc,
                        ContentError("managed_original_persistence_unknown", 503),
                        persistence_failure,
                    ],
                )
            ) from None
        if isinstance(exc, BaseExceptionGroup):
            raise
        error = ContentError(_error(exc), status)
        if operation or getattr(exc, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(error)
        raise error from exc


async def _shielded(db, allocation, stream):
    task = asyncio.create_task(_execute(db, allocation, stream))
    try:
        return await asyncio.shield(task)
    except BaseException as first_failure:
        # Do not let request cancellation abandon physical work or hide a
        # later write, close, or receipt-persistence failure.
        try:
            await _drain(task)
        except BaseException as late_failure:
            if late_failure is not first_failure:
                combined = BaseExceptionGroup(
                    "Original creation was interrupted and later failed",
                    [first_failure, late_failure],
                )
                if getattr(late_failure, "storage_operation_unsettled", False):
                    storage_access.mark_unsettled(combined)
                raise combined from None
        raise


async def create_stream(db, allocation, stream, *, content_type):
    if allocation["origin"] == "direct" or allocation["content_type"] != content_type:
        reject("managed_original_creation_input_changed")
    return await _shielded(db, allocation, stream)


async def complete_direct(db, allocation):
    if allocation["origin"] != "direct":
        reject("managed_original_creation_input_changed")
    return await _shielded(db, allocation, None)


async def image_dimensions(db, allocation, created):
    """Read a just-verified candidate before the caller takes publication locks."""
    dims = storage_service.read_image_dimensions_from_bytes(created.head_bytes)
    if dims is not None:
        return dims
    from functools import partial
    from app.services import managed_original_reader as reader

    owner = original_locator(allocation)
    options = {}
    if owner.location_id != LEGACY_LOCATION_ID:
        target = await _original_storage(allocation).target_for_ref(db, owner.ref)
        options["client_context"] = partial(target.client, read_timeout=60)
    async with await storage_access.acquire(
        db,
        owner.location_id,
        "dataset_original_dimensions",
        expected_revision=allocation["maintenance_revision"],
    ):
        # The durable permit precedes the exact-key reclaim fence. A claim
        # installed first rejects this read; otherwise the permit blocks GC.
        from app.services.managed_original_reclaim_fence import shared

        async with async_sessionmaker(storage_access.engine(db))() as fence:
            await shared(fence, owner.bucket, owner.object_key)
        stage = await reader.capture(
            reader.ObjectSpec(
                owner.endpoint_url,
                owner.bucket,
                owner.object_key,
                created.sha256,
                created.size_bytes,
            ),
            max_bytes=created.size_bytes,
            timeout_seconds=allocation["timeout_seconds"],
            **options,
        )
        try:
            try:
                from PIL import Image

                with Image.open(stage.file) as image:
                    return storage_service._logical_image_dimensions(image)
            except Exception:
                return None
        finally:
            stage.close()


async def attach(db, item, creation_id):
    row = await _read(db, creation_id)
    if row is None or row["owner_kind"] != "dataset" or row["status"] != "verified":
        reject("managed_original_creation_unverified")
    await _validate(db, row, lock=True)
    asset = await db.scalar(
        select(DatasetAsset)
        .where(DatasetAsset.dataset_item_id == item.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    replica = (
        await db.get(
            DatasetAssetLocation,
            (asset.id, row["location_id"], row["bucket"]),
            populate_existing=True,
        )
        if asset
        else None
    )
    if (
        item.dataset_id != row["dataset_id"]
        or item.file_path != row["object_key"]
        or asset is None
        or asset.verification_state != "verified"
        or asset.checksum_algorithm != "sha256"
        or asset.checksum_value != row["sha256"]
        or asset.size_bytes != row["size_bytes"]
        or replica is None
        or replica.object_key != row["object_key"]
        or replica.role != "managed_original"
        or replica.creation_id not in (None, creation_id)
        or replica.storage_ref != original_ref(row)
    ):
        reject("managed_original_creation_attachment_mismatch")
    from app.services.managed_original_reclaim_fence import shared

    await shared(db, row["bucket"], row["object_key"])
    replica.creation_id = creation_id
    await db.flush()
