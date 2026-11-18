"""Record registered managed locators in the caller's business transaction.

This owner neither discovers unregistered objects nor authorizes or performs
physical deletion. Configuration agreement is not exclusive ownership proof.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.managed_original_retirement import ManagedOriginalRetirement
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.reference_keys import PREFIX
from app.services.storage import storage_service


def managed_storage_identity(endpoint_url, bucket):
    """Match the existing full-cache physical identity recipe."""
    return hashlib.sha256(
        json.dumps([endpoint_url.rstrip("/"), bucket]).encode()
    ).hexdigest()


def _identity_evidence(row):
    """Only initial platform binding/configuration can be compared to runtime."""
    if row["location_kind"] is None:
        error = "location_missing"
    elif row["location_id"] != LEGACY_LOCATION_ID:
        error = "platform_location_unproven"
    elif (
        row["location_kind"] != "s3"
        or row["location_config_revision"] != 1
        or row["location_config"]
        != {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
    ):
        error = "platform_configuration_unproven"
    elif row["binding_generation"] != 1:
        error = "platform_binding_unproven"
    else:
        endpoint = getattr(
            getattr(storage_service.client, "meta", None), "endpoint_url", None
        )
        scheme = "https" if settings.minio_use_ssl else "http"
        expected = f"{scheme}://{settings.minio_endpoint}"
        if (
            not isinstance(endpoint, str)
            or endpoint.rstrip("/") != expected.rstrip("/")
            or row["bucket"] != storage_service.datasets_bucket
        ):
            error = "runtime_configuration_unproven"
        else:
            return {
                "identity_state": "verified_binding",
                "storage_identity": managed_storage_identity(endpoint, row["bucket"]),
                "identity_error_code": None,
            }
    return {
        "identity_state": "unknown",
        "storage_identity": None,
        "identity_error_code": error,
    }


def _assertion(row, evidence=None):
    fields = (
        "dataset_id",
        "dataset_item_id",
        "asset_id",
        "creation_id",
        "location_id",
        "bucket",
        "object_key",
        "role",
        "location_config_revision",
        "binding_generation",
        "asset_verification_state",
        "size_bytes",
        "reference_state",
        "etag",
        "verified_at",
        "dataset_lifecycle_revision",
    )
    values = {field: row[field] for field in fields}
    values["sha256"] = (
        row["checksum_value"]
        if row["asset_verification_state"] == "verified"
        and row["checksum_algorithm"] == "sha256"
        else None
    )
    values.update(_identity_evidence(row) if evidence is None else evidence)
    provenance = [
        1,
        str(row["asset_id"]),
        str(row["location_id"]),
        row["bucket"],
        row["object_key"],
        row["location_config_revision"],
        row["binding_generation"],
    ]
    values["assertion_digest"] = hashlib.sha256(
        json.dumps(provenance, separators=(",", ":")).encode()
    ).hexdigest()
    values["id"] = uuid.uuid4()
    return values


async def _saved_assertion(db, row):
    ref = row["storage_ref"]
    if ref is None:
        return _assertion(row)
    from app.services import dataset_storage

    row = dict(row)
    evidence = {
        "identity_state": "unknown",
        "storage_identity": None,
        "identity_error_code": "saved_location_unproven",
    }
    try:
        saved = dataset_storage.resolve_ref(ref)
        if (
            saved.location_id != row["location_id"]
            or saved.bucket != row["bucket"]
            or saved.object_key != row["object_key"]
        ):
            raise ValueError
        await dataset_storage.target_for_ref(db, ref, require_enabled=False)
        row["location_config_revision"] = saved.config_revision
        row["binding_generation"] = saved.binding_generation
        evidence = {
            "identity_state": "verified_binding",
            "storage_identity": saved.storage_identity,
            "identity_error_code": None,
        }
    except ValueError:
        # Failed identity evidence must not prevent legitimate logical deletion.
        pass
    return _assertion(row, evidence)


async def capture_managed_original_retirements(
    db, *, dataset_id, item_id=None, fixture_run_id=None
):
    """Append idempotent assertions before cascades, without committing or I/O.

    The DatasetService caller owns its dataset/item write locks. Unknown
    historical identity remains an assertion, never a reason to deny deletion.
    A real database failure propagates so the business transaction also fails.
    """
    if fixture_run_id is not None:
        from app.services.test_seed_access import assert_isolated_test_seed_database

        await assert_isolated_test_seed_database(db)
    statement = (
        select(
            Dataset.id.label("dataset_id"),
            Dataset.lifecycle_revision.label("dataset_lifecycle_revision"),
            DatasetItem.id.label("dataset_item_id"),
            DatasetAsset.id.label("asset_id"),
            DatasetAsset.verification_state.label("asset_verification_state"),
            DatasetAsset.checksum_algorithm,
            DatasetAsset.checksum_value,
            DatasetAsset.size_bytes,
            DatasetAssetLocation.location_id,
            DatasetAssetLocation.bucket,
            DatasetAssetLocation.object_key,
            DatasetAssetLocation.storage_ref,
            DatasetAssetLocation.creation_id,
            DatasetAssetLocation.role,
            DatasetAssetLocation.reference_state,
            DatasetAssetLocation.etag,
            DatasetAssetLocation.verified_at,
            StorageLocation.kind.label("location_kind"),
            StorageLocation.config.label("location_config"),
            StorageLocation.config_revision.label("location_config_revision"),
            StorageBinding.generation.label("binding_generation"),
        )
        .select_from(Dataset)
        .join(DatasetItem, DatasetItem.dataset_id == Dataset.id)
        .join(DatasetAsset, DatasetAsset.dataset_item_id == DatasetItem.id)
        .join(DatasetAssetLocation, DatasetAssetLocation.asset_id == DatasetAsset.id)
        .outerjoin(
            StorageLocation, StorageLocation.id == DatasetAssetLocation.location_id
        )
        .outerjoin(
            StorageBinding,
            (StorageBinding.purpose == "datasets")
            & (StorageBinding.location_id == DatasetAssetLocation.location_id)
            & (StorageBinding.bucket == DatasetAssetLocation.bucket),
        )
        .where(
            Dataset.id == dataset_id,
            DatasetAssetLocation.role == "managed_original",
            ~DatasetItem.file_path.startswith(PREFIX, autoescape=True),
        )
    )
    if item_id is not None:
        statement = statement.where(DatasetItem.id == item_id)
    result = await db.stream(statement)
    try:
        async for rows in result.mappings().partitions(500):
            from app.services.managed_original_reclaim_fence import shared

            for bucket, key in sorted(
                {(row["bucket"], row["object_key"]) for row in rows}
            ):
                await shared(db, bucket, key)
            assertions = [await _saved_assertion(db, row) for row in rows]
            if fixture_run_id is not None:
                from app.db.models.fixture_run import FixtureRunResource
                from app.services.content_snapshot import ContentError

                for assertion in assertions:
                    creation_id = assertion["creation_id"]
                    owner = (
                        (
                            await db.execute(
                                select(
                                    FixtureRunResource.run_id,
                                    FixtureRunResource.item_id,
                                ).where(
                                    FixtureRunResource.kind == "creation",
                                    FixtureRunResource.creation_id == creation_id,
                                )
                            )
                        ).one_or_none()
                        if creation_id is not None
                        else None
                    )
                    classification = (
                        await db.scalar(
                            text(
                                "SELECT yoao_assets_v1.classify_fixture_original_reclaim("
                                "CAST(:creation AS uuid), CAST(:identity AS text))"
                            ),
                            {
                                "creation": creation_id,
                                "identity": assertion["storage_identity"],
                            },
                        )
                        if creation_id is not None
                        else None
                    )
                    if (
                        owner is None
                        or tuple(owner)
                        != (fixture_run_id, assertion["dataset_item_id"])
                        or classification != "qualified"
                    ):
                        raise ContentError("fixture_original_retirement_unqualified")
                    # This is the initial policy for a fresh fixture Creation,
                    # never a change to an existing historical assertion.
                    assertion["retain_until"] = datetime.now(timezone.utc)
            await db.execute(
                insert(ManagedOriginalRetirement)
                .values(assertions)
                .on_conflict_do_nothing(index_elements=["assertion_digest"])
            )
    finally:
        await result.close()


async def capture_fixture_auxiliary_retirements(db, *, dataset_id, fixture_run_id):
    """Record exact verified auxiliary owners before the fixture Dataset is removed."""
    from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
    from app.db.models.fixture_run import FixtureAuxiliaryIntent
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.services import dataset_storage
    from app.services.content_snapshot import ContentError
    from app.services.fixture_run_ownership import _require_settled_original_receipt
    from app.db.models.storage_maintenance import StorageOperation
    from app.services.managed_original_reclaim_fence import shared

    dataset = await db.get(Dataset, dataset_id, with_for_update=True)
    if dataset is None:
        raise ContentError("fixture_auxiliary_dataset_missing")
    unowned = await db.scalar(
        select(DatasetAuxiliaryOriginal.id)
        .where(
            DatasetAuxiliaryOriginal.dataset_id == dataset_id,
            ~select(FixtureAuxiliaryIntent.creation_id)
            .where(
                FixtureAuxiliaryIntent.creation_id == DatasetAuxiliaryOriginal.id,
                FixtureAuxiliaryIntent.run_id == fixture_run_id,
                FixtureAuxiliaryIntent.dataset_id == dataset_id,
            )
            .exists(),
        )
        .limit(1)
    )
    if unowned is not None:
        raise ContentError("fixture_auxiliary_owner_untracked")
    rows = (
        await db.execute(
            select(
                DatasetAuxiliaryOriginal,
                FixtureAuxiliaryIntent,
                ManagedOriginalCreation,
            )
            .join(
                FixtureAuxiliaryIntent,
                FixtureAuxiliaryIntent.creation_id == DatasetAuxiliaryOriginal.id,
            )
            .join(
                ManagedOriginalCreation,
                ManagedOriginalCreation.id == DatasetAuxiliaryOriginal.creation_id,
            )
            .where(
                DatasetAuxiliaryOriginal.dataset_id == dataset_id,
                FixtureAuxiliaryIntent.run_id == fixture_run_id,
            )
            .order_by(DatasetAuxiliaryOriginal.id)
            .with_for_update()
        )
    ).all()
    for owner, intent, creation in rows:
        operation = (
            await db.get(StorageOperation, creation.operation_id)
            if creation.operation_id is not None
            else None
        )
        _require_settled_original_receipt(creation, operation)
        if (
            owner.id != creation.id
            or owner.creation_id != creation.id
            or owner.dataset_id != intent.dataset_id
            or creation.dataset_id != dataset_id
            or owner.kind != intent.kind
            or owner.source_contract != intent.source_contract
            or owner.sha256 != intent.sha256
            or owner.sha256 != creation.sha256
            or owner.size_bytes != intent.size_bytes
            or owner.size_bytes != creation.size_bytes
            or owner.location_id != creation.location_id
            or owner.storage_identity != creation.storage_identity
            or owner.bucket != creation.bucket
            or owner.object_key != creation.object_key
            or owner.etag != creation.etag
            or owner.version_id != creation.version_id
            or owner.storage_ref != (creation.configuration or {}).get("original_ref")
        ):
            raise ContentError("fixture_auxiliary_owner_changed")
        await shared(db, owner.bucket, owner.object_key)
        classification = await db.scalar(
            text(
                "SELECT yoao_assets_v1.classify_fixture_original_reclaim("
                "CAST(:creation AS uuid), CAST(:identity AS text))"
            ),
            {"creation": creation.id, "identity": owner.storage_identity},
        )
        if classification != "qualified":
            raise ContentError("fixture_auxiliary_retirement_unqualified")
        try:
            saved = dataset_storage.resolve_ref(owner.storage_ref)
            if (
                saved.location_id != owner.location_id
                or saved.bucket != owner.bucket
                or saved.object_key != owner.object_key
                or saved.storage_identity != owner.storage_identity
            ):
                raise ValueError
            await dataset_storage.target_for_ref(
                db, owner.storage_ref, require_enabled=False
            )
        except ValueError as exc:
            raise ContentError("fixture_auxiliary_location_changed") from exc
        provenance = [
            "dataset_auxiliary",
            1,
            str(owner.id),
            str(owner.location_id),
            owner.bucket,
            owner.object_key,
            saved.config_revision,
            saved.binding_generation,
        ]
        await db.execute(
            insert(ManagedOriginalRetirement)
            .values(
                id=uuid.uuid4(),
                assertion_digest=hashlib.sha256(
                    json.dumps(provenance, separators=(",", ":")).encode()
                ).hexdigest(),
                owner_type="dataset_auxiliary",
                auxiliary_original_id=owner.id,
                dataset_id=dataset_id,
                dataset_item_id=None,
                asset_id=None,
                creation_id=creation.id,
                location_id=owner.location_id,
                bucket=owner.bucket,
                object_key=owner.object_key,
                role="managed_original",
                location_config_revision=saved.config_revision,
                binding_generation=saved.binding_generation,
                storage_identity=owner.storage_identity,
                identity_state="verified_binding",
                asset_verification_state="verified",
                sha256=owner.sha256,
                size_bytes=owner.size_bytes,
                reference_state="present",
                etag=owner.etag,
                verified_at=creation.readback_at,
                dataset_lifecycle_revision=dataset.lifecycle_revision,
                retain_until=datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing(index_elements=["assertion_digest"])
        )
    return tuple(owner.id for owner, _, _ in rows)
