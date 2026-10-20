"""SMTP classification and TLS ordering using only mocks and loopback SMTP."""

from __future__ import annotations

import socketserver
import smtplib
import threading
from contextlib import contextmanager
from email.message import EmailMessage
from unittest import TestCase
from unittest.mock import patch

from aap_mail_transport import (
    MailDeliveryResult,
    MailOutcome,
    MailReason,
    SMTPConfig,
    send_message,
)


def _config(port: int = 2525) -> SMTPConfig:
    return SMTPConfig(
        host="mail.test",
        port=port,
        user="user-secret",
        password="password-secret",
        sender="sender@example.test",
    )


def _message() -> EmailMessage:
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "recipient@example.test"
    message["Subject"] = "Local test"
    message.set_content("A local message")
    return message


class _SMTPDouble:
    def __init__(self, *, advertises_tls: bool = True):
        self.advertises_tls = advertises_tls
        self.calls: list[str] = []
        self.starttls_error: Exception | None = None
        self.login_error: Exception | None = None
        self.send_error: Exception | None = None
        self.refused: dict[str, tuple[int, bytes]] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.calls.append("exit")

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, extension: str) -> bool:
        self.calls.append(f"has_extn:{extension}")
        return self.advertises_tls

    def starttls(self, **_kwargs):
        self.calls.append("starttls")
        if self.starttls_error:
            raise self.starttls_error

    def login(self, _user: str, _password: str):
        self.calls.append("login")
        if self.login_error:
            raise self.login_error

    def send_message(self, _message, *, from_addr):
        self.calls.append(f"send:{from_addr}")
        if self.send_error:
            raise self.send_error
        return self.refused


