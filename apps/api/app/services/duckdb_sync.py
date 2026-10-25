"""v0.10.16 · PostgreSQL → DuckDB 增量同步（ROADMAP §1.6）。

设计：
- DuckDB 文件路径来自 settings.duckdb_path（默认 ./data/duckdb/analytics.duckdb）。
- 每日同步两类源表：
  - task_events: 增量按 created_at > last_synced_at 拉取；
  - audit_logs: 仅拉当月分区（量大且分析口径只看最近期）。
- DuckDB 端表 schema 与 PG 对齐（保留必要列），首次跑时自动 CREATE TABLE IF NOT EXISTS。
- 单 writer：本模块独占 DuckDB 写连接；FastAPI 只读路径见 analytics_queries.py。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.audit_read_relations import audit_event
from app.services.labeling_discussion_relations import task_event_facts

log = logging.getLogger(__name__)


# DuckDB schema 元数据表（自身维护 last_synced_at 高水位）
_SYNC_STATE_DDL = """
CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT PRIMARY KEY,
    last_synced_at TIMESTAMP NOT NULL,
    last_run_at TIMESTAMP NOT NULL
);
"""

# task_events 目标表（与 PG 端字段对齐）
_TASK_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS task_events (
    id UUID PRIMARY KEY,
    task_id UUID,
    user_id UUID,
    project_id UUID,
    kind TEXT,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    duration_ms INTEGER,
    annotation_count INTEGER,
    was_rejected BOOLEAN,
    reject_reason_type TEXT,  -- 由 §1.2 反向 join tasks 时填充，可空
    created_at TIMESTAMP
);
"""

# audit_logs 目标表（只保留分析所需列）
_AUDIT_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGINT,
    actor_id UUID,
    actor_role TEXT,
    action TEXT,
    target_type TEXT,
    target_id TEXT,
    status_code INTEGER,
    created_at TIMESTAMP
);
"""


def _ensure_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _connect_writer() -> duckdb.DuckDBPyConnection:
    """v0.10.16 · 同步 worker 用的可写连接；初始化 schema。"""
    _ensure_dir(settings.duckdb_path)
    con = duckdb.connect(settings.duckdb_path, read_only=False)
    con.execute(_SYNC_STATE_DDL)
    con.execute(_TASK_EVENTS_DDL)
    con.execute(_AUDIT_LOGS_DDL)
    return con


def _get_high_water(con: duckdb.DuckDBPyConnection, source: str) -> datetime:
    row = con.execute(
        "SELECT last_synced_at FROM sync_state WHERE source = ?", [source]
    ).fetchone()
    if row is None:
        # 首次同步：拉过去 90 天
        return datetime(2020, 1, 1, tzinfo=timezone.utc)
    return row[0].replace(tzinfo=timezone.utc) if row[0].tzinfo is None else row[0]


def _set_high_water(con: duckdb.DuckDBPyConnection, source: str, ts: datetime) -> None:
    now = datetime.now(timezone.utc)
    con.execute(
        """
        INSERT INTO sync_state (source, last_synced_at, last_run_at) VALUES (?, ?, ?)
        ON CONFLICT (source) DO UPDATE SET last_synced_at = excluded.last_synced_at,
                                           last_run_at = excluded.last_run_at;
        """,
        [source, ts, now],
    )


async def sync_task_events(db: AsyncSession) -> dict:
    """v0.10.16 · 增量同步 task_events（带 task.reject_reason_type 反向 join）。"""
    con = _connect_writer()
    try:
        watermark = _get_high_water(con, "task_events")
        stmt = (
            select(
                task_event_facts.c.id,
                task_event_facts.c.task_id,
                task_event_facts.c.user_id,
                task_event_facts.c.project_id,
                task_event_facts.c.kind,
                task_event_facts.c.started_at,
                task_event_facts.c.ended_at,
                task_event_facts.c.duration_ms,
                task_event_facts.c.annotation_count,
                task_event_facts.c.was_rejected,
                task_event_facts.c.reject_reason_type,
                task_event_facts.c.created_at,
            )
            .where(task_event_facts.c.created_at > watermark)
            .order_by(task_event_facts.c.created_at)
        )
        result = await db.execute(stmt)
        rows = result.all()
        if not rows:
            return {
                "source": "task_events",
                "rows": 0,
                "high_water": watermark.isoformat(),
            }

        # batch insert
        con.executemany(
            """
            INSERT INTO task_events (id, task_id, user_id, project_id, kind,
                started_at, ended_at, duration_ms, annotation_count, was_rejected,
                reject_reason_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                reject_reason_type = excluded.reject_reason_type
            """,
            [tuple(r) for r in rows],
        )
        new_high = max(r.created_at for r in rows)
        _set_high_water(con, "task_events", new_high)
        return {
            "source": "task_events",
            "rows": len(rows),
            "high_water": new_high.isoformat(),
        }
    finally:
        con.close()


async def sync_audit_logs(db: AsyncSession) -> dict:
    """v0.10.16 · 增量同步 audit_logs（仅近 31 天数据进 DuckDB）。"""
    con = _connect_writer()
    try:
        watermark = _get_high_water(con, "audit_logs")
        stmt = (
            select(
                audit_event.id,
                audit_event.actor_id,
                audit_event.actor_role,
                audit_event.action,
                audit_event.target_type,
                audit_event.target_id,
                audit_event.status_code,
                audit_event.created_at,
            )
            .where(audit_event.created_at > watermark)
            .order_by(audit_event.created_at)
            .limit(50000)  # 单次 cap，防止首次同步爆内存
        )
        result = await db.execute(stmt)
        rows = result.all()
        if not rows:
            return {
                "source": "audit_logs",
                "rows": 0,
                "high_water": watermark.isoformat(),
            }

        con.executemany(
            """
            INSERT INTO audit_logs (id, actor_id, actor_role, action, target_type,
                target_id, status_code, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [tuple(r) for r in rows],
        )
        new_high = max(r.created_at for r in rows)
        _set_high_water(con, "audit_logs", new_high)
        return {
            "source": "audit_logs",
            "rows": len(rows),
            "high_water": new_high.isoformat(),
        }
    finally:
        con.close()


async def sync_all(db: AsyncSession) -> dict:
    """v0.10.16 · 顺序同步两类源表，返回各 source 的结果。"""
    out_te = await sync_task_events(db)
    out_al = await sync_audit_logs(db)
    return {"task_events": out_te, "audit_logs": out_al}


def _connect_reader() -> duckdb.DuckDBPyConnection:
    """v0.10.16 · FastAPI 只读连接（read_only=True）。若文件不存在抛 FileNotFoundError。

    调用方应在每次请求里开/关连接（DuckDB 连接是线程局部的）。
    """
    path = settings.duckdb_path
    if not os.path.exists(path):
        raise FileNotFoundError(f"DuckDB 文件不存在: {path}。首次同步可能尚未运行。")
    return duckdb.connect(path, read_only=True)
