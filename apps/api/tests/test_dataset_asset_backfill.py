from __future__ import annotations

import importlib.util
from pathlib import Path
import uuid

import pytest
from sqlalchemy import func, select

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation

spec = importlib.util.spec_from_file_location(
    "asset_backfill", Path(__file__).parents[1] / "scripts/backfill_dataset_assets.py"
)
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)


@pytest.mark.asyncio
async def test_backfill_bounded_resume_idempotency_and_no_business_writes(
    db_session, super_admin
):
    user, _ = super_admin
    dataset = Dataset(
        name="renamed-again", display_id="DS-BACKFILL", created_by=user.id, file_count=3
    )
    db_session.add(dataset)
    await db_session.flush()
    items = [
        DatasetItem(
            id=uuid.UUID(int=i),
            dataset_id=dataset.id,
            file_name=f"{i}.bin",
            file_path=f"actual-old-root/{i}.bin",
            file_type="other",
            content_hash="1" * 32,
        )
        for i in range(1, 4)
    ]
    db_session.add_all(items)
    await db_session.flush()
    before = [
        (item.id, item.file_path, item.updated_at, item.content_hash) for item in items
    ]
    report, cursor = await backfill.backfill_batch(
        db_session, mode="dry-run", dataset_id=dataset.id, batch_size=2
    )
    assert report["pending"] == 2
    assert await db_session.scalar(select(func.count()).select_from(DatasetAsset)) == 0
    report, cursor = await backfill.backfill_batch(
        db_session, mode="apply", dataset_id=dataset.id, batch_size=2
    )
    assert report["created"] == 2 and cursor == items[1].id
    # Resume after a completed batch, and rescan from the beginning afterward.
    report, _ = await backfill.backfill_batch(
        db_session, mode="apply", dataset_id=dataset.id, batch_size=2, after=cursor
    )
    assert report["created"] == 1
    report, _ = await backfill.backfill_batch(
        db_session, mode="apply", dataset_id=dataset.id
    )
    assert report["created"] == 0 and report["already_mapped"] == 3
    report, _ = await backfill.backfill_batch(
        db_session, mode="verify", dataset_id=dataset.id
    )
    assert report["invalid"] == report["pending"] == 0
    assert (
        await db_session.scalar(select(func.count()).select_from(DatasetAssetLocation))
        == 3
    )
    for item in items:
        await db_session.refresh(item)
    assert before == [
        (item.id, item.file_path, item.updated_at, item.content_hash) for item in items
    ]
    assert dataset.file_count == 3
    assets = list(await db_session.scalars(select(DatasetAsset)))
    assert all(
        asset.verification_state == "unverified" and asset.checksum_value is None
        for asset in assets
    )


@pytest.mark.asyncio
async def test_run_readonly_and_explicit_target_are_enforced(test_engine):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    original = backfill.backfill_batch

    async def attempted_write(db, **kwargs):
        assert await db.scalar(text("SHOW transaction_read_only")) == "on"
        await db.execute(text("CREATE TABLE forbidden_asset_probe (id int)"))
        raise AssertionError("read-only write unexpectedly succeeded")

    args = backfill.parser().parse_args(["--all-datasets", "--dry-run"])
    backfill.backfill_batch = attempted_write
    try:
        with pytest.raises(DBAPIError):
            await backfill.run(args, sessions, lambda report: None)
        args.mode = "verify"
        with pytest.raises(DBAPIError):
            await backfill.run(args, sessions, lambda report: None)
    finally:
        backfill.backfill_batch = original
    with pytest.raises(SystemExit):
        backfill.parser().parse_args(["--apply"])


@pytest.mark.asyncio
async def test_committed_batch_survives_interruption_and_original_command_resumes(
    test_engine,
):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.models.user import User
    from tests.factory import make_user_dict

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    from app.db.models.dataset_asset import StorageBinding, StorageLocation
    from app.services.dataset_assets import LEGACY_LOCATION_ID

    async with sessions() as db:
        previous_purposes = set(await db.scalars(select(StorageBinding.purpose)))
        had_location = await db.get(StorageLocation, LEGACY_LOCATION_ID) is not None
        user = User(
            **make_user_dict("super_admin", f"backfill-{suffix}@test.local", "Backfill")
        )
        db.add(user)
        await db.flush()
        dataset = Dataset(
            name="history", display_id=f"DS-{suffix[:10]}", created_by=user.id
        )
        db.add(dataset)
        await db.flush()
        items = [
            DatasetItem(
                dataset_id=dataset.id,
                file_name=f"{n}.bin",
                file_path=f"historical/{n}.bin",
                file_type="other",
            )
            for n in range(3)
        ]
        db.add_all(items)
        await db.commit()
        dataset_id, user_id = dataset.id, user.id
    args = backfill.parser().parse_args(
        ["--dataset-id", str(dataset_id), "--apply", "--batch-size", "1"]
    )

    class Interrupted(Exception):
        pass

    def interrupt(event):
        if event["type"] == "batch":
            raise Interrupted()

    try:
        with pytest.raises(Interrupted):
            await backfill.run(args, sessions, interrupt)
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(DatasetAsset)
                    .join(DatasetItem, DatasetItem.id == DatasetAsset.dataset_item_id)
                    .where(DatasetItem.dataset_id == dataset_id)
                )
                == 1
            )
        reports = []
        assert await backfill.run(args, sessions, reports.append) == 0
        assert reports[-1]["created"] == 2
        assert reports[-1]["already_mapped"] == 1
        args.mode = "verify"
        assert await backfill.run(args, sessions, reports.append) == 0
        assert reports[-1]["already_mapped"] == 3
        assert reports[-1]["configuration_errors"] == 0
    finally:
        async with sessions() as db:
            await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.execute(
                delete(StorageBinding).where(
                    StorageBinding.location_id == LEGACY_LOCATION_ID,
                    StorageBinding.purpose.notin_(previous_purposes),
                )
            )
            if not had_location:
                await db.execute(
                    delete(StorageLocation).where(
                        StorageLocation.id == LEGACY_LOCATION_ID
                    )
                )
            await db.commit()
