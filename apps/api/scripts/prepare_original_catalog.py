"""Prepare the exact legacy original target using explicit owner credentials."""

import asyncio
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.services.dataset_assets import LEGACY_LOCATION_ID, validate_legacy_bindings
from app.services.managed_original_creation import bootstrap
from app.services.storage import storage_service


async def main() -> None:
    required = (
        "DATABASE_URL",
        "MINIO_ENDPOINT",
        "MINIO_USE_SSL",
        "MINIO_DATASETS_BUCKET",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Explicit owner settings required: " + ", ".join(missing))
    engine = create_async_engine(os.environ["DATABASE_URL"], hide_parameters=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await bootstrap(db)
            async with db.begin():
                # Keep the existing Location -> Binding lock order through
                # provisioning so a concurrent cutover cannot redirect it.
                location = await db.scalar(
                    select(StorageLocation)
                    .where(StorageLocation.id == LEGACY_LOCATION_ID)
                    .with_for_update(read=True)
                )
                binding = await db.scalar(
                    select(StorageBinding)
                    .where(StorageBinding.purpose == "datasets")
                    .with_for_update(read=True)
                )
                if (
                    await validate_legacy_bindings(db)
                    or location is None
                    or not location.enabled
                    or binding is None
                    or binding.location_id != LEGACY_LOCATION_ID
                    or binding.generation != 1
                    or binding.bucket != settings.minio_datasets_bucket
                ):
                    raise RuntimeError(
                        "Configured managed-original target differs from its owner catalog"
                    )
                bucket = settings.minio_datasets_bucket
                storage_service.ensure_bucket(bucket)
                state = storage_service.client.get_bucket_versioning(Bucket=bucket)
                if state.get("Status") != "Enabled":
                    storage_service.client.put_bucket_versioning(
                        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
                    )
                    state = storage_service.client.get_bucket_versioning(Bucket=bucket)
                if state.get("Status") != "Enabled":
                    raise RuntimeError(
                        "Managed-original bucket versioning was not enabled"
                    )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
