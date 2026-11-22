"""检测 alembic migration 与 SQLAlchemy model 字段一致性。

防止模型与实际数据库迁移不一致，例如模型有 unique=True 但迁移缺少约束。
策略：alembic upgrade head 后，反射真实库 schema，与 Base.metadata 比每张表的
列名集合 + 主键集合，不一致则 fail。

注意：NOT NULL / 默认值 / FK 这种细粒度不比，会有很多 false positive
（PG 类型与 SA 类型映射 / server_default 字符串差异）。仅校验列存在性 + PK，
够用作 sanity check。
"""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData

import app.db.models  # noqa: F401 · 确保全部 model 注册进 Base.metadata（消除导入顺序偶发 drift）
from app.db.base import Base


@pytest.mark.asyncio
async def test_models_match_database(test_engine, apply_migrations):
    reflected = MetaData()
    async with test_engine.connect() as conn:
        await conn.run_sync(reflected.reflect)

    drift: list[str] = []
    for tbl_name, model_tbl in Base.metadata.tables.items():
        if tbl_name not in reflected.tables:
            drift.append(
                f"表 `{tbl_name}` 在模型中存在，但数据库无（migration 漏写？）"
            )
            continue
        db_tbl = reflected.tables[tbl_name]
        model_cols = {c.name for c in model_tbl.columns}
        db_cols = {c.name for c in db_tbl.columns}
        missing_in_db = model_cols - db_cols
        missing_in_model = db_cols - model_cols
        if missing_in_db:
            drift.append(f"`{tbl_name}`：模型有但库没的列 {sorted(missing_in_db)}")
        if missing_in_model:
            drift.append(f"`{tbl_name}`：库有但模型没的列 {sorted(missing_in_model)}")

    audit_partitions = {
        t for t in reflected.tables if t.startswith("audit_logs_y") and len(t) >= 18
    }
    prediction_partitions = {
        t
        for t in reflected.tables
        if t.startswith("predictions_y") or t == "predictions_default"
    }
    db_only_tables = (
        set(reflected.tables.keys())
        - set(Base.metadata.tables.keys())
        - {"alembic_version"}
        - audit_partitions
        - prediction_partitions
    )
    if db_only_tables:
        drift.append(
            f"库有但 Base.metadata 没的表 {sorted(db_only_tables)}（可能 model 未在 __init__.py 注册）"
        )

    assert not drift, "检测到 model ↔ migration drift:\n  - " + "\n  - ".join(drift)
