"""The isolated sender emits one fixed message and settles only safe outcomes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from yoao_mail_transport import MailDeliveryResult, MailOutcome, MailReason, SMTPConfig
from yoao_identity.mail_postgres import SmtpTestAttempt
from yoao_identity_runtime.smtp_test import run_smtp_test_once


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False])
async def test_isolated_diagnostic_sends_only_to_claimed_account(
    monkeypatch, configured
):
    attempt = SmtpTestAttempt(uuid.uuid4(), uuid.uuid4(), "recipient@example.test")
    sender = SimpleNamespace(
        claim_smtp_test=AsyncMock(side_effect=[attempt, None]),
        finish_smtp_test=AsyncMock(return_value=True),
    )
    policy = AsyncMock(
        return_value=SimpleNamespace(
            smtp_host="mail.test" if configured else "",
            smtp_port=587,
            smtp_user="user-secret",
            smtp_password="password-secret",
            smtp_from="sender@example.test",
        )
    )

    def fail(config, message):
        assert config == SMTPConfig(
            host="mail.test",
            port=587,
            user="user-secret",
            password="password-secret",
            sender="sender@example.test",
        )
        assert message["To"] == attempt.recipient
        assert message["Subject"] == "[YOAO] SMTP 测试邮件"
        return MailDeliveryResult(MailOutcome.RETRY, MailReason.CONNECT_FAILED)

    monkeypatch.setattr("yoao_identity_runtime.smtp_test.send_message", fail)
    assert await run_smtp_test_once(sender, policy)
    assert not await run_smtp_test_once(sender, policy)
    policy.assert_awaited_once()
    if configured:
        sender.finish_smtp_test.assert_awaited_once_with(
            attempt,
            MailReason.CONNECT_FAILED,
            sender="sender@example.test",
            host="mail.test",
            port=587,
        )
    else:
        sender.finish_smtp_test.assert_awaited_once_with(
            attempt, MailReason.NOT_CONFIGURED
        )
    assert "password-secret" not in repr(sender.finish_smtp_test.await_args)
