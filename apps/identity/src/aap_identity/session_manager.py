"""Portable manager for deterministic Identity session commands."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Protocol

from jose import jwt

from .authenticator import VerifiedCredential
from .contract import IdentityFailure, IdentityUnavailable, Principal
from .security import ALGORITHM, signing_key, verify_password
from .session_contract import (
    SESSION_DERIVATION,
    SESSION_PROTOCOL,
    LoginReply,
    LoginRequest,
    LogoutAllReply,
    LogoutAllRequest,
    LogoutReply,
    LogoutRequest,
    NativeLogin,
    NativeLoginReply,
    NativeLogoutAllReply,
    NativeLogoutReply,
    NativeRefreshReply,
    NativeSessionCommand,
    NativeSessionFailureReply,
    NativeSessionReply,
    RefreshReply,
    RefreshRequest,
    SessionFailure,
    SessionIssuedData,
    SessionPolicy,
    SessionReply,
    SessionRequest,
    SessionClaims,
    decode_login_candidate,
    decode_native_session_reply,
    decode_session_reply,
    native_session_command,
)


class BoundAuthenticator(Protocol):
    async def authenticate_bound(
        self, token: str, *, for_refresh: bool = False
    ) -> VerifiedCredential: ...


class SessionData(Protocol):
    async def read_login_candidate(self, email: str) -> dict: ...

    async def execute_session_command(self, payload: dict) -> dict: ...


class LoginCounter(Protocol):
    async def get_count(self, ip: str) -> int: ...

    async def increment(self, ip: str) -> int: ...

    async def reset(self, ip: str) -> None: ...


def session_derivation(secret: str) -> tuple[bytes, str]:
    master = hmac.new(
        secret.encode("utf-8"), SESSION_DERIVATION.encode("ascii"), hashlib.sha256
    ).digest()
    return master, hashlib.sha256(master).hexdigest()


def login_request_fingerprint(secret: str, email: str, password: str) -> str:
    master, _ = session_derivation(secret)
    canonical = json.dumps(
        {"email": email, "password": password},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hmac.new(master, canonical, hashlib.sha256).hexdigest()


def sign_session_claims(claims: SessionClaims, secret: str) -> str:
    return jwt.encode(
        claims.model_dump(mode="json"),
        signing_key(secret),
        algorithm=ALGORITHM,
        headers={"typ": "JWT"},
    )


def _counter_value(value: object) -> int:
    if type(value) is not int or value < 0:
        raise IdentityUnavailable()
    return value


class SessionManager:
    def __init__(
        self,
        authenticator: BoundAuthenticator,
        data: SessionData,
        secret: str,
        *,
        counter: LoginCounter,
        expire_minutes: int = 1440,
        email_verification_required: bool = False,
        login_captcha_threshold: int = 5,
    ):
        self.authenticator = authenticator
        self.data = data
        self.secret = secret
        self.counter = counter
        self.expire_minutes = expire_minutes
        self.email_verification_required = email_verification_required
        self.login_captcha_threshold = login_captcha_threshold
        _, self.derivation_id = session_derivation(secret)

    def _policy(self) -> SessionPolicy:
        return SessionPolicy(
            expire_minutes=self.expire_minutes,
            email_verification_required=self.email_verification_required,
            derivation_id=self.derivation_id,
        )

    async def execute(self, request: SessionRequest) -> SessionReply:
        if isinstance(request, LoginRequest):
            return await self._login(request)

        try:
            verified = await self.authenticator.authenticate_bound(
                request.token,
                for_refresh=isinstance(request, RefreshRequest),
            )
        except IdentityFailure as error:
            raise SessionFailure(error.code) from None
        if (
            isinstance(request, (RefreshRequest, LogoutRequest))
            and verified.principal.credential_type != "jwt"
        ):
            raise SessionFailure("credential_invalid")

        command = native_session_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            login=None,
            policy=self._policy(),
            request_fingerprint=None,
        )
        reply = await self._execute_native(command)
        if isinstance(reply, NativeSessionFailureReply):
            raise SessionFailure(reply.error.code, reply.error.failed_count)
        return self._wire_reply(request, reply, verified.principal)

    async def _login(self, request: LoginRequest) -> LoginReply:
        failed_before = _counter_value(
            await self.counter.get_count(request.payload.client_ip)
        )
        if (
            failed_before >= self.login_captcha_threshold
            and not request.payload.captcha_verified
        ):
            raise SessionFailure("captcha_required", failed_before)

        candidate = decode_login_candidate(
            await self.data.read_login_candidate(request.payload.email)
        ).item
        password_matches = False
        actor_id = None
        password_hash = None
        if candidate is not None:
            actor_id = candidate.actor_id
            password_hash = candidate.password_hash
            password_matches = await asyncio.to_thread(
                verify_password, request.payload.password, password_hash
            )

        failed_count = failed_before
        if not password_matches:
            failed_count = _counter_value(
                await self.counter.increment(request.payload.client_ip)
            )
        login = NativeLogin(
            email=request.payload.email,
            actor_id=actor_id,
            expected_password_hash=password_hash,
            password_matches=password_matches,
            failed_count=failed_count,
        )
        command = native_session_command(
            request,
            actor=None,
            expected_key_hash=None,
            login=login,
            policy=self._policy(),
            request_fingerprint=login_request_fingerprint(
                self.secret, request.payload.email, request.payload.password
            ),
        )
        reply = await self._execute_native(command)
        if isinstance(reply, NativeSessionFailureReply):
            raise SessionFailure(reply.error.code, reply.error.failed_count)
        if not isinstance(reply, NativeLoginReply):
            raise IdentityUnavailable()
        result = self._wire_reply(request, reply, None)
        await self.counter.reset(request.payload.client_ip)
        return result

    async def _execute_native(
        self, command: NativeSessionCommand
    ) -> NativeSessionReply | NativeSessionFailureReply:
        payload = command.model_dump(mode="json", by_alias=True)
        raw = await self.data.execute_session_command(payload)
        return decode_native_session_reply(command, raw)

    def _wire_reply(
        self,
        request: SessionRequest,
        reply: NativeSessionReply,
        expected_principal: Principal | None,
    ) -> SessionReply:
        common = {
            "schema": SESSION_PROTOCOL,
            "ok": True,
            "operation_id": reply.operation_id,
            "principal": reply.principal,
        }
        if isinstance(reply, NativeLoginReply) and isinstance(request, LoginRequest):
            data = SessionIssuedData(
                claims=reply.data.claims,
                access_token=sign_session_claims(reply.data.claims, self.secret),
            )
            wire: SessionReply = LoginReply(**common, operation="login", data=data)
        elif isinstance(reply, NativeRefreshReply) and isinstance(
            request, RefreshRequest
        ):
            data = SessionIssuedData(
                claims=reply.data.claims,
                access_token=sign_session_claims(reply.data.claims, self.secret),
            )
            wire = RefreshReply(**common, operation="refresh", data=data)
        elif isinstance(reply, NativeLogoutReply) and isinstance(
            request, LogoutRequest
        ):
            wire = LogoutReply(
                **common,
                operation="logout",
                data={"claims": None, "access_token": None},
            )
        elif isinstance(reply, NativeLogoutAllReply) and isinstance(
            request, LogoutAllRequest
        ):
            data = SessionIssuedData(
                claims=reply.data.claims,
                access_token=sign_session_claims(reply.data.claims, self.secret),
            )
            wire = LogoutAllReply(**common, operation="logout_all", data=data)
        else:
            raise IdentityUnavailable()
        return decode_session_reply(
            request,
            wire.model_dump(mode="json", by_alias=True),
            expected_principal=expected_principal,
        )
