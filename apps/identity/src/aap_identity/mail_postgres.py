"""Narrow PostgreSQL function clients for mail delivery and SMTP settings."""

from __future__ import annotations

import asyncio
import json

from .contract import IdentityUnavailable
from .mail_contract import (
    MAIL_DELIVERY_POLICY_FIELDS,
    MAIL_NATIVE_PROTOCOL,
    MAIL_POLICY,
    MailAcceptedReply,
    MailDeliveryPolicy,
    MailFinishRequest,
    MailLease,
    MailPolicyReply,
    MailRenewRequest,
    decode_mail_accepted_reply,
    decode_mail_claim_reply,
    decode_mail_delivery_policy,
    decode_mail_policy,
    native_command_json,
)


class MailPostgresData:
    """Calls only the four granted mail capability functions."""

    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def _open(self):
        import asyncpg

        return await asyncpg.connect(
            self.dsn,
            timeout=self.connect_timeout,
            command_timeout=self.command_timeout,
            server_settings={"application_name": "aap-identity-credential-mail"},
        )

    async def _close(self, connection) -> None:
        if connection is None:
            return
        try:
            await asyncio.shield(connection.close(timeout=2))
        except Exception:
            if not asyncio.current_task().cancelling():
                raise IdentityUnavailable() from None

    async def _execute(self, sql: str, command=None):
        import asyncpg

        connection = None
        try:
            connection = await self._open()
            raw = (
                await connection.fetchval(sql, native_command_json(command))
                if command is not None
                else await connection.fetchval(sql)
            )
            return json.loads(raw) if isinstance(raw, str) else raw
        except IdentityUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            raise IdentityUnavailable() from None
        finally:
            await self._close(connection)

    async def mail_policy(self) -> MailPolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await self._open()
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_recovery_v1.mail_policy()"
                )
                required = [
                    "aap_identity_recovery_v1.mail_policy()",
                    "aap_identity_recovery_v1.claim_mail(jsonb)",
                    "aap_identity_recovery_v1.renew_mail(jsonb)",
                    "aap_identity_recovery_v1.finish_mail(jsonb)",
                ]
                allowed = await connection.fetchval(
                    "SELECT bool_and(has_function_privilege(current_user, capability, "
                    "'EXECUTE')) "
                    "FROM unnest($1::text[]) AS required(capability)",
                    required,
                )
                if allowed is not True:
                    raise IdentityUnavailable()
                policy = decode_mail_policy(raw)
                if policy.policy != MAIL_POLICY:
                    raise IdentityUnavailable()
                return policy
        except IdentityUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise IdentityUnavailable() from None
        finally:
            await self._close(connection)

    async def claim_mail(
        self,
        worker_id,
        *,
        lease_seconds: int = 60,
        delivery_id=None,
    ) -> MailLease | None:
        if type(lease_seconds) is not int:
            raise IdentityUnavailable()
        lease_seconds = max(1, min(300, lease_seconds))
        command = {
            "schema": MAIL_NATIVE_PROTOCOL,
            "worker_id": str(worker_id),
            "lease_seconds": lease_seconds,
            "delivery_id": str(delivery_id) if delivery_id is not None else None,
        }
        from .mail_contract import decode_mail_claim_request

        request = decode_mail_claim_request(command)
        reply = decode_mail_claim_reply(
            await self._execute(
                "SELECT aap_identity_recovery_v1.claim_mail($1::jsonb)", request
            )
        )
        delivery = reply.data.delivery
        if delivery is not None and delivery.worker_id != request.worker_id:
            raise IdentityUnavailable()
        return delivery

    async def renew_mail(self, lease: MailLease) -> bool:
        request = MailRenewRequest(
            schema=MAIL_NATIVE_PROTOCOL,
            delivery_id=lease.id,
            worker_id=lease.worker_id,
            lease_token=lease.lease_token,
        )
        reply = decode_mail_accepted_reply(
            await self._execute(
                "SELECT aap_identity_recovery_v1.renew_mail($1::jsonb)", request
            )
        )
        return reply.data.accepted

    async def finish_mail(
        self,
        lease: MailLease,
        *,
        outcome: str,
        reason: str,
        retry_delay_seconds: int | None,
    ) -> bool:
        try:
            request = MailFinishRequest(
                schema=MAIL_NATIVE_PROTOCOL,
                delivery_id=lease.id,
                worker_id=lease.worker_id,
                lease_token=lease.lease_token,
                outcome=outcome,
                reason=reason,
                retry_delay_seconds=retry_delay_seconds,
            )
        except (ValueError, TypeError):
            raise IdentityUnavailable() from None
        reply: MailAcceptedReply = decode_mail_accepted_reply(
            await self._execute(
                "SELECT aap_identity_recovery_v1.finish_mail($1::jsonb)", request
            )
        )
        return reply.data.accepted


class MailSettingsPostgresData:
    """Reads only the Settings-owned mail delivery policy function."""

    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def mail_delivery_policy(
        self, fallbacks: dict[str, object]
    ) -> MailDeliveryPolicy:
        import asyncpg

        if not isinstance(fallbacks, dict) or set(fallbacks) != set(
            MAIL_DELIVERY_POLICY_FIELDS
        ):
            raise IdentityUnavailable()
        try:
            encoded = json.dumps(
                fallbacks,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        except (TypeError, ValueError, UnicodeError):
            raise IdentityUnavailable() from None

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-mail-settings"},
            )
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_settings_v1.mail_delivery_policy($1::jsonb)", encoded
                )
                allowed = await connection.fetchval(
                    "SELECT has_function_privilege(current_user, "
                    "'aap_settings_v1.mail_delivery_policy(jsonb)', 'EXECUTE')"
                )
                if allowed is not True:
                    raise IdentityUnavailable()
                result = decode_mail_delivery_policy(raw)
                if set(result.model_dump(mode="python")) != set(
                    MAIL_DELIVERY_POLICY_FIELDS
                ):
                    raise IdentityUnavailable()
                return result
        except IdentityUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            UnicodeError,
        ):
            raise IdentityUnavailable() from None
        finally:
            if connection is not None:
                try:
                    await asyncio.shield(connection.close(timeout=2))
                except Exception:
                    if not asyncio.current_task().cancelling():
                        raise IdentityUnavailable() from None
