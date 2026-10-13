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

MARKER = "scale-seed"

engine = create_async_engine(settings.database_url, echo=False)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _purge(db: AsyncSession) -> None:
    await db.execute(
        text(
            "DELETE FROM annotations WHERE task_id IN "
            "(SELECT t.id FROM tasks t JOIN projects p ON p.id = t.project_id "
            " WHERE p.name = :m)"
        ),
        {"m": MARKER},
    )
    await db.execute(
        text(
            "DELETE FROM tasks WHERE project_id IN "
            "(SELECT id FROM projects WHERE name = :m)"
        ),
        {"m": MARKER},
    )
    await db.execute(
        text(
            "DELETE FROM task_batches WHERE project_id IN "
            "(SELECT id FROM projects WHERE name = :m)"
        ),
        {"m": MARKER},
    )
    await db.execute(
        text(
            "DELETE FROM dataset_items WHERE dataset_id IN "
            "(SELECT id FROM datasets WHERE name = :m)"
        ),
        {"m": MARKER},
    )
    await db.execute(text("DELETE FROM datasets WHERE name = :m"), {"m": MARKER})
    await db.execute(text("DELETE FROM projects WHERE name = :m"), {"m": MARKER})
    await db.commit()


async def seed(n_tasks: int, n_batches: int, n_annotated: int) -> None:
    async with Session() as db:
        await _purge(db)

        owner_id = (
            await db.execute(
                text(
                    "SELECT id FROM users WHERE role = 'super_admin' ORDER BY created_at LIMIT 1"
                )
            )
        ).scalar()
        # Project duties live on project_members; the account platform role is
        # no longer an annotator/reviewer value after the 0174 employee cutover.
        annotator_id = (
            await db.execute(
                text(
                    "SELECT u.id FROM users u "
                    "JOIN project_members pm ON pm.user_id = u.id "
                    "WHERE pm.role = 'annotator' AND u.is_active "
                    "ORDER BY u.created_at LIMIT 1"
                )
            )
        ).scalar()
        if not owner_id or not annotator_id:
            print(
                "[seed_scale] need at least 1 super_admin and 1 user with the "
                "annotator project duty via project_members (run seed.py first)"
            )
            raise SystemExit(1)

        project_id = (
            await db.execute(
                text(
                    "INSERT INTO projects (id, display_id, name, type_label, type_key, owner_id, total_tasks) "
                    "VALUES (gen_random_uuid(), 'P-'||nextval('display_seq_projects'), :m, "
                    "'图像检测', 'image_detection', :owner, :n) RETURNING id"
                ),
                {"m": MARKER, "owner": owner_id, "n": n_tasks},
            )
        ).scalar()

        # Explicit project duty so the stress project is actually reachable by
        # the annotator under the project-scoped access model.
        await db.execute(
            text(
                "INSERT INTO project_members (id, project_id, user_id, role, assigned_by) "
                "VALUES (gen_random_uuid(), :proj, :anno, 'annotator', :owner) "
                "ON CONFLICT (project_id, user_id) DO NOTHING"
            ),
            {"proj": project_id, "anno": annotator_id, "owner": owner_id},
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

        await db.execute(
            text(
                "INSERT INTO task_batches (project_id, dataset_id, display_id, name, status, annotator_id) "
                "SELECT :proj, :ds, 'B-'||nextval('display_seq_batches'), 'batch '||g, 'active', :anno "
                "FROM generate_series(1, :m) g"
            ),
            {
                "proj": project_id,
                "ds": dataset_id,
                "m": n_batches,
                "anno": annotator_id,
            },
        )
        await db.commit()

        # tasks：按 dataset_item 行号轮转分配到批次；created_at 错开以让排序有区分度。
        await db.execute(
            text(
                "WITH b AS ("
                "  SELECT id, row_number() OVER (ORDER BY display_id) - 1 AS rn,"
                "         count(*) OVER () AS cnt FROM task_batches WHERE project_id = :proj"
                "), items AS ("
                "  SELECT id, row_number() OVER (ORDER BY file_name) - 1 AS rn"
                "  FROM dataset_items WHERE dataset_id = :ds"
                ") "
                "INSERT INTO tasks (id, project_id, dataset_item_id, batch_id, display_id, "
                "  file_name, file_path, file_type, status, sequence_order, created_at, is_labeled) "
                "SELECT gen_random_uuid(), :proj, items.id, "
                "  (SELECT id FROM b WHERE b.rn = items.rn % (SELECT cnt FROM b LIMIT 1)), "
                "  'T-'||nextval('display_seq_tasks'), "
                "  'img_'||items.rn||'.jpg', '/seed/'||items.rn||'.jpg', 'image', 'pending', "
                "  items.rn, now() - make_interval(secs => items.rn), false "
                "FROM items"
            ),
            {"proj": project_id, "ds": dataset_id},
        )
        await db.commit()

        # 标注员对前 K 个 task 留 active 标注（task 仍 is_labeled=false）。
        # geometry 用绑定参数传入：text() 会把内联 JSON 里的 ":0" 误解析为绑定参数。
        await db.execute(
            text(
                "INSERT INTO annotations (id, task_id, user_id, project_id, class_name, geometry, is_active) "
                "SELECT gen_random_uuid(), t.id, :anno, :proj, 'seed', cast(:geom AS jsonb), true "
                "FROM (SELECT id FROM tasks WHERE project_id = :proj ORDER BY sequence_order LIMIT :k) t"
            ),
            {
                "anno": annotator_id,
                "proj": project_id,
                "k": n_annotated,
                "geom": '{"type":"bbox","x":0,"y":0,"w":1,"h":1}',
            },
        )
        await db.commit()

        a_batch = (
            await db.execute(
                text(
                    "SELECT id FROM task_batches WHERE project_id = :proj ORDER BY display_id LIMIT 1"
                ),
                {"proj": project_id},
            )
        ).scalar()

        print(
            f"[seed_scale] done: {n_tasks} tasks / {n_batches} batches / {n_annotated} annotated"
        )
        print(f"  project_id   = {project_id}")
        print(f"  a_batch_id   = {a_batch}")
        print(f"  annotator_id = {annotator_id}")
        print("  EXPLAIN 示例：")
        print(
            f"    EXPLAIN ANALYZE SELECT * FROM tasks WHERE project_id='{project_id}' "
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
