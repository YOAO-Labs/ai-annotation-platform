"""
清空所有数据集记录（dataset_items, project_datasets, datasets）。
此命令只清理数据库行，不遍历或删除 datasets 桶中的 MinIO 对象。

用法：
    cd apps/api
    uv run python scripts/reset_datasets.py
"""

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.application.dataset_reset import DatasetResetWorkflow
from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def reset() -> None:
    async with Session() as db:
        workflow = DatasetResetWorkflow(db)
        rows = await workflow.list_datasets()
        if not rows:
            print("没有数据集需要清理")
            return

        print(f"即将删除 {len(rows)} 个数据集：")
        for row in rows:
            print(f"  {row.display_id}  {row.name}")

        await workflow.reset()
        await db.commit()
        print(f"\n已清空 {len(rows)} 个数据集及关联记录")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(reset())
