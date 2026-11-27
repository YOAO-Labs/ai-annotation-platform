"""Lock the fixture scope and verify its original storage receipts."""

import re
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.fixture_run import FixtureOriginalTarget, FixtureRun
from app.services.test_seed_access import assert_isolated_test_seed_database


def _reject(code: str) -> None:
    raise HTTPException(status_code=409, detail={"code": code})


async def lock_original_scope(
    db, run_id: uuid.UUID, *, sealed: bool = False
) -> tuple[FixtureRun, FixtureRun]:
    """Lock root then child and require the coordinator's original-store anchor."""
    await assert_isolated_test_seed_database(db)
    observed = await db.get(FixtureRun, run_id, populate_existing=True)
    if observed is None:
        _reject("fixture_run_not_found")
    root_id = observed.parent_run_id or observed.id
    root = await db.scalar(
        select(FixtureRun)
        .where(FixtureRun.id == root_id)
        .with_for_update(read=root_id != run_id)
        .execution_options(populate_existing=True)
    )
    if root is None or root.parent_run_id is not None:
        _reject("fixture_original_store_unqualified")
    run = (
        root
        if root_id == run_id
        else await db.scalar(
            select(FixtureRun)
            .where(FixtureRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if (
        run is None
        or (run.parent_run_id or run.id) != root.id
        or root.state not in ({"open", "sealing"} if sealed else {"open"})
        or run.state != ("sealing" if sealed else "open")
        or root.original_target_id is None
        or root.original_storage_identity is None
        or root.original_store_receipt_sha256 is None
    ):
        _reject("fixture_original_store_unqualified")
    return run, root


async def original_target(db, root: FixtureRun, *, lock_location: bool = False):
    """Lock and verify the real private binding without changing product defaults."""
    from app.db.models.dataset_asset import StorageBinding, StorageLocation
    from app.db.models.storage_connection import StorageConnection
    from app.services import dataset_storage_target
    from app.services.storage_identity import (
        LEGACY_LOCATION_ID,
        managed_storage_identity,
    )

    initial = await db.get(
        FixtureOriginalTarget, root.original_target_id, populate_existing=True
    )
    if initial is None or initial.location_id == LEGACY_LOCATION_ID:
        _reject("fixture_original_storage_unavailable")
    facts = (
        initial.id,
        initial.location_id,
        initial.bucket,
        initial.generation,
        initial.storage_identity,
        initial.store_receipt_sha256,
    )
    initial_location = await db.get(StorageLocation, facts[1], populate_existing=True)
    if initial_location is None or initial_location.connection_id is None:
        _reject("fixture_original_storage_unavailable")
    connection_id = initial_location.connection_id
    await db.execute(
        select(StorageConnection.id)
        .where(StorageConnection.id == connection_id)
        .with_for_update(read=True)
    )
    location = await db.scalar(
        select(StorageLocation)
        .where(StorageLocation.id == facts[1])
        .with_for_update(read=not lock_location)
        .execution_options(populate_existing=True)
    )
    binding = await db.scalar(
        select(FixtureOriginalTarget)
        .where(FixtureOriginalTarget.id == facts[0])
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        location is None
        or location.connection_id != connection_id
        or binding is None
        or (
            binding.id,
            binding.location_id,
            binding.bucket,
            binding.generation,
            binding.storage_identity,
            binding.store_receipt_sha256,
        )
        != facts
        or root.original_storage_identity != binding.storage_identity
        or root.original_store_receipt_sha256 != binding.store_receipt_sha256
    ):
        _reject("fixture_original_storage_changed")
    target = await dataset_storage_target.target_for(db, location)
    if (
        target.bucket != binding.bucket
        or managed_storage_identity(target.endpoint_url, target.bucket)
        != binding.storage_identity
    ):
        _reject("fixture_original_storage_changed")
    product = await db.get(StorageBinding, "datasets", populate_existing=True)
    if product is not None:
        if product.location_id == binding.location_id:
            _reject("fixture_original_storage_shared_with_product")
        product_location = await db.get(StorageLocation, product.location_id)
        if product_location is None:
            _reject("fixture_product_storage_unavailable")
        product_target = await dataset_storage_target.target_for(db, product_location)
        if (
            product.bucket == binding.bucket
            and managed_storage_identity(product_target.endpoint_url, product.bucket)
            == binding.storage_identity
        ):
            _reject("fixture_original_storage_shared_with_product")
    return binding, location


def _require_settled_original_receipt(creation, operation) -> None:
    receipt = creation.receipt if isinstance(creation.receipt, dict) else {}
    verification = (
        creation.verification if isinstance(creation.verification, dict) else {}
    )
    response = (
        receipt.get("response") if isinstance(receipt.get("response"), dict) else {}
    )
    metadata = response.get("ResponseMetadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if (
        creation.status != "verified"
        or creation.settled_at is None
        or creation.readback_at is None
        or operation is not None
        or not creation.version_id
        or creation.version_id == "null"
        or not isinstance(creation.sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", creation.sha256) is None
        or type(creation.size_bytes) is not int
        or creation.size_bytes < 0
        or not isinstance(creation.etag, str)
        or not creation.etag
        or metadata.get("HTTPStatusCode") != 200
        or receipt.get("version_id") != creation.version_id
        or response.get("VersionId") != creation.version_id
        or verification.get("sha256") != creation.sha256
        or verification.get("size_bytes") != creation.size_bytes
    ):
        _reject("fixture_original_unsettled")
