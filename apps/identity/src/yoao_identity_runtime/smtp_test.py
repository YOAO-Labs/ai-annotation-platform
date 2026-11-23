"""Deliver one fixed SMTP diagnostic without exposing relay credentials to the API."""

from __future__ import annotations

import asyncio
from email.message import EmailMessage
from email.utils import formatdate

from yoao_identity.contract import IdentityUnavailable
from yoao_mail_transport import MailReason, SMTPConfig, send_message


async def run_smtp_test_once(data, policy) -> bool:
    attempt = await data.claim_smtp_test()
    if attempt is None:
        return False
    try:
        cfg = await policy()
    except IdentityUnavailable:
        await data.finish_smtp_test(attempt, MailReason.TRANSPORT_FAILED)
        return True

    if not cfg.smtp_host or not cfg.smtp_port or not cfg.smtp_from:
        await data.finish_smtp_test(attempt, MailReason.NOT_CONFIGURED)
        return True
    try:
        msg = EmailMessage()
        msg.set_content(
            "这是一封 YOAO 的 SMTP 测试邮件。\n"
            "收到这封邮件，表示本次测试已送达你的邮箱。\n"
        )
        msg["Subject"] = "[YOAO] SMTP 测试邮件"
        msg["From"] = cfg.smtp_from
        msg["To"] = attempt.recipient
        msg["Date"] = formatdate(localtime=True)
    except (TypeError, ValueError):
        await data.finish_smtp_test(attempt, MailReason.MESSAGE_INVALID)
        return True

    result = await asyncio.to_thread(
        send_message,
        SMTPConfig(
            host=cfg.smtp_host,
            port=cfg.smtp_port,
            user=cfg.smtp_user,
            password=cfg.smtp_password,
            sender=cfg.smtp_from,
        ),
        msg,
    )
    await data.finish_smtp_test(
        attempt,
        result.reason,
        sender=cfg.smtp_from,
        host=cfg.smtp_host,
        port=cfg.smtp_port,
    )
    return True
