"""Verify the private owner and exact run resources for screenshot helpers."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import uuid

from sqlalchemy import select


async def screenshot_owner():
    raw = os.environ.get("SCREENSHOT_FIXTURE_RUN_ID")
    if not raw:
        raise RuntimeError("SCREENSHOT_FIXTURE_RUN_ID is required")
    run_id = uuid.UUID(raw)
    if str(run_id) != raw:
        raise RuntimeError("screenshot run ID must be a canonical UUID")
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    from fixture_suite_coordinator import verified_fixture_owner_url

    return run_id, await verified_fixture_owner_url()


async def close_screenshot_owner(engine, failure: BaseException | None) -> None:
    from app.services.storage_access import finish

    try:
        await finish(asyncio.create_task(engine.dispose()))
    except BaseException as disposal_failure:
        if failure is not None:
            raise BaseExceptionGroup(
                "Screenshot operation and owner disposal failed",
                [failure, disposal_failure],
            ) from None
        raise


async def assert_screenshot_scope(db, *, run_id, project_key, project_id, task_id):
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
    from app.db.models.fixture_run import FixtureRun, FixtureRunResource
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.storage_maintenance import StorageOperation
    from app.services.fixture_run_ownership import _require_settled_original_receipt
    from app.services.labeling_core_read_relations import get_core_row, task_row
    from app.services.screenshot_seed_spec import (
        PROJECT_SPECS,
        SEED_MANAGED_BY,
        resolve_project_spec,
    )

    run = await db.scalar(
        select(FixtureRun)
        .where(FixtureRun.id == run_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        run is None
        or run.family != "screenshot"
        or run.parent_run_id is not None
        or run.state != "open"
    ):
        raise RuntimeError("screenshot run is not open")
    project = await db.get(FixtureRunResource, (run_id, "project", project_id))
    if (
        project is None
        or project.state != "published"
        or project.logical_key != project_key
    ):
        raise RuntimeError("project is outside the screenshot run")
    task = await get_core_row(db, task_row, task_id)
    if task is None or task.project_id != project_id or task.dataset_item_id is None:
        raise RuntimeError("task is outside the screenshot project")
    item_owner = await db.get(
        FixtureRunResource, (run_id, "item", task.dataset_item_id)
    )
    item = await db.get(DatasetItem, task.dataset_item_id)
    if (
        item_owner is None
        or item_owner.state != "published"
        or item is None
        or task.file_path != item.file_path
    ):
        raise RuntimeError("task original is outside the screenshot run")
    source = (
        await db.execute(
            select(ManagedOriginalCreation, StorageOperation)
            .join(
                FixtureRunResource,
                FixtureRunResource.creation_id == ManagedOriginalCreation.id,
            )
            .join(
                DatasetAsset,
                DatasetAsset.dataset_item_id == item.id,
            )
            .join(
                DatasetAssetLocation,
                (DatasetAssetLocation.asset_id == DatasetAsset.id)
                & (DatasetAssetLocation.creation_id == ManagedOriginalCreation.id),
            )
            .outerjoin(
                StorageOperation,
                StorageOperation.id == ManagedOriginalCreation.operation_id,
            )
            .where(
                FixtureRunResource.run_id == run_id,
                FixtureRunResource.kind == "creation",
                FixtureRunResource.item_id == item.id,
                FixtureRunResource.state == "published",
                DatasetAsset.verification_state == "verified",
                DatasetAsset.checksum_value == ManagedOriginalCreation.sha256,
                DatasetAsset.size_bytes == ManagedOriginalCreation.size_bytes,
                DatasetAssetLocation.location_id == ManagedOriginalCreation.location_id,
                DatasetAssetLocation.bucket == ManagedOriginalCreation.bucket,
                DatasetAssetLocation.object_key == ManagedOriginalCreation.object_key,
                DatasetAssetLocation.etag == ManagedOriginalCreation.etag,
                DatasetAssetLocation.reference_state == "present",
            )
        )
    ).one_or_none()
    if source is None:
        raise RuntimeError("task has no verified screenshot original")
    _require_settled_original_receipt(*source)
    dataset_owner = await db.get(
        FixtureRunResource, (run_id, "dataset", item.dataset_id)
    )
    dataset = await db.get(Dataset, item.dataset_id)
    link = await db.scalar(
        select(ProjectDataset.id).where(
            ProjectDataset.project_id == project_id,
            ProjectDataset.dataset_id == item.dataset_id,
        )
    )
    if (
        dataset_owner is None
        or dataset_owner.state != "published"
        or dataset_owner.logical_key != project_key
        or dataset is None
        or link is None
    ):
        raise RuntimeError("dataset is outside the screenshot project")
    marker = (dataset.metadata_ or {}).get("seed")
    if (
        not isinstance(marker, dict)
        or marker.get("managed_by") != SEED_MANAGED_BY
        or marker.get("logical_key") != project_key
        or marker.get("run_id") != str(run_id)
    ):
        raise RuntimeError("screenshot dataset marker changed")
    spec = resolve_project_spec(
        PROJECT_SPECS[project_key], dataset.id, dataset.metadata_
    )
    relative = (item.metadata_ or {}).get("storage_relative_path")
    matches = [
        candidate
        for candidate in spec.tasks
        if candidate.file_path.removeprefix(spec.storage_prefix) == relative
    ]
    if len(matches) != 1:
        raise RuntimeError("task is not declared by the screenshot run")
    return matches[0].status
