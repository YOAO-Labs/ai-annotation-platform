"""DuckDB 固定分析面板查询（ROADMAP §1.6）。

仅暴露 3 个 enum 面板，**不**接收任意 SQL（防注入 + 防慢查询）。
"""

from __future__ import annotations

from typing import Any

from app.services.duckdb_sync import _connect_reader


def user_throughput_daily(days: int = 30) -> list[dict[str, Any]]:
    """人均日吞吐（最近 N 天 task_events.kind='annotate' 提交数）。"""
    days = max(1, min(int(days), 365))
    con = _connect_reader()
    try:
        rows = con.execute(
            f"""
            SELECT
                date_trunc('day', created_at) AS day,
                user_id::TEXT AS user_id,
                count(*) AS event_count
            FROM task_events
            WHERE kind = 'annotate'
              AND created_at > current_timestamp - INTERVAL '{days} days'
              AND user_id IS NOT NULL
            GROUP BY 1, 2
            ORDER BY 1, 2;
            """,
        ).fetchall()
        return [
            {"day": r[0].isoformat(), "user_id": r[1], "event_count": int(r[2])}
            for r in rows
        ]
    finally:
        con.close()


def reject_rate_by_type(days: int = 30) -> list[dict[str, Any]]:
    """reject 率按类型分布（依赖 §1.2 reject_reason_type 列）。

    分母只算 reject_reason_type IS NOT NULL 的 review 事件（旧数据不污染）。
    """
    days = max(1, min(int(days), 365))
    con = _connect_reader()
    try:
        rows = con.execute(
            f"""
            SELECT
                reject_reason_type,
                count(*) AS rejected_n
            FROM task_events
            WHERE was_rejected = TRUE
              AND reject_reason_type IS NOT NULL
              AND created_at > current_timestamp - INTERVAL '{days} days'
            GROUP BY 1
            ORDER BY 2 DESC;
            """,
        ).fetchall()
        total = sum(int(r[1]) for r in rows)
        return [
            {
                "reason_type": r[0],
                "count": int(r[1]),
                "pct": (int(r[1]) / total * 100) if total else 0,
            }
            for r in rows
        ]
    finally:
        con.close()


def activity_heatmap(days: int = 30) -> list[dict[str, Any]]:
    """工时热力图（按星期几 × 小时聚合 annotate 事件计数）。

    用 started_at（工时实际发生时刻，比 created_at 更贴合）。
    DuckDB `dayofweek()` 约定 0=周日 .. 6=周六。

    只返回出现过的 (weekday, hour) 行，前端补零成完整 7×24 网格。
    """
    days = max(1, min(int(days), 365))
    con = _connect_reader()
    try:
        rows = con.execute(
            f"""
            SELECT
                CAST(dayofweek(started_at) AS INTEGER) AS weekday,
                CAST(extract('hour' FROM started_at) AS INTEGER) AS hour,
                count(*) AS n
            FROM task_events
            WHERE kind = 'annotate'
              AND started_at IS NOT NULL
              AND started_at > current_timestamp - INTERVAL '{days} days'
            GROUP BY 1, 2
            ORDER BY 1, 2;
            """,
        ).fetchall()
        return [
            {"weekday": int(r[0]), "hour": int(r[1]), "count": int(r[2])} for r in rows
        ]
    finally:
        con.close()


def annotation_duration_distribution(days: int = 30) -> dict[str, Any]:
    """标注耗时分布（p50 / p95 / 平均，单位 ms）。"""
    days = max(1, min(int(days), 365))
    con = _connect_reader()
    try:
        row = con.execute(
            f"""
            SELECT
                count(*) AS n,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
                avg(duration_ms) AS mean
            FROM task_events
            WHERE kind = 'annotate'
              AND duration_ms IS NOT NULL
              AND created_at > current_timestamp - INTERVAL '{days} days';
            """,
        ).fetchone()
        if row is None:
            return {"n": 0, "p50": 0, "p95": 0, "mean": 0}
        return {
            "n": int(row[0] or 0),
            "p50": int(row[1] or 0),
            "p95": int(row[2] or 0),
            "mean": int(row[3] or 0),
        }
    finally:
        con.close()
