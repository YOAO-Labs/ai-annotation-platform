from __future__ import annotations

import asyncio
import hmac

from aap_identity.account_contract import (
    ACCOUNT_POLICY_PROTOCOL,
    ACCOUNT_PROTOCOL,
    AccountFailure,
    account_failure_reply,
    decode_account_request,
)
from aap_identity.account_manager import AccountManager
from aap_identity.account_postgres import AccountPostgresData
from aap_identity.authenticator import Authenticator
from aap_identity.contract import (
    REFRESH_GRACE_SECONDS,
    AuthenticateRequest,
    IdentityFailure,
    IdentityUnavailable,
    failure_reply,
    principal_reply,
)
from aap_identity.key_contract import (
    KEY_DERIVATION,
    KeyFailure,
    decode_key_request,
    key_failure_reply,
)
from aap_identity.key_manager import KeyManager
from aap_identity.key_postgres import KeyPostgresData
from aap_identity.login_counter import RedisLoginCounter
from aap_identity.postgres import PostgresData
from aap_identity.presence_contract import (
    decode_presence_request,
)
from aap_identity.presence_postgres import PresencePostgresData
from aap_identity.recovery_contract import (
    RECOVERY_POLICY_PROTOCOL,
    RECOVERY_PROTOCOL,
    RecoveryFailure,
    decode_recovery_request,
    recovery_failure_reply,
)
from aap_identity.recovery_crypto import mail_key_from_secret
from aap_identity.recovery_manager import RecoveryManager
from aap_identity.recovery_postgres import RecoveryPostgresData
from aap_identity.security import ALGORITHM, ISSUER
from aap_identity.session_contract import (
    SessionFailure,
    decode_session_request,
    session_failure_reply,
)
from aap_identity.session_manager import SessionManager
from aap_identity.session_postgres import SessionPostgresData
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import Settings