class SMTPTransportTests(TestCase):
    def test_secure_submission_order(self):
        cases = (
            (587, False, False, True),
            (25, True, False, True),
            (465, False, True, False),
        )
        for port, advertises_tls, uses_ssl, expected_tls in cases:
            with self.subTest(port=port):
                smtp = _SMTPDouble(advertises_tls=advertises_tls)
                clients = []

                def plain(*_args, **_kwargs):
                    clients.append("plain")
                    return smtp

                def implicit_tls(*_args, **_kwargs):
                    clients.append("ssl")
                    return smtp

                with (
                    patch("aap_mail_transport.transport.smtplib.SMTP", plain),
                    patch(
                        "aap_mail_transport.transport.smtplib.SMTP_SSL", implicit_tls
                    ),
                ):
                    result = send_message(_config(port), _message())

                self.assertEqual(
                    result, MailDeliveryResult(MailOutcome.DELIVERED, MailReason.SENT)
                )
                self.assertEqual(clients, ["ssl" if uses_ssl else "plain"])
                self.assertEqual(smtp.calls.count("starttls"), int(expected_tls))
                if expected_tls:
                    self.assertLess(
                        smtp.calls.index("starttls"), smtp.calls.index("login")
                    )
                    send_index = next(
                        i
                        for i, call in enumerate(smtp.calls)
                        if call.startswith("send:")
                    )
                    self.assertLess(smtp.calls.index("starttls"), send_index)
                    self.assertEqual(smtp.calls.count("ehlo"), 2)
                else:
                    self.assertNotIn("has_extn:starttls", smtp.calls)
                    self.assertEqual(smtp.calls.count("ehlo"), 1)

    def test_auth_status_codes_are_classified_without_diagnostics(self):
        for code, expected_outcome in (
            (454, MailOutcome.RETRY),
            (535, MailOutcome.REJECTED),
        ):
            with self.subTest(code=code):
                smtp = _SMTPDouble()
                smtp.login_error = smtplib.SMTPAuthenticationError(
                    code, b"auth details"
                )
                with patch(
                    "aap_mail_transport.transport.smtplib.SMTP", return_value=smtp
                ):
                    result = send_message(_config(), _message())

                self.assertEqual(
                    (result.outcome, result.reason),
                    (expected_outcome, MailReason.AUTH_FAILED),
                )
                self.assertIn("login", smtp.calls)
                self.assertFalse(any(call.startswith("send:") for call in smtp.calls))
                self.assertNotIn("auth details", repr(result))
                self.assertNotIn("password-secret", repr(result))

    def test_tls_failure_never_authenticates_or_sends(self):
        smtp = _SMTPDouble()
        smtp.starttls_error = smtplib.SMTPResponseException(454, b"server-secret")
        with patch("aap_mail_transport.transport.smtplib.SMTP", return_value=smtp):
            result = send_message(_config(587), _message())

        self.assertEqual(
            (result.outcome, result.reason), (MailOutcome.RETRY, MailReason.TLS_FAILED)
        )
        self.assertNotIn("login", smtp.calls)
        self.assertFalse(any(call.startswith("send:") for call in smtp.calls))
        self.assertNotIn("server-secret", repr(result))

    def test_plain_relay_with_credentials_remains_supported_when_tls_is_not_advertised(
        self,
    ):
        smtp = _SMTPDouble(advertises_tls=False)
        with patch("aap_mail_transport.transport.smtplib.SMTP", return_value=smtp):
            result = send_message(_config(2525), _message())

        self.assertEqual(
            (result.outcome, result.reason),
            (MailOutcome.DELIVERED, MailReason.SENT),
        )
        self.assertNotIn("starttls", smtp.calls)
        self.assertLess(
            smtp.calls.index("login"), smtp.calls.index("send:sender@example.test")
        )
        self.assertNotIn("password-secret", repr(result))

    def test_known_sender_and_data_rejections_keep_safe_reason_codes(self):
        cases = (
            (
                smtplib.SMTPSenderRefused(
                    550, b"private sender response", "sender-secret"
                ),
                MailOutcome.REJECTED,
                MailReason.SENDER_REJECTED,
            ),
            (
                smtplib.SMTPDataError(451, b"private temporary response"),
                MailOutcome.RETRY,
                MailReason.DATA_REJECTED,
            ),
            (
                smtplib.SMTPDataError(554, b"private permanent response"),
                MailOutcome.REJECTED,
                MailReason.DATA_REJECTED,
            ),
        )
        for error, expected_outcome, expected_reason in cases:
            with self.subTest(error=type(error).__name__, expected=expected_outcome):
                smtp = _SMTPDouble()
                smtp.send_error = error
                with patch(
                    "aap_mail_transport.transport.smtplib.SMTP", return_value=smtp
                ):
                    result = send_message(_config(), _message())
                self.assertEqual(
                    (result.outcome, result.reason), (expected_outcome, expected_reason)
                )
                self.assertNotIn("private", repr(result))
                self.assertNotIn("sender-secret", repr(result))

    def test_connect_failure_is_retryable_and_sanitized(self):
        with patch(
            "aap_mail_transport.transport.smtplib.SMTP",
            side_effect=OSError("server-secret recipient-secret"),
        ):
            result = send_message(_config(), _message())

        self.assertEqual(
            (result.outcome, result.reason),
            (MailOutcome.RETRY, MailReason.CONNECT_FAILED),
        )
        self.assertNotIn("server-secret", repr(result))
        self.assertNotIn("recipient-secret", repr(result))

    def test_missing_configuration_and_invalid_message_do_not_connect(self):
        with patch(
            "aap_mail_transport.transport.smtplib.SMTP",
            side_effect=AssertionError("transport should not connect"),
        ):
            unconfigured = SMTPConfig("", 2525, "", "", "sender@example.test")
            invalid = EmailMessage()
            self.assertEqual(
                send_message(unconfigured, _message()),
                MailDeliveryResult(MailOutcome.REJECTED, MailReason.NOT_CONFIGURED),
            )
            self.assertEqual(
                send_message(_config(), invalid),
                MailDeliveryResult(MailOutcome.REJECTED, MailReason.MESSAGE_INVALID),
            )

    def test_config_repr_hides_smtp_credentials(self):
        config_repr = repr(_config())
        self.assertNotIn("password-secret", config_repr)
        self.assertNotIn("user-secret", config_repr)

    def test_disconnect_after_send_entry_is_unknown_and_sanitized(self):
        smtp = _SMTPDouble()
        smtp.send_error = smtplib.SMTPServerDisconnected(
            "recipient-and-password-secret"
        )
        with patch("aap_mail_transport.transport.smtplib.SMTP", return_value=smtp):
            result = send_message(_config(), _message())

        self.assertEqual(
            (result.outcome, result.reason),
            (MailOutcome.UNKNOWN, MailReason.TRANSPORT_FAILED),
        )
        self.assertNotIn("recipient-and-password-secret", repr(result))

    def test_partial_recipient_acceptance_is_unknown_without_address_details(self):
        smtp = _SMTPDouble()
        smtp.refused = {"recipient-secret": (550, b"private refusal")}
        with patch("aap_mail_transport.transport.smtplib.SMTP", return_value=smtp):
            result = send_message(_config(), _message())

        self.assertEqual(
            (result.outcome, result.reason),
            (MailOutcome.UNKNOWN, MailReason.RECIPIENT_REJECTED),
        )
        self.assertNotIn("recipient-secret", repr(result))
        self.assertNotIn("private refusal", repr(result))

    def test_loopback_smtp_success_and_known_recipient_rejections(self):
        cases = (
            (250, MailOutcome.DELIVERED, MailReason.SENT),
            (450, MailOutcome.RETRY, MailReason.RECIPIENT_REJECTED),
            (550, MailOutcome.REJECTED, MailReason.RECIPIENT_REJECTED),
        )
        for recipient_code, expected_outcome, expected_reason in cases:
            with self.subTest(recipient_code=recipient_code):
                with _loopback_smtp(recipient_code=recipient_code) as (
                    host,
                    port,
                    server,
                ):
                    config = SMTPConfig(host, port, "", "", "sender@example.test")
                    result = send_message(config, _message())

                self.assertEqual(
                    (result.outcome, result.reason), (expected_outcome, expected_reason)
                )
                if recipient_code == 250:
                    self.assertEqual(len(server.messages), 1)
                    self.assertIn("Subject: Local test", server.messages[0])
                else:
                    self.assertEqual(server.messages, [])


