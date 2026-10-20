"""Independent durable credential-mail sender and its small health surface."""

from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from urllib.parse import urlencode, urlsplit, urlunsplit

from aap_identity.contract import IdentityUnavailable
from aap_identity.mail_contract import (
    MAIL_OUTCOMES,
    MAIL_REASONS,
    MailDeliveryPolicy,
    MailLease,
    decode_mail_delivery_policy,
)
from aap_identity.mail_v2_contract import InvitationMailV2Lease, MailV2Lease
from aap_identity.mail_postgres import MailPostgresData
from pydantic import SecretStr

from .mail_config import MailWorkerSettings

_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_RETRYABLE = frozenset({"retry", "unknown", "invalid_key"})
MailWorkItem = MailLease | MailV2Lease


class MailExecutor:
    """Claim one committed intent and send it while renewing its fencing lease."""

    def __init__(
        self,
        data,
        policy,
        key: str | SecretStr,
        *,
        worker_id: uuid.UUID | None = None,
        lease_seconds: int = 60,
        retry_delay_seconds: int = 30,
        transport=None,
    ):
        if worker_id is not None and type(worker_id) is not uuid.UUID:
            raise TypeError("worker_id must be a UUID")
        if type(lease_seconds) is not int or type(retry_delay_seconds) is not int:
            raise TypeError("Mail lease and retry delay must be integers")
        self.data = data
        self.policy = policy
        self._key = key if isinstance(key, SecretStr) else SecretStr(key)
        self.worker_id = worker_id or uuid.uuid4()
        self.lease_seconds = max(1, min(300, lease_seconds))
        self.retry_delay_seconds = max(1, min(300, retry_delay_seconds))
        self.transport = transport

    async def _read_policy(self) -> MailDeliveryPolicy:
        value = self.policy
        if callable(value):
            value = value()
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, MailDeliveryPolicy):
            return value
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json", by_alias=True)
        return decode_mail_delivery_policy(value)

    def _key_value(self) -> str:
        return self._key.get_secret_value()

    def _decode_token(self, lease: MailWorkItem) -> str | None:
        key = self._key_value()
        try:
            from aap_identity.recovery_crypto import (
                decrypt_mail_token,
                mail_encryption_id,
                token_hash,
            )

            if mail_encryption_id(key) != lease.encryption_id:
                return None
            token = decrypt_mail_token(
                lease.token_ciphertext,
                key,
                lease.encryption_id,
            )
            if not isinstance(token, str):
                return None
            if lease.purpose == "invitation":
                if not 1 <= len(token) <= 64:
                    return None
            elif _TOKEN.fullmatch(token) is None:
                return None
            if token_hash(token) != lease.token_hash:
                return None
            return token
        except Exception:
            # Ciphertext, key and token material are secrets; callers only receive
            # the closed `derivation_unavailable` outcome.
            return None

    @staticmethod
    def _link(lease: MailWorkItem, token: str) -> str:
        if any(character in lease.frontend_base_url for character in "\r\n\x00"):
            raise ValueError("Invalid frontend URL")
        parts = urlsplit(lease.frontend_base_url)
        if parts.username or parts.password:
            raise ValueError("Invalid frontend URL")
        endpoint = {
            "password_reset": "reset-password",
            "email_verification": "verify-email",
            "invitation": "register",
        }[lease.purpose]
        path = f"{parts.path.rstrip('/')}/{endpoint}"
        query = "&".join(
            part for part in (parts.query, urlencode({"token": token})) if part
        )
        return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))

    @staticmethod
    def _message(lease: MailWorkItem, token: str, sender: str) -> EmailMessage:
        if any(character in lease.email for character in "\r\n\x00"):
            raise ValueError("Invalid recipient")
        url = MailExecutor._link(lease, token)
        message = EmailMessage()
        message["From"] = sender
        message["To"] = lease.email
        message["Message-ID"] = f"<aap.{lease.id}.{lease.purpose}@aap.invalid>"
        if lease.purpose == "password_reset":
            message["Subject"] = "[AI 标注平台] 重置你的密码"
            message.set_content(
                "你收到这封邮件，是因为有人请求重置你的 AI 标注平台密码。\n\n"
                "请在该链接失效前点击以下链接设置新密码：\n"
                f"{url}\n\n"
                "如果你没有发起此请求，请忽略此邮件。你的密码不会因此改变。\n"
            )
        elif lease.purpose == "email_verification":
            message["Subject"] = "[AI 标注平台] 验证你的邮箱"
            message.set_content(
                "欢迎注册 AI 标注平台！\n\n"
                "请点击以下链接验证你的邮箱（24 小时内有效）：\n"
                f"{url}\n\n"
                "如果你没有注册本平台，请忽略此邮件。\n"
            )
        else:
            assert isinstance(lease, InvitationMailV2Lease)
            target = (
                f"项目「{lease.project_name}」" if lease.project_name else "AI 标注平台"
            )
            role_line = (
                f"项目角色：{lease.project_member_role}\n"
                if lease.project_member_role
                else ""
            )
            expiry_line = f"链接有效期至：{lease.expires_at.astimezone().isoformat()}\n"
            message["Subject"] = "[AI 标注平台] 你有一份新的邀请"
            message.set_content(
                f"你收到一份加入 {target} 的邀请。\n\n"
                f"{role_line}{expiry_line}"
                "请点击以下链接接受邀请：\n"
                f"{url}\n\n"
                "如果你不认识邀请人，请忽略此邮件。\n"
            )
        return message

    @staticmethod
    def _configured(policy: MailDeliveryPolicy) -> bool:
        return policy.smtp_port > 0 and bool(
            policy.smtp_host.strip() and policy.smtp_from.strip()
        )

    def _send_sync(self, policy: MailDeliveryPolicy, message: EmailMessage):
        if self.transport is None:
            from aap_mail_transport import SMTPConfig, send_message

            sender = send_message
        else:
            sender = getattr(self.transport, "send_message", self.transport)
            if not callable(sender):
                raise TypeError("Invalid mail transport")
            from aap_mail_transport import SMTPConfig

        config = SMTPConfig(
            host=policy.smtp_host,
            port=policy.smtp_port,
            user=policy.smtp_user,
            password=policy.smtp_password,
            sender=policy.smtp_from,
        )
        return sender(config, message)

    @staticmethod
    def _result(value) -> tuple[str, str]:
        try:
            outcome = getattr(value, "outcome")
            reason = getattr(value, "reason")
            outcome = getattr(outcome, "value", outcome)
            reason = getattr(reason, "value", reason)
            if outcome not in MAIL_OUTCOMES or reason not in MAIL_REASONS:
                raise ValueError()
            if outcome == "delivered" and reason != "sent":
                raise ValueError()
            if outcome == "invalid_key" and reason != "derivation_unavailable":
                raise ValueError()
            if outcome != "delivered" and reason == "sent":
                raise ValueError()
            if outcome != "invalid_key" and reason == "derivation_unavailable":
                raise ValueError()
            return outcome, reason
        except (AttributeError, TypeError, ValueError):
            return "unknown", "transport_failed"

    async def _renew(self, lease: MailWorkItem, timeout: float) -> bool:
        try:
            async with asyncio.timeout(timeout):
                return await self.data.renew_mail(lease)
        except (IdentityUnavailable, TimeoutError):
            return False

    async def _send_with_renewal(
        self, lease: MailWorkItem, policy: MailDeliveryPolicy, message: EmailMessage
    ) -> tuple[tuple[str, str] | None, bool]:
        remaining = (lease.lease_expires_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return None, True
        interval = max(
            0.05,
            min(30.0, self.lease_seconds / 3, remaining / 3),
        )
        initial_renew_timeout = max(0.05, min(10.0, remaining - 0.1))
        if not await self._renew(lease, initial_renew_timeout):
            return None, True

        send_task = asyncio.create_task(
            asyncio.to_thread(self._send_sync, policy, message)
        )
        while True:
            done, _ = await asyncio.wait({send_task}, timeout=interval)
            if send_task in done:
                try:
                    return self._result(send_task.result()), False
                except Exception:
                    return ("unknown", "transport_failed"), False
            token_remaining = (lease.expires_at - datetime.now(UTC)).total_seconds()
            renewal_window = min(float(self.lease_seconds), token_remaining)
            renewal_timeout = max(0.05, min(10.0, renewal_window - 0.1))
            if token_remaining <= 0 or not await self._renew(lease, renewal_timeout):
                # A blocking SMTP call cannot be safely cancelled mid-DATA. Let it
                # finish, but abandon completion so the database fence owns truth.
                try:
                    await asyncio.shield(send_task)
                except Exception:
                    pass
                return None, True

    async def _finish(
        self,
        lease: MailWorkItem,
        outcome: str,
        reason: str,
    ) -> bool:
        retry = self.retry_delay_seconds if outcome in _RETRYABLE else None
        return await self.data.finish_mail(
            lease,
            outcome=outcome,
            reason=reason,
            retry_delay_seconds=retry,
        )

    @staticmethod
    def _summary(
        state: str,
        lease: MailWorkItem | None = None,
        *,
        outcome=None,
        reason=None,
        accepted=None,
    ):
        summary = {"state": state}
        if lease is not None:
            summary["delivery_id"] = str(lease.id)
        if outcome is not None:
            summary["outcome"] = outcome
        if reason is not None:
            summary["reason"] = reason
        if accepted is not None:
            summary["accepted"] = accepted
        return summary

    async def run_once(self, delivery_id: uuid.UUID | None = None) -> dict[str, object]:
        if delivery_id is not None and type(delivery_id) is not uuid.UUID:
            raise TypeError("delivery_id must be a UUID")
        policy = await self._read_policy()
        lease = await self.data.claim_mail(
            self.worker_id,
            lease_seconds=self.lease_seconds,
            delivery_id=delivery_id,
        )
        if lease is None:
            return self._summary("idle")
        if lease.worker_id != self.worker_id or (
            delivery_id is not None and lease.id != delivery_id
        ):
            raise IdentityUnavailable()

        token = self._decode_token(lease)
        if token is None:
            outcome, reason = "invalid_key", "derivation_unavailable"
            accepted = await self._finish(lease, outcome, reason)
            return self._summary(
                "finished" if accepted else "stale",
                lease,
                outcome=outcome,
                reason=reason,
                accepted=accepted,
            )

        if not self._configured(policy):
            outcome, reason = "rejected", "not_configured"
            accepted = await self._finish(lease, outcome, reason)
            return self._summary(
                "finished" if accepted else "stale",
                lease,
                outcome=outcome,
                reason=reason,
                accepted=accepted,
            )

        try:
            message = self._message(lease, token, policy.smtp_from)
        except (TypeError, ValueError, UnicodeError):
            outcome, reason = "rejected", "message_invalid"
            accepted = await self._finish(lease, outcome, reason)
            return self._summary(
                "finished" if accepted else "stale",
                lease,
                outcome=outcome,
                reason=reason,
                accepted=accepted,
            )
        finally:
            # Drop the raw bearer token reference as soon as message construction
            # finishes; it remains only in the transient message sent to SMTP.
            token = ""

        result, lease_lost = await self._send_with_renewal(lease, policy, message)
        if lease_lost or result is None:
            return self._summary("stale", lease)

        outcome, reason = result
        try:
            accepted = await self._finish(lease, outcome, reason)
        except IdentityUnavailable:
            return self._summary("unavailable", lease, outcome=outcome, reason=reason)
        return self._summary(
            "finished" if accepted else "stale",
            lease,
            outcome=outcome,
            reason=reason,
            accepted=accepted,
        )


def _fallbacks(settings: MailWorkerSettings) -> dict[str, object]:
    return {
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_user": settings.smtp_user,
        "smtp_password": settings.smtp_password.get_secret_value(),
        "smtp_from": settings.smtp_from,
    }


def create_app(
    settings: MailWorkerSettings | None = None,
    *,
    data: MailPostgresData | None = None,
    settings_data=None,
    executor: MailExecutor | None = None,
):
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    settings = settings or MailWorkerSettings()
    data = data or MailPostgresData(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.connect_timeout_seconds,
        command_timeout=settings.command_timeout_seconds,
    )
    if settings_data is None:
        from aap_identity.mail_postgres import MailSettingsPostgresData

        settings_data = MailSettingsPostgresData(
            settings.settings_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )

    async def policy():
        return await settings_data.mail_delivery_policy(_fallbacks(settings))

    executor = executor or MailExecutor(
        data,
        policy,
        settings.encryption_key,
        lease_seconds=settings.lease_seconds,
        retry_delay_seconds=settings.retry_delay_seconds,
    )
    stop = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_app):
        stop.clear()
        task = asyncio.create_task(poll_queue(), name="identity-credential-mail-poll")
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def poll_queue() -> None:
        while not stop.is_set():
            try:
                result = await executor.run_once()
                if result.get("state") in {"idle", "stale", "unavailable"}:
                    await asyncio.sleep(settings.poll_interval_seconds)
            except IdentityUnavailable:
                await asyncio.sleep(settings.poll_interval_seconds)
            except Exception:
                # Driver and SMTP exceptions can contain connection details or
                # credentials; the durable queue is retried without logging them.
                await asyncio.sleep(settings.poll_interval_seconds)

    app = FastAPI(
        title="Identity credential mail worker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            from aap_identity.recovery_crypto import mail_encryption_id

            async with asyncio.timeout(settings.command_timeout_seconds + 1):
                native = await data.mail_policy()
                await policy()
                if native.policy != "aap-identity-recovery-mail/v1":
                    raise IdentityUnavailable()
                mail_encryption_id(executor._key_value())
        except (IdentityUnavailable, TimeoutError, ValueError, TypeError):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    return app


def main() -> None:
    import uvicorn

    try:
        settings = MailWorkerSettings()
        app = create_app(settings)
    except Exception:
        raise SystemExit("Credential mail worker configuration invalid") from None
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
