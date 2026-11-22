"""display_id 序列化生成器测试

验证：
- 顺序生成（单 session）
- 并发安全（多 session）
- 前缀映射正确
- B-DEFAULT 哨兵不碰撞迁移逻辑（间接：迁移不抛异常）
"""

from __future__ import annotations

import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.display_id import next_display_id, ENTITY_TO_PREFIX


pytestmark = pytest.mark.usefixtures("apply_migrations")


@pytest.mark.asyncio
async def test_prefix_mapping_complete() -> None:
    assert ENTITY_TO_PREFIX == {
        "bug_reports": "B",
        "tasks": "T",
        "datasets": "D",
        "projects": "P",
        "batches": "BT",
        "project_templates": "PT",
        "scenes": "SCN",
    }


@pytest.mark.asyncio
async def test_sequential_generation(db_session: AsyncSession) -> None:
    a = await next_display_id(db_session, "tasks")
    b = await next_display_id(db_session, "tasks")
    c = await next_display_id(db_session, "tasks")

    for x in (a, b, c):
        assert x.startswith("T-")

    nums = [int(x.split("-")[1]) for x in (a, b, c)]
    assert nums[1] == nums[0] + 1
    assert nums[2] == nums[1] + 1


@pytest.mark.asyncio
async def test_unknown_entity_rejected(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await next_display_id(db_session, "no_such_table")


@pytest.mark.asyncio
async def test_concurrent_uniqueness(test_engine) -> None:
    """50 并发 next_display_id 返回 50 个 unique 值。

    注意：不能用 db_session fixture，每个并发任务需要独立 session/connection。
    """
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def one_call() -> str:
        async with maker() as s:
            return await next_display_id(s, "datasets")

    results = await asyncio.gather(*[one_call() for _ in range(50)])
    assert len(set(results)) == 50, f"有重复 display_id: {results}"
    for r in results:
        assert r.startswith("D-")


@pytest.mark.asyncio
async def test_prefix_per_entity(db_session: AsyncSession) -> None:
    proj = await next_display_id(db_session, "projects")
    bug = await next_display_id(db_session, "bug_reports")
    batch = await next_display_id(db_session, "batches")
    assert proj.startswith("P-")
    assert bug.startswith("B-")
    assert batch.startswith("BT-")