def create_app(
    settings: Settings | None = None,
    *,
    data: PostgresData | None = None,
    key_data: KeyPostgresData | None = None,
    session_data: SessionPostgresData | None = None,
    login_counter: RedisLoginCounter | None = None,
    account_data: AccountPostgresData | None = None,
    presence_data: PresencePostgresData | None = None,
    recovery_data: RecoveryPostgresData | None = None,
) -> FastAPI:
    settings = settings or Settings()
    data = data or PostgresData(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.connect_timeout_seconds,
        command_timeout=settings.command_timeout_seconds,
    )
    auth = Authenticator(data, settings.signing_secret.get_secret_value())
    if key_data is None and settings.key_database_url is not None:
        key_data = KeyPostgresData(
            settings.key_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    keys = (
        KeyManager(auth, key_data, settings.signing_secret.get_secret_value())
        if key_data is not None
        else None
    )
    if session_data is None and settings.session_database_url is not None:
        session_data = SessionPostgresData(
            settings.session_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    if login_counter is None and settings.login_redis_url is not None:
        login_counter = RedisLoginCounter(
            settings.login_redis_url.get_secret_value(),
            window_seconds=settings.login_failed_window_seconds,
        )
    sessions = (
        SessionManager(
            auth,
            session_data,
            settings.signing_secret.get_secret_value(),
            counter=login_counter,
            expire_minutes=settings.expire_minutes,
            email_verification_required=settings.email_verification_required,
            login_captcha_threshold=settings.login_captcha_threshold,
        )
        if session_data is not None and login_counter is not None
        else None
    )
    if account_data is None and settings.account_database_url is not None:
        account_data = AccountPostgresData(
            settings.account_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    accounts = (
        AccountManager(auth, account_data, settings.signing_secret.get_secret_value())
        if account_data is not None and settings.account_database_url is not None
        else None
    )
    if presence_data is None and settings.presence_database_url is not None:
        presence_data = PresencePostgresData(
            settings.presence_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    recovery_configured = (
        recovery_data is not None or settings.recovery_database_url is not None
    )
    if recovery_data is None and settings.recovery_database_url is not None:
        recovery_data = RecoveryPostgresData(
            settings.recovery_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    recovery_manager = (
        RecoveryManager(
            recovery_data,
            settings.signing_secret.get_secret_value(),
            (
                settings.recovery_mail_key.get_secret_value()
                if settings.recovery_mail_key is not None
                else mail_key_from_secret(settings.signing_secret.get_secret_value())
            ),
            email_verification_required=settings.email_verification_required,
            allow_open_registration=settings.allow_open_registration,
            frontend_base_url=settings.frontend_base_url,
            expire_minutes=settings.expire_minutes,
        )
        if recovery_configured and recovery_data is not None
        else None
    )
    slots = asyncio.Semaphore(settings.max_concurrency)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                policy = await data.authentication_policy()
                if settings.key_database_url is not None:
                    if key_data is None:
                        raise IdentityUnavailable()
                    key_policy = await key_data.key_policy()
                if settings.session_database_url is not None:
                    if session_data is None:
                        raise IdentityUnavailable()
                    session_policy = await session_data.session_policy()
                if settings.account_database_url is not None:
                    if account_data is None:
                        raise IdentityUnavailable()
                    account_policy = await account_data.account_policy()
                if settings.presence_database_url is not None:
                    if presence_data is None:
                        raise IdentityUnavailable()
                    await presence_data.check_capability()
                if recovery_configured:
                    if recovery_data is None:
                        raise IdentityUnavailable()
                    recovery_policy = await recovery_data.recovery_policy()
            if policy != {
                "schema": "aap-identity-policy/v1",
                "issuer": ISSUER,
                "refresh_grace_seconds": REFRESH_GRACE_SECONDS,
            }:
                raise IdentityUnavailable()
            if settings.key_database_url is not None and key_policy != {
                "schema": "aap-identity-keys-policy/v1",
                "derivation": KEY_DERIVATION,
            }:
                raise IdentityUnavailable()
            if settings.session_database_url is not None and session_policy != {
                "schema": "aap-identity-sessions-policy/v1",
                "issuer": ISSUER,
                "algorithm": ALGORITHM,
                "refresh_grace_seconds": REFRESH_GRACE_SECONDS,
            }:
                raise IdentityUnavailable()
            if settings.account_database_url is not None:
                account_policy_value = (
                    account_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(account_policy, "model_dump")
                    else account_policy
                )
                if account_policy_value != {
                    "schema": ACCOUNT_POLICY_PROTOCOL,
                    "policy": ACCOUNT_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if recovery_configured:
                recovery_policy_value = (
                    recovery_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(recovery_policy, "model_dump")
                    else recovery_policy
                )
                if recovery_policy_value != {
                    "schema": RECOVERY_POLICY_PROTOCOL,
                    "policy": RECOVERY_PROTOCOL,
                }:
                    raise IdentityUnavailable()
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.post("/v1/identity/authenticate")
    async def authenticate(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(failure_reply("invalid_request"), status_code=400)
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                try:
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 65536:
                            return JSONResponse(
                                failure_reply("invalid_request"), status_code=400
                            )
                    parsed = AuthenticateRequest.model_validate_json(body)
                except (ValidationError, ValueError, UnicodeError):
                    return JSONResponse(
                        failure_reply("invalid_request"), status_code=400
                    )
                async with slots:
                    principal = await auth.authenticate(parsed.token)
        except IdentityFailure as error:
            return JSONResponse(failure_reply(error.code))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity authentication unavailable"}, status_code=503
            )
        return JSONResponse(principal_reply(principal))

    @app.post("/v1/identity/api-keys/execute")
    async def execute_api_key_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(key_failure_reply("invalid_request"), status_code=400)
        if settings.key_database_url is None or keys is None:
            return JSONResponse(
                {"detail": "Identity API-key commands unavailable"}, status_code=503
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                try:
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 65536:
                            return JSONResponse(
                                key_failure_reply("invalid_request"), status_code=400
                            )
                    parsed = decode_key_request(bytes(body))
                except (ValidationError, ValueError, TypeError, UnicodeError):
                    return JSONResponse(
                        key_failure_reply("invalid_request"), status_code=400
                    )
                async with slots:
                    reply = await keys.execute(parsed)
        except KeyFailure as error:
            return JSONResponse(key_failure_reply(error.code))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity API-key commands unavailable"}, status_code=503
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/sessions/execute")
    async def execute_session_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                session_failure_reply("invalid_request"), status_code=400
            )
        if settings.session_database_url is None or sessions is None:
            return JSONResponse(
                {"detail": "Identity session commands unavailable"}, status_code=503
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                try:
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 65536:
                            return JSONResponse(
                                session_failure_reply("invalid_request"),
                                status_code=400,
                            )
                    parsed = decode_session_request(bytes(body))
                except (ValidationError, ValueError, TypeError, UnicodeError):
                    return JSONResponse(
                        session_failure_reply("invalid_request"), status_code=400
                    )
                async with slots:
                    reply = await sessions.execute(parsed)
        except SessionFailure as error:
            return JSONResponse(session_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity session commands unavailable"}, status_code=503
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/accounts/execute")
    async def execute_account_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                account_failure_reply("invalid_request"), status_code=400
            )
        if settings.account_database_url is None or accounts is None:
            return JSONResponse(
                {"detail": "Identity account commands unavailable"}, status_code=503
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                    try:
                        parsed = decode_account_request(bytes(body))
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            account_failure_reply("invalid_request"), status_code=400
                        )
                    reply = await accounts.execute(parsed)
        except AccountFailure as error:
            return JSONResponse(account_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity account commands unavailable"}, status_code=503
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/presence/mark-inactive")
    async def mark_inactive_accounts_offline(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse({"detail": "Invalid request"}, status_code=400)
        if settings.presence_database_url is None or presence_data is None:
            return JSONResponse(
                {"detail": "Identity presence maintenance unavailable"},
                status_code=503,
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 4096:
                            return JSONResponse(
                                {"detail": "Invalid request"}, status_code=400
                            )
                    try:
                        parsed = decode_presence_request(bytes(body))
                    except (IdentityUnavailable, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            {"detail": "Invalid request"}, status_code=400
                        )
                    reply = await presence_data.mark_inactive_accounts_offline(
                        parsed.cutoff
                    )
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity presence maintenance unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/recovery/execute")
    async def execute_recovery_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                recovery_failure_reply("invalid_request"), status_code=400
            )
        if recovery_manager is None:
            return JSONResponse(
                {"detail": "Identity recovery commands unavailable"},
                status_code=503,
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 65536:
                            return JSONResponse(
                                recovery_failure_reply("invalid_request"),
                                status_code=400,
                            )
                    try:
                        parsed = decode_recovery_request(bytes(body))
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            recovery_failure_reply("invalid_request"), status_code=400
                        )
                    reply = await recovery_manager.execute(parsed)
        except RecoveryFailure as error:
            return JSONResponse(recovery_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity recovery commands unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    return app
