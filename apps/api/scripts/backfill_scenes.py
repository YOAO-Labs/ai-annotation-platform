"""v0.14.0 · 一次性脚本:对历史 dataset 补 scene + frame_index。

通过 Assets Spatial owner 回填 scene,并提供 CLI / 日志 / 错误码退出。

用法:
    cd apps/api
    # 单 dataset
    uv run python scripts/backfill_scenes.py --dataset-id <uuid>
    # 所有缺 scene 的 dataset
    uv run python scripts/backfill_scenes.py --all-missing
    # 预览(不写库)
    uv run python scripts/backfill_scenes.py --all-missing --dry-run
    # 显式指定 mode(默认 auto)
    uv run python scripts/backfill_scenes.py --dataset-id <uuid> --mode single

幂等:
- 已有 scene 的 dataset 跳过(notes 提示);
- 部分 items 已有 scene_id 的 dataset 跳过("partial migration"),需人工 review。

退出码:
- 0:全部 dataset 处理成功(包含全部跳过)
- 1:至少一个 dataset 报错(ValueError,如推出 > 100 scene)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aap_assets_catalog.spatial_contract import (
    BackfillScenesRequest,
    LayoutMutationResult,
)
from app.config import settings
from app.db.models.dataset import Dataset
from app.services.assets_spatial import AssetsSpatialGateway
from app.services.assets_spatial_views import spatial_scene_references

logger = logging.getLogger("backfill_scenes")


async def _datasets_missing_scene(db: AsyncSession) -> list[uuid.UUID]:
    """返回所有"无 scene"的 dataset。"""
    sub = select(spatial_scene_references.c.dataset_id).distinct().subquery()
    rows = await db.execute(
        select(Dataset.id).where(
            ~exists(select(sub.c.dataset_id).where(sub.c.dataset_id == Dataset.id))
        )
    )
    return list(rows.scalars())


def _print_result(res: LayoutMutationResult) -> None:
    logger.info(
        "dataset=%s created_scenes=%s assigned_items=%s skipped_items=%s dry_run=%s",
        res.dataset_id,
        res.created_scenes,
        res.assigned_items,
        res.skipped_items,
        res.dry_run,
    )
    for note in res.notes:
        logger.info("  note: %s", note)


async def _run(
    *,
    dataset_ids: list[uuid.UUID] | None,
    all_missing: bool,
    mode: str,
    dry_run: bool,
) -> int:
    engine = create_async_engine(settings.database_url, echo=False)
    sessionmaker = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    error_count = 0
    async with sessionmaker() as db:
        if all_missing:
            datasets = await _datasets_missing_scene(db)
            if not datasets:
                logger.info("no datasets missing scene; nothing to do")
                await engine.dispose()
                return 0
            targets = datasets
            logger.info("found %d dataset(s) missing scene", len(targets))
        else:
            assert dataset_ids is not None
            targets = dataset_ids

        for did in targets:
            try:
                res = await AssetsSpatialGateway(db).backfill_scenes(
                    BackfillScenesRequest(
                        operation_id=uuid.uuid4(),
                        actor_id=None,
                        dataset_id=did,
                        mode=mode,
                        dry_run=dry_run,
                    )
                )
            except ValueError as exc:
                logger.error("dataset=%s failed: %s", did, exc)
                error_count += 1
                # 单个失败不阻塞其他;但脚本最终退出码非 0
                await db.rollback()
                continue
            _print_result(res)

    await engine.dispose()
    return 1 if error_count else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dataset-id", action="append", type=uuid.UUID, help="可重复指定")
    grp.add_argument(
        "--all-missing", action="store_true", help="所有无 scene 的 dataset"
    )
    parser.add_argument(
        "--mode",
        choices=["single", "per_subdirectory", "auto"],
        default="auto",
        help="scene 推断模式,默认 auto",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rc = asyncio.run(
        _run(
            dataset_ids=args.dataset_id,
            all_missing=args.all_missing,
            mode=args.mode,
            dry_run=args.dry_run,
        )
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
