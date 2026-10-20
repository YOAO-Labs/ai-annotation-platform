"""Invitation and diagnostic mail using the shared standard-library SMTP transport."""

from __future__ import annotations

import asyncio
from email.message import EmailMessage
from email.utils import formatdate
from datetime import datetime
from typing import Any

from aap_mail_transport import MailOutcome, MailReason, SMTPConfig, send_message
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.system_settings_service import SystemSettingsService


class SmtpConfigError(Exception):
    pass


async def _load_smtp_config(db: AsyncSession) -> dict[str, Any]:
    keys = ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from"]
    # Host, credentials and sender must come from one saved configuration,
    # including immediately after a different API process changed SMTP.
    out = await SystemSettingsService.get_many(db, keys, bypass_cache=True)
    if not out["smtp_host"] or not out["smtp_port"] or not out["smtp_from"]:
        raise SmtpConfigError("SMTP 未完整配置（host / port / from 必填）")
    return out


def _plain_text_message(
    body: str, *, subject: str, sender: str, recipient: str
) -> EmailMessage:
    msg = EmailMessage()
    msg.set_content(body, subtype="plain", charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    return msg


async def _send(db: AsyncSession, to_address: str, subject: str, body: str) -> None:
    """连 SMTP 发一封纯文本邮件。失败抛 SmtpConfigError。"""
    cfg = await _load_smtp_config(db)
    msg = _plain_text_message(
        body, subject=subject, sender=cfg["smtp_from"], recipient=to_address
    )
    await asyncio.to_thread(_send_message, cfg, msg)


def _send_message(cfg: dict[str, Any], msg: EmailMessage) -> None:
    """Compatibility adapter for existing API callers and their error contract."""
    try:
        port = int(cfg.get("smtp_port") or 0)
    except (TypeError, ValueError):
        port = 0
    result = send_message(
        SMTPConfig(
            host=str(cfg.get("smtp_host") or ""),
            port=port,
            user=str(cfg.get("smtp_user") or ""),
            password=str(cfg.get("smtp_password") or ""),
            sender=str(cfg.get("smtp_from") or msg.get("From") or ""),
        ),
        msg,
    )
    if result.outcome == MailOutcome.DELIVERED:
        return
    if result.reason == MailReason.NOT_CONFIGURED:
        raise SmtpConfigError("SMTP 未完整配置（host / port / from 必填）")
    if result.reason == MailReason.MESSAGE_INVALID:
        raise SmtpConfigError("SMTP 邮件内容无效")
    raise SmtpConfigError(f"SMTP 发送失败: {result.reason.value}")


async def send_invitation_email(
    db: AsyncSession,
    to_address: str,
    invite_url: str,
    *,
    project_name: str | None = None,
    role: str | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Send an already-created invitation without changing its token."""

    target = f"项目「{project_name}」" if project_name else "AI 标注平台"
    role_line = f"项目角色：{role}\n" if role else ""
    expiry_line = (
        f"链接有效期至：{expires_at.astimezone().isoformat()}\n"
        if expires_at is not None
        else ""
    )
    body = (
        f"你收到一份加入 {target} 的邀请。\n\n"
        f"{role_line}{expiry_line}"
        "请点击以下链接接受邀请：\n"
        f"{invite_url}\n\n"
        "如果你不认识邀请人，请忽略此邮件。\n"
    )
    await _send(db, to_address, "[AI 标注平台] 你有一份新的邀请", body)


async def send_test_email(db: AsyncSession, to_address: str) -> dict[str, Any]:
    """连 SMTP 发一封测试邮件。返回诊断字典；失败抛 SmtpConfigError。"""
    cfg = await _load_smtp_config(db)
    msg = _plain_text_message(
        "这是一封来自 AI 标注平台的 SMTP 配置测试邮件。\n"
        "如收到此邮件，说明 SMTP 设置正确。\n",
        subject="[AI 标注平台] SMTP 测试邮件",
        sender=cfg["smtp_from"],
        recipient=to_address,
    )

    await asyncio.to_thread(_send_message, cfg, msg)

    return {
        "to": to_address,
        "from": cfg["smtp_from"],
        "host": cfg["smtp_host"],
        "port": int(cfg["smtp_port"]),
    }