class _LoopbackServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, *, recipient_code: int):
        self.recipient_code = recipient_code
        self.messages: list[str] = []
        super().__init__(address, handler)


class _LoopbackHandler(socketserver.StreamRequestHandler):
    def _reply(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode("ascii"))

    def handle(self) -> None:
        self._reply("220 loopback.test ESMTP")
        while line := self.rfile.readline():
            command = line.decode("ascii", "replace").rstrip("\r\n")
            verb = command.partition(" ")[0].upper()
            if verb in {"EHLO", "HELO"}:
                self._reply("250-loopback.test")
                self._reply("250 SIZE 1048576")
            elif verb == "MAIL":
                self._reply("250 sender accepted")
            elif verb == "RCPT":
                self._reply(f"{self.server.recipient_code} recipient response")
            elif verb == "DATA":
                self._reply("354 end with dot")
                chunks: list[str] = []
                while data_line := self.rfile.readline():
                    decoded = data_line.decode("utf-8", "replace").rstrip("\r\n")
                    if decoded == ".":
                        break
                    chunks.append(decoded[1:] if decoded.startswith("..") else decoded)
                self.server.messages.append("\n".join(chunks))
                self._reply("250 queued")
            elif verb == "QUIT":
                self._reply("221 bye")
                return
            else:
                self._reply("250 ok")


@contextmanager
def _loopback_smtp(*, recipient_code: int):
    server = _LoopbackServer(
        ("127.0.0.1", 0), _LoopbackHandler, recipient_code=recipient_code
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[0], server.server_address[1], server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
