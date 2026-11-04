"""Small standard-library SMTP transport shared by AAP modules."""

from .transport import (
    MailDeliveryResult,
    MailOutcome,
    MailReason,
    SMTPConfig,
    send_message,
)

__all__ = [
    "MailDeliveryResult",
    "MailOutcome",
    "MailReason",
    "SMTPConfig",
    "send_message",
]
