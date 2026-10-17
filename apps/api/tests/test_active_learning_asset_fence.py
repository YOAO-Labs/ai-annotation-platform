"""Assets lock candidate source rows without waiting on deletion or mutation."""

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.user import User
from app.services.assets_active_learning_fence import (
    SqlAlchemyAssetsActiveLearningFence,
)
from app.services.assets_active_learning_fence_contract import (
    CandidateItemFence,
    CandidateItemFenceBusy,
)


async def test_source_item_for_update_conflict_maps_to_busy(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    user_id, dataset_id, item_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    owner = User(
        id=user_id,
        email=f"active-pool-{uuid.uuid4().hex}@test.local",
        name="Active Pool Test",
        password_hash="test-only",
    )
    dataset = Dataset(
        id=dataset_id,
        display_id="D-" + uuid.uuid4().hex[:10],
        name="Active Pool Test",
        created_by=user_id,
    )
    item = DatasetItem(
        id=item_id,
        dataset_id=dataset_id,
        file_name="source.png",
        file_path="source/source.png",
        file_type="image",
    )

    async with maker() as seed:
        seed.add(owner)
        await seed.flush()
        seed.add(dataset)
        await seed.flush()
        seed.add(item)
        await seed.commit()

    holder, contender = maker(), maker()
    try:
        await holder.execute(
            select(DatasetItem.id).where(DatasetItem.id == item_id).with_for_update()
        )
        fence = SqlAlchemyAssetsActiveLearningFence(contender)
        with pytest.raises(CandidateItemFenceBusy):
            await fence.lock_candidate_items(CandidateItemFence((item_id,)))
    finally:
        await holder.rollback()
        await contender.rollback()
        await holder.close()
        await contender.close()
        async with maker() as cleanup:
            await cleanup.execute(delete(DatasetItem).where(DatasetItem.id == item_id))
            await cleanup.execute(delete(Dataset).where(Dataset.id == dataset_id))
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()
