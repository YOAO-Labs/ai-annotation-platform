"""规模化压测种子数据。

生成 1 个 project + 1 dataset + N 个 task（默认 10 万），分布在 M 个 active 批次；
并让一个标注员对前 K 个 task 留有 active 标注（撑大 scheduler 的「已标注」NOT EXISTS
判定）。task 全程 is_labeled=false，仍是「未标注」候选——正是热路径压测目标。

用法：
    cd apps/api
    uv run python scripts/seed_scale.py            # 默认 N=100000, M=50, K=20000
    uv run python scripts/seed_scale.py 200000 80 40000
    uv run python scripts/seed_scale.py --purge    # 仅清理上次种子，不重建

清理：按 project / dataset 名称标记 'scale-seed' 级联删除（annotations → tasks →
task_batches → dataset_items → datasets → projects）。

跑完打印 project_id / 一个 batch_id / annotator_id，供 EXPLAIN ANALYZE 使用。
"""

import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import text

from app.config import settings

if settings.environment == "production":
    print("[seed_scale] refusing to run with environment=production", file=sys.stderr)
    raise SystemExit(2)

from app.services.workspace_scale_seed_data import (  # noqa: E402
    create_scale_seed_project,
    delete_scale_seed_projects,
    ensure_scale_seed_member,
    guard_scale_seed_sources,
    prepare_scale_seed_cleanup,
)
from app.services.labeling_scale_seed_operations import (  # noqa: E402
    LabelingScaleSeedOperations,
)

MARKER = "scale-seed"

engine = create_async_engine(settings.database_url, echo=False)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _purge(db: AsyncSession) -> None:
    project_ids, dataset_ids = await prepare_scale_seed_cleanup(db, MARKER)
    await LabelingScaleSeedOperations(db).purge_labeling_rows(MARKER, project_ids)
    await guard_scale_seed_sources(db, dataset_ids, after_task_cleanup=True)
    await db.execute(
        text("DELETE FROM dataset_items WHERE dataset_id=ANY(CAST(:ids AS uuid[]))"),
        {"ids": dataset_ids},
    )
    await db.execute(
        text(
            "DELETE FROM datasets WHERE id=ANY(CAST(:ids AS uuid[])) AND name=:marker"
        ),
        {"ids": dataset_ids, "marker": MARKER},
    )
    await delete_scale_seed_projects(db, marker=MARKER, project_ids=project_ids)
    await db.commit()


async def seed(n_tasks: int, n_batches: int, n_annotated: int) -> None:
    from app.services.identity_commands import IdentityCommandService

    async with Session() as db:
        labeling = LabelingScaleSeedOperations(db)
        await _purge(db)

        owner_id, annotator_id = await IdentityCommandService(
            db
        ).resolve_scale_seed_actors()
        if not owner_id or not annotator_id:
            print(
                "[seed_scale] need at least 1 super_admin and 1 user with the "
                "annotator project duty via project_members (run seed.py first)"
            )
            raise SystemExit(1)

        project_id = await create_scale_seed_project(
            db, owner_id=owner_id, marker=MARKER, total_tasks=n_tasks
        )
        await ensure_scale_seed_member(
            db, project_id=project_id, annotator_id=annotator_id, owner_id=owner_id
        )

        dataset_id = (
            await db.execute(
                text(
                    "INSERT INTO datasets (id, display_id, name, created_by) "
                    "VALUES (gen_random_uuid(), 'D-'||nextval('display_seq_datasets'), :m, :owner) "
                    "RETURNING id"
                ),
                {"m": MARKER, "owner": owner_id},
            )
        ).scalar()

        await db.execute(
            text(
                "INSERT INTO dataset_items (id, dataset_id, file_name, file_path) "
                "SELECT gen_random_uuid(), :ds, 'img_'||g||'.jpg', '/seed/'||g||'.jpg' "
                "FROM generate_series(1, :n) g"
            ),
            {"ds": dataset_id, "n": n_tasks},
        )

        await labeling.create_batches(
            MARKER, project_id, dataset_id, n_batches, annotator_id
        )
        await db.commit()

        # tasks：按 dataset_item 行号轮转分配到批次；created_at 错开以让排序有区分度。
        await labeling.create_tasks(MARKER, project_id, dataset_id)
        await db.commit()

        # 标注员对前 K 个 task 留 active 标注（task 仍 is_labeled=false）。
        await labeling.create_annotations(MARKER, project_id, annotator_id, n_annotated)
        await db.commit()

        a_batch = await labeling.read_first_batch_id(project_id)

        print(
            f"[seed_scale] done: {n_tasks} tasks / {n_batches} batches / {n_annotated} annotated"
        )
        print(f"  project_id   = {project_id}")
        print(f"  a_batch_id   = {a_batch}")
        print(f"  annotator_id = {annotator_id}")
        print("  EXPLAIN 示例：")
        print(
            f"    EXPLAIN ANALYZE SELECT * FROM aap_labeling_v1.task_rows "
            f"WHERE project_id='{project_id}' "
            "ORDER BY created_at, id LIMIT 100;"
        )

    await engine.dispose()


def _parse_args() -> tuple[bool, int, int, int]:
    args = sys.argv[1:]
    if args and args[0] == "--purge":
        return True, 0, 0, 0
    n = int(args[0]) if len(args) > 0 else 100_000
    m = int(args[1]) if len(args) > 1 else 50
    k = int(args[2]) if len(args) > 2 else 20_000
    return False, n, m, k


async def _main() -> None:
    purge_only, n, m, k = _parse_args()
    if purge_only:
        async with Session() as db:
            await _purge(db)
        await engine.dispose()
        print(f"[seed_scale] purged all '{MARKER}' seed data")
        return
    await seed(n, m, k)


if __name__ == "__main__":
    asyncio.run(_main())
