"""Portable manager for anonymous registration and one-time recovery commands."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from cryptography.fernet import Fernet

from .contract import IdentityUnavailable
from .password_policy import validate_password_strength
from .recovery_contract import (
    RECOVERY_PROTOCOL,
    ConsumePasswordResetRequest,
    NativeRecoveryPrepareCommand,
    NativeRecoveryResult,
    RecoveryFailure,
    RecoveryReply,
    RecoveryRequest,
    RegisterOpenRequest,
    decode_native_recovery_prepare_reply,
    decode_native_recovery_reply,
    decode_recovery_reply,
    native_recovery_apply_command,
    native_recovery_prepare_command,
    recovery_failure_reply,
)
from .recovery_crypto import (
    mail_encryption_id,
    mail_key_from_secret,
    request_fingerprint,
    token_hash,
)
from .security import decode_access_token, hash_password, verify_password
from .session_contract import SessionClaims
from .session_manager import sign_session_claims


class RecoveryTransaction(Protocol):
    async def prepare_recovery_command(self, payload: dict) -> dict: ...

    async def apply_recovery_command(self, payload: dict) -> dict: ...


class RecoveryData(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[RecoveryTransaction]: ...

    async def recovery_policy(self): ...


def _password_failure(
    request: RecoveryRequest, password: str
) -> RecoveryFailure | None:
    errors = validate_password_strength(password)
    if not errors:
        return None
    field = "password" if isinstance(request, RegisterOpenRequest) else "new_password"
    detail = [
        {
            "type": "value_error",
            "loc": ["body", field],
            "msg": "Value error, " + "; ".join(errors),
        }
    ]
    return RecoveryFailure("policy_rejected", 422, detail)


def _issue_token(key: str) -> tuple[str, str, str]:
    raw = secrets.token_hex(32)
    digest = token_hash(raw)
    ciphertext = (
        Fernet(key.encode("ascii")).encrypt(raw.encode("utf-8")).decode("ascii")
    )
    return digest, ciphertext, mail_encryption_id(key)


def _verify_signed_claims(
    access_token: str, claims: SessionClaims, secret: str
) -> None:
    try:
        payload = decode_access_token(access_token, secret=secret, verify_exp=False)
        expected = claims.model_dump(mode="json", by_alias=True)
        if payload != expected:
            raise ValueError()
    except Exception:
        raise IdentityUnavailable() from None


def _wire_result(
    request: RecoveryRequest,
    result: NativeRecoveryResult,
    secret: str,
) -> RecoveryReply:
    token = None
    if result.claims is not None:
        token = sign_session_claims(result.claims, secret)
        _verify_signed_claims(token, result.claims, secret)
    value = {
        "schema": RECOVERY_PROTOCOL,
        "ok": True,
        "operation": request.operation,
        "operation_id": str(request.operation_id),
        "data": {
            "account": (
                result.account.model_dump(mode="json", by_alias=True)
                if result.account is not None
                else None
            ),
            "claims": (
                result.claims.model_dump(mode="json", by_alias=True)
                if result.claims is not None
                else None
            ),
            "access_token": token,
            "email_verification_required": result.email_verification_required,
            "completed": result.completed,
        },
    }
    reply = decode_recovery_reply(request, value)
    if reply.data.claims is not None and reply.data.access_token is not None:
        _verify_signed_claims(reply.data.access_token, reply.data.claims, secret)
    return reply


class RecoveryManager:
    def __init__(
        self,
        data: RecoveryData,
        secret: str,
        mail_key: str | None,
        *,
        email_verification_required: bool = False,
        allow_open_registration: bool = False,
        frontend_base_url: str = "http://localhost:3000",
        expire_minutes: int = 1440,
    ):
        self.data = data
        self.secret = secret
        self.mail_key = (
            mail_key if mail_key is not None else mail_key_from_secret(secret)
        )
        # Validate the key at construction, so a bad explicit key is an
        # unavailable deployment and never silently falls back to another key.
        self.mail_encryption_id = mail_encryption_id(self.mail_key)
        self.email_verification_required = email_verification_required
        self.allow_open_registration = allow_open_registration
        self.frontend_base_url = frontend_base_url
        self.expire_minutes = expire_minutes

    def _command(self, request: RecoveryRequest) -> NativeRecoveryPrepareCommand:
        fingerprint, derivation_id = request_fingerprint(self.secret, request)
        config = {
            "allow_open_registration": self.allow_open_registration,
            "email_verification_required": self.email_verification_required,
            "frontend_base_url": self.frontend_base_url,
            "expire_minutes": self.expire_minutes,
        }
        try:
            return native_recovery_prepare_command(
                request,
                request_fingerprint=fingerprint,
                derivation_id=derivation_id,
                config=config,
            )
        except (TypeError, ValueError):
            raise IdentityUnavailable() from None

    @staticmethod
    def _raise_failure(request: RecoveryRequest, failure: RecoveryFailure) -> None:
        # The round-trip applies the same operation-specific safe-error policy
        # to failures generated locally and failures returned by native SQL.
        try:
            decode_recovery_reply(request, recovery_failure_reply(failure))
        except RecoveryFailure as checked:
            raise checked from None
        raise IdentityUnavailable()

    async def _password_hash(self, password: str) -> str:
        try:
            return await asyncio.to_thread(hash_password, password)
        except (ValueError, TypeError, UnicodeError, RuntimeError):
            raise IdentityUnavailable() from None

    async def _verify_current_password(self, password: str, password_hash: str) -> bool:
        try:
            return await asyncio.to_thread(verify_password, password, password_hash)
        except (ValueError, TypeError, UnicodeError, RuntimeError):
            return False

    async def execute(self, request: RecoveryRequest) -> RecoveryReply:
        if isinstance(request, RegisterOpenRequest):
            failure = _password_failure(request, request.payload.password)
            if failure is not None:
                self._raise_failure(request, failure)
            password = request.payload.password
        elif isinstance(request, ConsumePasswordResetRequest):
            failure = _password_failure(request, request.payload.new_password)
            if failure is not None:
                self._raise_failure(request, failure)
            password = request.payload.new_password
        else:
            password = None

        command = self._command(request)
        async with self.data.transaction() as transaction:
            try:
                prepared = decode_native_recovery_prepare_reply(
                    command,
                    await transaction.prepare_recovery_command(
                        command.model_dump(mode="json", by_alias=True)
                    ),
                )
            except RecoveryFailure as failure:
                self._raise_failure(request, failure)

            data = prepared.data
            if data.replay is not None:
                replay_password_hash = None
                if (
                    isinstance(request, RegisterOpenRequest)
                    and data.replay.claims is not None
                ):
                    if data.password_hash is None or data.actor is None:
                        raise IdentityUnavailable()
                    if (
                        not data.actor.is_active
                        or data.actor.email != request.payload.email
                        or not await self._verify_current_password(
                            request.payload.password, data.password_hash
                        )
                    ):
                        self._raise_failure(
                            request,
                            RecoveryFailure(
                                "operation_conflict", 409, "operation_conflict"
                            ),
                        )
                    # Bind the following native apply to the exact password hash
                    # that was checked, rather than making a new bcrypt hash.
                    replay_password_hash = data.password_hash
                replay_token_hash = (
                    command.args.token_hash
                    if request.operation
                    in {
                        "consume_password_reset",
                        "consume_email_verification",
                    }
                    else None
                )
                apply = native_recovery_apply_command(
                    command,
                    password_hash=replay_password_hash,
                    token_hash=replay_token_hash,
                    token_ciphertext=None,
                    encryption_id=None,
                )
                try:
                    applied = decode_native_recovery_reply(
                        apply,
                        await transaction.apply_recovery_command(
                            apply.model_dump(mode="json", by_alias=True)
                        ),
                    )
                except RecoveryFailure as failure:
                    self._raise_failure(request, failure)
                if not applied.data.replayed:
                    raise IdentityUnavailable()
                return _wire_result(request, applied.data.result, self.secret)

            if (
                request.operation
                in {
                    "consume_password_reset",
                    "consume_email_verification",
                }
                and data.token is None
            ):
                self._raise_failure(
                    request,
                    RecoveryFailure(
                        "invalid_request",
                        400,
                        "重置链接无效或已过期"
                        if request.operation == "consume_password_reset"
                        else "验证链接无效或已过期",
                    ),
                )

            password_hash = None
            if password is not None and data.eligible:
                password_hash = await self._password_hash(password)

            issue_mail = (
                data.eligible
                and request.operation
                in {
                    "request_password_reset",
                    "resend_email_verification",
                }
            ) or (
                data.eligible
                and isinstance(request, RegisterOpenRequest)
                and data.policy.email_verification_required
            )
            issued_hash = ciphertext = encryption_id = None
            if issue_mail:
                issued_hash, ciphertext, encryption_id = _issue_token(self.mail_key)

            consumed_hash = None
            if request.operation in {
                "consume_password_reset",
                "consume_email_verification",
            }:
                consumed_hash = command.args.token_hash

            apply = native_recovery_apply_command(
                command,
                password_hash=password_hash,
                token_hash=consumed_hash if consumed_hash is not None else issued_hash,
                token_ciphertext=ciphertext,
                encryption_id=encryption_id,
            )
            try:
                applied = decode_native_recovery_reply(
                    apply,
                    await transaction.apply_recovery_command(
                        apply.model_dump(mode="json", by_alias=True)
                    ),
                )
            except RecoveryFailure as failure:
                self._raise_failure(request, failure)
            if applied.data.replayed:
                raise IdentityUnavailable()
            if not isinstance(request, RegisterOpenRequest):
                prepared_actor = data.actor
                applied_actor = applied.data.actor
                if prepared_actor is None:
                    if applied_actor is not None:
                        raise IdentityUnavailable()
                elif (
                    applied_actor is None
                    or applied_actor.id != prepared_actor.id
                    or applied_actor.email != prepared_actor.email
                ):
                    raise IdentityUnavailable()
            return _wire_result(request, applied.data.result, self.secret)
