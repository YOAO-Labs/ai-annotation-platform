"""Wait for one owner-settled SMTP diagnostic; the API never opens SMTP."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_QUEUE = text("SELECT yoao_settings_v1.queue_smtp_test(CAST(:actor AS uuid))")
_RESULT = text(
    "SELECT yoao_settings_v1.read_smtp_test(CAST(:actor AS uuid), CAST(:attempt AS uuid))"
)


class SmtpConfigError(Exception):
    pass


async def send_test_email(db: AsyncSession, actor_id: uuid.UUID) -> dict[str, Any]:
    attempt = await db.scalar(_QUEUE, {"actor": actor_id})
    if attempt is None:
        raise HTTPException(403, detail="需要角色权限: super_admin")
    await db.commit()
    try:
        async with asyncio.timeout(60):
            while True:
                result = await db.scalar(
                    _RESULT, {"actor": actor_id, "attempt": attempt}
                )
                await db.rollback()
                if result is not None:
                    break
                await asyncio.sleep(0.5)
    except TimeoutError:
        raise SmtpConfigError("SMTP 发送失败: transport_failed") from None

    reason = result["reason"]
    if reason == "sent":
        return {key: result[key] for key in ("to", "from", "host", "port")}
    if reason == "not_authorized":
        raise HTTPException(403, detail="需要角色权限: super_admin")
    if reason == "not_configured":
        raise SmtpConfigError("SMTP 未完整配置（host / port / from 必填）")
    if reason == "message_invalid":
        raise SmtpConfigError("SMTP 邮件内容无效")
    raise SmtpConfigError(f"SMTP 发送失败: {reason}")
