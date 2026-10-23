"""v0.7.6 · AuditMiddleware 异步化 Celery task。

AuditMiddleware 写每条写请求一行 audit_logs，原同步 INSERT 在主请求 dispatch 后旁路 1-3ms。
本 task 把 Audit owner 追加移出请求路径：中间件改
`persist_audit_entry.delay(payload)`，主请求 < 0.1ms。

Fallback：当 settings.audit_async = False 或 broker 不可用时，中间件回退同步路径。
本 task 使用与同步回退相同的版本化 Audit append owner 能力。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.audit.persist_audit_entry")
def persist_audit_entry(payload: dict[str, Any]) -> None:
    """把 AuditMiddleware 收集的 payload 交给同事务 Audit append owner。

    payload shape（与 AuditLog 列对齐）：
      {
        actor_id: str | None,
        actor_role: str | None,
        action: str,
        method: str,
        path: str,
        status_code: int,
        ip: str | None,
        request_id: str | None,
      }
    """
    asyncio.run(_async_persist(payload))


async def _async_persist(payload: dict[str, Any]) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.services.audit import append_http_metadata
    from app.workers._db import task_session

    async with task_session() as session:
        try:
            await append_http_metadata(session, payload)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise
        except Exception:
            await session.rollback()
            logger.warning(
                "persist_audit_entry commit failed action=%s", payload["action"]
            )
            raise
