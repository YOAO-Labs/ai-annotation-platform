"""Record registered managed locators in the caller's business transaction.

This owner neither discovers unregistered objects nor authorizes or performs
physical deletion. Configuration agreement is not exclusive ownership proof.
"""

import hashlib
import json
import uuid

from sqlalchemy import select
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


def _assertion(row):
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
    values.update(_identity_evidence(row))
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


async def capture_managed_original_retirements(db, *, dataset_id, item_id=None):
    """Append idempotent assertions before cascades, without committing or I/O.

    The DatasetService caller owns its dataset/item write locks. Unknown
    historical identity remains an assertion, never a reason to deny deletion.
    A real database failure propagates so the business transaction also fails.
    """
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
            await db.execute(
                insert(ManagedOriginalRetirement)
                .values([_assertion(row) for row in rows])
                .on_conflict_do_nothing(index_elements=["assertion_digest"])
            )
    finally:
        await result.close()
