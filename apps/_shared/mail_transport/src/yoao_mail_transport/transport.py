"""SMTP delivery with safe, retry-aware outcomes and no runtime dependencies."""

from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from enum import StrEnum


class MailOutcome(StrEnum):
    DELIVERED = "delivered"
    REJECTED = "rejected"
    RETRY = "retry"
    UNKNOWN = "unknown"
    INVALID_KEY = "invalid_key"


class MailReason(StrEnum):
    SENT = "sent"
    NOT_CONFIGURED = "not_configured"
    MESSAGE_INVALID = "message_invalid"
    TLS_FAILED = "tls_failed"
    AUTH_FAILED = "auth_failed"
    RECIPIENT_REJECTED = "recipient_rejected"
    SENDER_REJECTED = "sender_rejected"
    DATA_REJECTED = "data_rejected"
    CONNECT_FAILED = "connect_failed"
    TRANSPORT_FAILED = "transport_failed"
    DERIVATION_UNAVAILABLE = "derivation_unavailable"


@dataclass(frozen=True, slots=True)
class SMTPConfig:
    host: str
    port: int
    user: str = field(repr=False)
    password: str = field(repr=False)
    sender: str


@dataclass(frozen=True, slots=True)
class MailDeliveryResult:
    outcome: MailOutcome
    reason: MailReason


def send_message(config: SMTPConfig, message: EmailMessage) -> MailDeliveryResult:
    """Send one message and return a closed result without SMTP diagnostics.

    A refused SMTP command is retryable for 4xx and terminal for 5xx. Once SMTP
    submission starts, an unexpected transport failure has an unknown outcome
    because the server may already have accepted the message.
    """

    try:
        configured = _is_configured(config)
    except Exception:
        configured = False
    if not configured:
        return _result(MailOutcome.REJECTED, MailReason.NOT_CONFIGURED)
    try:
        valid_message = _is_message_valid(message)
    except Exception:
        valid_message = False
    if not valid_message:
        return _result(MailOutcome.REJECTED, MailReason.MESSAGE_INVALID)

    phase = "connect"
    try:
        if config.port == 465:
            phase = "tls_connect"
            client = smtplib.SMTP_SSL(
                config.host,
                config.port,
                timeout=15,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=15)

        phase = "hello"
        with client:
            client.ehlo()
            if config.port != 465:
                advertises_starttls = client.has_extn("starttls")
                if config.port == 587 or advertises_starttls:
                    phase = "tls"
                    client.starttls(context=ssl.create_default_context())
                    phase = "hello"
                    client.ehlo()

            if config.user and config.password:
                phase = "auth"
                client.login(config.user, config.password)

            phase = "send"
            refused = client.send_message(message, from_addr=config.sender)
            if refused:
                # SMTP accepted at least one recipient and refused another. A
                # retry is safer than claiming full delivery; it may duplicate
                # delivery to accepted recipients, which mail is allowed to do.
                return _result(MailOutcome.UNKNOWN, MailReason.RECIPIENT_REJECTED)
        return _result(MailOutcome.DELIVERED, MailReason.SENT)
    except smtplib.SMTPRecipientsRefused as exc:
        return _response_result(exc, MailReason.RECIPIENT_REJECTED)
    except smtplib.SMTPSenderRefused as exc:
        return _response_result(exc, MailReason.SENDER_REJECTED)
    except smtplib.SMTPDataError as exc:
        return _response_result(exc, MailReason.DATA_REJECTED)
    except smtplib.SMTPAuthenticationError as exc:
        return _response_result(exc, MailReason.AUTH_FAILED)
    except smtplib.SMTPConnectError as exc:
        return _response_result(exc, MailReason.CONNECT_FAILED)
    except ssl.SSLError:
        if phase == "send":
            return _result(MailOutcome.UNKNOWN, MailReason.TRANSPORT_FAILED)
        return _result(MailOutcome.RETRY, MailReason.TLS_FAILED)
    except (smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError, OSError):
        if phase == "send":
            return _result(MailOutcome.UNKNOWN, MailReason.TRANSPORT_FAILED)
        if phase == "connect":
            return _result(MailOutcome.RETRY, MailReason.CONNECT_FAILED)
        if phase in {"tls", "tls_connect"}:
            return _result(MailOutcome.RETRY, MailReason.TLS_FAILED)
        return _result(MailOutcome.RETRY, MailReason.TRANSPORT_FAILED)
    except smtplib.SMTPException as exc:
        if phase == "send":
            return _result(MailOutcome.UNKNOWN, MailReason.TRANSPORT_FAILED)
        if phase == "tls":
            return _response_result(
                exc, MailReason.TLS_FAILED, fallback=MailOutcome.RETRY
            )
        if phase == "auth":
            return _response_result(exc, MailReason.AUTH_FAILED)
        if phase in {"connect", "tls_connect"}:
            return _response_result(exc, MailReason.CONNECT_FAILED)
        return _result(MailOutcome.RETRY, MailReason.TRANSPORT_FAILED)
    except (TypeError, ValueError):
        # Header serialization and address parsing happen before DATA. They are
        # known message failures even if smtplib raises them from send_message.
        return _result(MailOutcome.REJECTED, MailReason.MESSAGE_INVALID)
    except Exception:
        if phase == "send":
            return _result(MailOutcome.UNKNOWN, MailReason.TRANSPORT_FAILED)
        if phase == "connect":
            return _result(MailOutcome.RETRY, MailReason.CONNECT_FAILED)
        if phase in {"tls", "tls_connect"}:
            return _result(MailOutcome.RETRY, MailReason.TLS_FAILED)
        return _result(MailOutcome.RETRY, MailReason.TRANSPORT_FAILED)


def _is_configured(config: SMTPConfig) -> bool:
    return (
        isinstance(config, SMTPConfig)
        and isinstance(config.host, str)
        and bool(config.host.strip())
        and isinstance(config.port, int)
        and not isinstance(config.port, bool)
        and 1 <= config.port <= 65535
        and isinstance(config.sender, str)
        and bool(config.sender.strip())
    )


def _is_message_valid(message: object) -> bool:
    if not isinstance(message, EmailMessage):
        return False
    if not any(
        value.strip()
        for name in ("To", "Cc", "Bcc")
        for value in message.get_all(name, [])
        if isinstance(value, str)
    ):
        return False
    if not any(
        value.strip() for value in message.get_all("From", []) if isinstance(value, str)
    ):
        return False
    try:
        message.as_bytes()
    except Exception:
        return False
    return True


def _response_result(
    exc: smtplib.SMTPException,
    reason: MailReason,
    *,
    fallback: MailOutcome = MailOutcome.REJECTED,
) -> MailDeliveryResult:
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int) and 400 <= code < 500:
        return _result(MailOutcome.RETRY, reason)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [
            code for code, _message in exc.recipients.values() if isinstance(code, int)
        ]
        if any(400 <= code < 500 for code in codes):
            return _result(MailOutcome.RETRY, reason)
    if isinstance(code, int) and 500 <= code < 600:
        return _result(MailOutcome.REJECTED, reason)
    return _result(fallback, reason)


def _result(outcome: MailOutcome, reason: MailReason) -> MailDeliveryResult:
    return MailDeliveryResult(outcome=outcome, reason=reason)
