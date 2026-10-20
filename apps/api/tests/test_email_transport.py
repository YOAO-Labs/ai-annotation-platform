"""The API keeps its existing mail helpers on the shared SMTP transport."""

from email.message import EmailMessage

import pytest

from aap_mail_transport import MailDeliveryResult, MailOutcome, MailReason, SMTPConfig
from app.services.email import SmtpConfigError, _send_message


def test_api_adapter_passes_one_snapshot_and_raises_only_safe_transport_status(
    monkeypatch,
):
    observed = {}

    def fail(config, message):
        observed["config"] = config
        observed["message"] = message
        return MailDeliveryResult(MailOutcome.RETRY, MailReason.CONNECT_FAILED)

    monkeypatch.setattr("app.services.email.send_message", fail)
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message.set_content("test")

    with pytest.raises(SmtpConfigError, match="connect_failed") as caught:
        _send_message(
            {
                "smtp_host": "mail.test",
                "smtp_port": 587,
                "smtp_user": "user-secret",
                "smtp_password": "password-secret",
                "smtp_from": "sender@example.test",
            },
            message,
        )

    assert observed["config"] == SMTPConfig(
        host="mail.test",
        port=587,
        user="user-secret",
        password="password-secret",
        sender="sender@example.test",
    )
    assert observed["message"] is message
    assert "user-secret" not in str(caught.value)
    assert "password-secret" not in str(caught.value)


def test_api_adapter_preserves_missing_configuration_error():
    with pytest.raises(SmtpConfigError, match="SMTP 未完整配置"):
        _send_message(
            {
                "smtp_host": "",
                "smtp_port": 587,
                "smtp_user": "",
                "smtp_password": "",
                "smtp_from": "",
            },
            EmailMessage(),
        )
