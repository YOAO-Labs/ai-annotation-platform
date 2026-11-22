"""0118 迁移（compact track identity 回填 + 索引）的 DB 集成测试。

**需要真实 Postgres**（用到 ``gen_random_uuid()`` / jsonb ``->>`` 运算符），依赖
``conftest.py`` 的 ``db_session`` fixture（TEST_DATABASE_URL / annotation_test 库）。

``apply_migrations`` 是 session 级、只在整个测试会话开始时跑一次 ``alembic upgrade
head``（此时 annotations 表通常还没有数据），所以没法靠"重新跑一次 alembic"覆盖 0118
那段回填 SQL 对**已有脏数据**的分支。这里改为在 ``db_session`` 的同一 SAVEPOINT 事务
内手工插入模拟"迁移前脏数据"的 annotation 行，再直接调用 0118 模块的 ``upgrade()``
（通过 ``alembic.operations.Operations.context`` 把全局 ``op`` 代理绑定到本连接），
逐条验证 SQL 的 CASE 分支——不重复摘抄 SQL 文本，跑的就是模块里那段真实语句。

``ix_annotations_project_track_active`` 索引已经在 session 级 ``apply_migrations``
里建过一次；``upgrade()`` 里 ``CREATE INDEX`` 没有 ``IF NOT EXISTS``，直接重跑会因为
索引已存在而报错，所以每次调用前先 ``DROP INDEX IF EXISTS``（同一事务内，测试结束
随 SAVEPOINT 回滚，不影响其他测试）。
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from sqlalchemy import select, text

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task
from tests.factory import build_tool_bindings


def _load_migration_0118():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0118_compact_track_identity_sync.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0118", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _run_upgrade_sync(sync_connection) -> None:
    """在给定的同步连接上跑一次 0118 ``upgrade()``。

    先 drop 掉可能已存在的索引（session 级 ``apply_migrations`` 已建过一次；
    这里要在同一事务内允许重复调用 ``upgrade()`` 来验证幂等性）。
    """
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    sync_connection.execute(
        text("DROP INDEX IF EXISTS ix_annotations_project_track_active")
    )
    migration_context = MigrationContext.configure(sync_connection)
    with Operations.context(migration_context):
        _load_migration_0118().upgrade()


async def _run_upgrade(db_session) -> None:
    conn = await db_session.connection()
    await conn.run_sync(_run_upgrade_sync)


async def _make_project_and_task(db_session, super_admin) -> Task:
    user, _token = super_admin
    project = Project(
        display_id=f"P-0118-{uuid.uuid4().hex[:6]}",
        name="Track Identity Migration Project",
        type_key="video-track",
        type_label="视频 · 时序追踪",
        owner_id=user.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        display_id=f"T-0118-{uuid.uuid4().hex[:6]}",
        file_name="clip.mp4",
        file_path="videos/clip.mp4",
        file_type="video",
        status="pending",
    )
    db_session.add(task)
    await db_session.flush()
    return task


async def _reload(db_session, ann_id) -> Annotation:
    result = await db_session.execute(
        select(Annotation)
        .where(Annotation.id == ann_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def test_0118_backfills_malformed_and_missing_track_ids(db_session, super_admin):
    task = await _make_project_and_task(db_session, super_admin)

    overlong_id = "legacy-" + "x" * 80
    ann_malformed = Annotation(
        task_id=task.id,
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "video_track_bbox", "track_id": overlong_id, "keyframes": []},
        track_id=None,
    )
    ann_from_geometry = Annotation(
        task_id=task.id,
        class_name="car",
        tool_unit_id="bbox",
        geometry={
            "type": "video_track_polygon",
            "track_id": "trk_legacy_abc123",
            "keyframes": [],
        },
        track_id=None,
    )
    ann_non_track = Annotation(
        task_id=task.id,
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        track_id=None,
    )
    db_session.add_all([ann_malformed, ann_from_geometry, ann_non_track])
    await db_session.flush()
    malformed_id, from_geometry_id, non_track_id = (
        ann_malformed.id,
        ann_from_geometry.id,
        ann_non_track.id,
    )

    await _run_upgrade(db_session)

    malformed = await _reload(db_session, malformed_id)
    assert malformed.track_id is not None
    assert malformed.track_id.startswith("trk_")
    assert malformed.track_id != overlong_id
    assert len(malformed.track_id) <= 64
    assert malformed.geometry["track_id"] == malformed.track_id

    from_geometry = await _reload(db_session, from_geometry_id)
    assert from_geometry.track_id == "trk_legacy_abc123"
    assert from_geometry.geometry["track_id"] == "trk_legacy_abc123"

    non_track = await _reload(db_session, non_track_id)
    assert non_track.track_id is None

    resolved_malformed_id = malformed.track_id
    resolved_from_geometry_id = from_geometry.track_id

    await _run_upgrade(db_session)

    malformed_again = await _reload(db_session, malformed_id)
    from_geometry_again = await _reload(db_session, from_geometry_id)
    assert malformed_again.track_id == resolved_malformed_id
    assert from_geometry_again.track_id == resolved_from_geometry_id
    assert malformed_again.geometry["track_id"] == resolved_malformed_id
    assert from_geometry_again.geometry["track_id"] == resolved_from_geometry_id
