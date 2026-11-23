"""
清空所有数据集记录（dataset_items, project_datasets, datasets）。
此命令只清理数据库行，不遍历或删除 datasets 桶中的 MinIO 对象。

用法：
    cd apps/api
    MIGRATION_DATABASE_URL="$RESET_TARGET_OWNER_URL" uv run python scripts/reset_datasets.py
"""

import asyncio
import os

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def reset() -> None:
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        raise SystemExit(
            "请显式设置目标库的 MIGRATION_DATABASE_URL；不会使用普通运行连接"
        )
    from app.application.dataset_reset import DatasetResetWorkflow

    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as db, db.begin():
            workflow = DatasetResetWorkflow(db)
            rows = await workflow.list_datasets()
            if not rows:
                print("没有数据集需要清理")
                return

            print(f"即将删除 {len(rows)} 个数据集：")
            for row in rows:
                print(f"  {row.display_id}  {row.name}")

            await workflow.reset()
        print(f"\n已清空 {len(rows)} 个数据集及关联记录")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(reset())
