from __future__ import annotations

import asyncio
import hmac
import uuid

from aap_identity_admission.actor_status_contract import (
    actor_status_failure_reply,
    actor_status_reply,
    decode_actor_status_request,
)

from aap_identity.account_contract import (
    ACCOUNT_POLICY_PROTOCOL,
    ACCOUNT_PROTOCOL,
    AccountFailure,
    account_failure_reply,
    decode_account_request,
)
from aap_identity.account_manager import AccountManager
from aap_identity.account_postgres import AccountPostgresData
from aap_identity.actor_contract import (
    ActorContextRequest,
    actor_failure_reply,
    actor_reply,
    decode_actor_request,
)
from aap_identity.actor_reader import ActorContextReader
from aap_identity.actor_status_postgres import ActorStatusPostgresData
from aap_identity.actor_status_reader import ActorStatusData, ActorStatusReader
from aap_identity.admin_password_contract import (
    ADMIN_PASSWORD_POLICY_PROTOCOL,
    ADMIN_PASSWORD_PROTOCOL,
    AdminPasswordFailure,
    decode_admin_password_request,
    admin_password_failure_reply,
)
from aap_identity.admin_password_manager import AdminPasswordManager
from aap_identity.admin_password_postgres import AdminPasswordPostgresData
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
from aap_identity.group_contract import (
    GROUP_POLICY_PROTOCOL,
    GROUP_PROTOCOL,
    GroupFailure,
    decode_group_request,
    group_failure_reply,
)
from aap_identity.group_manager import GroupManager
from aap_identity.group_postgres import GroupPostgresData
from aap_identity.invitation_contract import (
    INVITATION_POLICY_PROTOCOL,
    INVITATION_PROTOCOL,
    InvitationFailure,
    decode_invitation_request,
    invitation_failure_reply,
)
from aap_identity.invitation_manager import InvitationManager
from aap_identity.invitation_postgres import InvitationPostgresData
from aap_identity.deactivation_contract import (
    DEACTIVATION_POLICY_PROTOCOL,
    DEACTIVATION_PROTOCOL,
    DUE_POLICY_PROTOCOL,
    DUE_PROTOCOL,
    DeactivationDueFailure,
    DeactivationFailure,
    decode_deactivation_due_request,
    decode_deactivation_request,
    deactivation_due_failure_reply,
    deactivation_failure_reply,
)
from aap_identity.deactivation_manager import DeactivationManager
from aap_identity.deactivation_postgres import (
    DeactivationDuePostgresData,
    DeactivationPostgresData,
)
from aap_identity.directory_contract import (
    DIRECTORY_POLICY_PROTOCOL,
    DIRECTORY_PROTOCOL,
    DirectoryFailure,
    decode_directory_request,
    directory_failure_reply,
)
from aap_identity.directory_manager import DirectoryManager
from aap_identity.directory_postgres import DirectoryPostgresData
from aap_identity.briefs_contract import (
    BRIEFS_POLICY_PROTOCOL,
    BRIEFS_PROTOCOL,
    BriefsFailure,
    briefs_failure_reply,
    decode_briefs_request,
)
from aap_identity.briefs_manager import BriefsManager
from aap_identity.briefs_postgres import BriefsPostgresData
from aap_identity.login_counter import RedisLoginCounter
from aap_identity.postgres import PostgresData
from aap_identity.presence_contract import (
    decode_presence_request,
)
from aap_identity.presence_postgres import PresencePostgresData
from aap_identity.platform_role_contract import (
    PLATFORM_ROLE_POLICY_PROTOCOL,
    PLATFORM_ROLE_PROTOCOL,
    PlatformRoleFailure,
    decode_platform_role_request,
    platform_role_failure_reply,
)
from aap_identity.platform_role_manager import PlatformRoleManager
from aap_identity.platform_role_postgres import PlatformRolePostgresData
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
from aap_identity.registration_contract import decode_registration_status_request
from aap_identity.registration_reader import RegistrationStatusReader
from aap_identity.security import ALGORITHM, ISSUER
from aap_identity.session_contract import (
    SessionFailure,
    decode_session_request,
    session_failure_reply,
)
from aap_identity.session_manager import SessionManager
from aap_identity.session_postgres import SessionPostgresData
from aap_identity.suspension_contract import (
    SUSPENSION_POLICY_PROTOCOL,
    SUSPENSION_PROTOCOL,
    SuspensionFailure,
    decode_suspension_request,
    suspension_failure_reply,
)
from aap_identity.suspension_manager import SuspensionManager
from aap_identity.suspension_postgres import SuspensionPostgresData
from aap_identity.deletion_contract import (
    DELETION_POLICY_PROTOCOL,
    DELETION_PROTOCOL,
    DeletionFailure,
    decode_deletion_request,
    deletion_failure_reply,
)
from aap_identity.deletion_manager import DeletionManager
from aap_identity.deletion_postgres import DeletionPostgresData
from aap_identity.offboarding_contract import (
    OFFBOARDING_POLICY_PROTOCOL,
    OFFBOARDING_PROTOCOL,
    OffboardingFailure,
    decode_offboarding_request,
    offboarding_failure_reply,
)
from aap_identity.offboarding_manager import OffboardingManager
from aap_identity.offboarding_postgres import OffboardingPostgresData
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
    group_data: GroupPostgresData | None = None,
    deactivation_data: DeactivationPostgresData | None = None,
    deactivation_due_data: DeactivationDuePostgresData | None = None,
    invitation_data: InvitationPostgresData | None = None,
    directory_data: DirectoryPostgresData | None = None,
    admin_password_data: AdminPasswordPostgresData | None = None,
    platform_role_data: PlatformRolePostgresData | None = None,
    suspension_data: SuspensionPostgresData | None = None,
    deletion_data: DeletionPostgresData | None = None,
    offboarding_data: OffboardingPostgresData | None = None,
    briefs_data: BriefsPostgresData | None = None,
    actor_status_data: ActorStatusData | None = None,
) -> FastAPI:
    settings = settings or Settings()
    data = data or PostgresData(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.connect_timeout_seconds,
        command_timeout=settings.command_timeout_seconds,
    )
    auth = Authenticator(data, settings.signing_secret.get_secret_value())
    actor_context_reader = ActorContextReader(data)
    if actor_status_data is None and settings.actor_status_database_url is not None:
        actor_status_data = ActorStatusPostgresData(
            settings.actor_status_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    actor_status_reader = (
        ActorStatusReader(actor_status_data) if actor_status_data is not None else None
    )
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
    registration_status_reader = RegistrationStatusReader(
        recovery_data,
        allow_open_registration=settings.allow_open_registration,
        frontend_base_url=settings.frontend_base_url,
    )
    group_configured = group_data is not None or settings.group_database_url is not None
    if group_data is None and settings.group_database_url is not None:
        group_data = GroupPostgresData(
            settings.group_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    groups = (
        GroupManager(auth, group_data, settings.signing_secret.get_secret_value())
        if group_data is not None
        else None
    )
    invitation_configured = (
        invitation_data is not None or settings.invitation_database_url is not None
    )
    if invitation_data is None and settings.invitation_database_url is not None:
        invitation_data = InvitationPostgresData(
            settings.invitation_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    invitations = (
        InvitationManager(
            auth,
            invitation_data,
            settings.signing_secret.get_secret_value(),
            (
                settings.recovery_mail_key.get_secret_value()
                if settings.recovery_mail_key is not None
                else None
            ),
            invitation_ttl_days=settings.invitation_ttl_days,
            max_invitations_per_day=settings.max_invitations_per_day,
            frontend_base_url=settings.invitation_frontend_base_url,
            expire_minutes=settings.expire_minutes,
        )
        if invitation_data is not None
        else None
    )
    directory_configured = (
        directory_data is not None or settings.directory_database_url is not None
    )
    if directory_data is None and settings.directory_database_url is not None:
        directory_data = DirectoryPostgresData(
            settings.directory_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    directory = (
        DirectoryManager(
            auth, directory_data, settings.signing_secret.get_secret_value()
        )
        if directory_data is not None
        else None
    )
    briefs_configured = (
        briefs_data is not None or settings.briefs_database_url is not None
    )
    if briefs_data is None and settings.briefs_database_url is not None:
        briefs_data = BriefsPostgresData(
            settings.briefs_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    briefs = BriefsManager(briefs_data) if briefs_data is not None else None
    admin_password_configured = (
        admin_password_data is not None
        or settings.admin_password_database_url is not None
    )
    if admin_password_data is None and settings.admin_password_database_url is not None:
        admin_password_data = AdminPasswordPostgresData(
            settings.admin_password_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    admin_passwords = (
        AdminPasswordManager(
            auth, admin_password_data, settings.signing_secret.get_secret_value()
        )
        if admin_password_data is not None
        else None
    )
    platform_role_configured = (
        platform_role_data is not None
        or settings.platform_role_database_url is not None
    )
    if platform_role_data is None and settings.platform_role_database_url is not None:
        platform_role_data = PlatformRolePostgresData(
            settings.platform_role_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    platform_roles = (
        PlatformRoleManager(
            auth, platform_role_data, settings.signing_secret.get_secret_value()
        )
        if platform_role_data is not None
        else None
    )
    deactivation_configured = (
        deactivation_data is not None or settings.deactivation_database_url is not None
    )
    if deactivation_data is None and settings.deactivation_database_url is not None:
        deactivation_data = DeactivationPostgresData(
            settings.deactivation_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    deactivation_manager = (
        DeactivationManager(
            auth, deactivation_data, settings.signing_secret.get_secret_value()
        )
        if deactivation_data is not None
        else None
    )
    deactivation_due_configured = (
        deactivation_due_data is not None
        or settings.deactivation_operator_database_url is not None
    )
    if (
        deactivation_due_data is None
        and settings.deactivation_operator_database_url is not None
    ):
        deactivation_due_data = DeactivationDuePostgresData(
            settings.deactivation_operator_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    suspension_configured = (
        suspension_data is not None or settings.suspension_database_url is not None
    )
    if suspension_data is None and settings.suspension_database_url is not None:
        suspension_data = SuspensionPostgresData(
            settings.suspension_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    suspensions = (
        SuspensionManager(
            auth, suspension_data, settings.signing_secret.get_secret_value()
        )
        if suspension_data is not None
        else None
    )
    deletion_configured = (
        deletion_data is not None or settings.deletion_database_url is not None
    )
    if deletion_data is None and settings.deletion_database_url is not None:
        deletion_data = DeletionPostgresData(
            settings.deletion_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    deletions = (
        DeletionManager(auth, deletion_data, settings.signing_secret.get_secret_value())
        if deletion_data is not None
        else None
    )
    offboarding_configured = (
        offboarding_data is not None or settings.offboarding_database_url is not None
    )
    if offboarding_data is None and settings.offboarding_database_url is not None:
        offboarding_data = OffboardingPostgresData(
            settings.offboarding_database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    offboardings = (
        OffboardingManager(
            auth, offboarding_data, settings.signing_secret.get_secret_value()
        )
        if offboarding_data is not None
        else None
    )
    slots = asyncio.Semaphore(settings.max_concurrency)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def admission_authorized(request: Request) -> bool:
        supplied = request.headers.get("authorization", "").encode()
        expected = [("Bearer " + settings.token.get_secret_value()).encode()]
        if settings.admission_token is not None:
            expected.append(
                ("Bearer " + settings.admission_token.get_secret_value()).encode()
            )
        return any(hmac.compare_digest(supplied, candidate) for candidate in expected)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                policy = await data.authentication_policy()
                if actor_status_reader is not None:
                    async with slots:
                        await actor_status_reader.read(uuid.UUID(int=0))
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
                if group_configured:
                    if group_data is None:
                        raise IdentityUnavailable()
                    group_policy = await group_data.group_policy()
                if invitation_configured:
                    if invitation_data is None:
                        raise IdentityUnavailable()
                    invitation_policy = await invitation_data.invitation_policy()
                if directory_configured:
                    if directory_data is None:
                        raise IdentityUnavailable()
                    directory_policy = await directory_data.directory_policy()
                if briefs_configured:
                    if briefs_data is None:
                        raise IdentityUnavailable()
                    briefs_policy = await briefs_data.briefs_policy()
                if admin_password_configured:
                    if admin_password_data is None:
                        raise IdentityUnavailable()
                    admin_password_policy = (
                        await admin_password_data.admin_password_policy()
                    )
                if platform_role_configured:
                    if platform_role_data is None:
                        raise IdentityUnavailable()
                    platform_role_policy = (
                        await platform_role_data.platform_role_policy()
                    )
                if deactivation_configured:
                    if deactivation_data is None:
                        raise IdentityUnavailable()
                    deactivation_policy = await deactivation_data.deactivation_policy()
                if deactivation_due_configured:
                    if deactivation_due_data is None:
                        raise IdentityUnavailable()
                    due_policy = await deactivation_due_data.due_policy()
                if suspension_configured:
                    if suspension_data is None:
                        raise IdentityUnavailable()
                    suspension_policy = await suspension_data.suspension_policy()
                if deletion_configured:
                    if deletion_data is None:
                        raise IdentityUnavailable()
                    deletion_policy = await deletion_data.deletion_policy()
                if offboarding_configured:
                    if offboarding_data is None:
                        raise IdentityUnavailable()
                    offboarding_policy = await offboarding_data.offboarding_policy()
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
            if group_configured:
                group_policy_value = (
                    group_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(group_policy, "model_dump")
                    else group_policy
                )
                if group_policy_value != {
                    "schema": GROUP_POLICY_PROTOCOL,
                    "policy": GROUP_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if invitation_configured:
                invitation_policy_value = (
                    invitation_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(invitation_policy, "model_dump")
                    else invitation_policy
                )
                if invitation_policy_value != {
                    "schema": INVITATION_POLICY_PROTOCOL,
                    "policy": INVITATION_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if directory_configured:
                directory_policy_value = (
                    directory_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(directory_policy, "model_dump")
                    else directory_policy
                )
                if directory_policy_value != {
                    "schema": DIRECTORY_POLICY_PROTOCOL,
                    "policy": DIRECTORY_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if briefs_configured:
                briefs_policy_value = (
                    briefs_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(briefs_policy, "model_dump")
                    else briefs_policy
                )
                if briefs_policy_value != {
                    "schema": BRIEFS_POLICY_PROTOCOL,
                    "policy": BRIEFS_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if admin_password_configured:
                admin_password_policy_value = (
                    admin_password_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(admin_password_policy, "model_dump")
                    else admin_password_policy
                )
                if admin_password_policy_value != {
                    "schema": ADMIN_PASSWORD_POLICY_PROTOCOL,
                    "policy": ADMIN_PASSWORD_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if platform_role_configured:
                platform_role_policy_value = (
                    platform_role_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(platform_role_policy, "model_dump")
                    else platform_role_policy
                )
                if platform_role_policy_value != {
                    "schema": PLATFORM_ROLE_POLICY_PROTOCOL,
                    "policy": PLATFORM_ROLE_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if deactivation_configured:
                deactivation_policy_value = (
                    deactivation_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(deactivation_policy, "model_dump")
                    else deactivation_policy
                )
                if deactivation_policy_value != {
                    "schema": DEACTIVATION_POLICY_PROTOCOL,
                    "policy": DEACTIVATION_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if deactivation_due_configured:
                due_policy_value = (
                    due_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(due_policy, "model_dump")
                    else due_policy
                )
                if due_policy_value != {
                    "schema": DUE_POLICY_PROTOCOL,
                    "policy": DUE_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if suspension_configured:
                suspension_policy_value = (
                    suspension_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(suspension_policy, "model_dump")
                    else suspension_policy
                )
                if suspension_policy_value != {
                    "schema": SUSPENSION_POLICY_PROTOCOL,
                    "policy": SUSPENSION_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if deletion_configured:
                deletion_policy_value = (
                    deletion_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(deletion_policy, "model_dump")
                    else deletion_policy
                )
                if deletion_policy_value != {
                    "schema": DELETION_POLICY_PROTOCOL,
                    "policy": DELETION_PROTOCOL,
                }:
                    raise IdentityUnavailable()
            if offboarding_configured:
                offboarding_policy_value = (
                    offboarding_policy.model_dump(mode="json", by_alias=True)
                    if hasattr(offboarding_policy, "model_dump")
                    else offboarding_policy
                )
                if offboarding_policy_value != {
                    "schema": OFFBOARDING_POLICY_PROTOCOL,
                    "policy": OFFBOARDING_PROTOCOL,
                }:
                    raise IdentityUnavailable()
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    @app.post("/v1/identity/authenticate")
    async def authenticate(request: Request):
        if not admission_authorized(request):
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

    @app.post("/v1/identity/actor-status")
    async def read_actor_status(request: Request):
        if not admission_authorized(request):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                actor_status_failure_reply(),
                status_code=400,
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                try:
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 65536:
                            return JSONResponse(
                                actor_status_failure_reply(), status_code=400
                            )
                    parsed = decode_actor_status_request(bytes(body))
                except IdentityFailure:
                    return JSONResponse(actor_status_failure_reply(), status_code=400)
                if actor_status_reader is None:
                    raise IdentityUnavailable()
                async with slots:
                    actor = await actor_status_reader.read(parsed.actor_id)
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity actor status unavailable"}, status_code=503
            )
        return JSONResponse(actor_status_reply(actor))

    @app.post("/v1/identity/actor-context")
    async def read_actor_context(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(actor_failure_reply("invalid_request"), status_code=400)
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 65536:
                        return JSONResponse(
                            actor_failure_reply("invalid_request"), status_code=400
                        )
                parsed: ActorContextRequest = decode_actor_request(bytes(body))
                async with slots:
                    actor = await actor_context_reader.read(parsed)
        except IdentityFailure as error:
            if error.code not in {"invalid_request", "credential_invalid"}:
                return JSONResponse(
                    {"detail": "Identity actor context unavailable"},
                    status_code=503,
                )
            return JSONResponse(
                actor_failure_reply(error.code),
                status_code=400 if error.code == "invalid_request" else 200,
            )
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity actor context unavailable"}, status_code=503
            )
        return JSONResponse(actor_reply(actor))

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

    @app.post("/v1/identity/recovery/registration-status")
    async def read_registration_status(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                {"detail": "Invalid registration status request"}, status_code=400
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 65536:
                            return JSONResponse(
                                {"detail": "Invalid registration status request"},
                                status_code=400,
                            )
                    try:
                        parsed = decode_registration_status_request(bytes(body))
                    except (
                        IdentityUnavailable,
                        ValidationError,
                        ValueError,
                        TypeError,
                        UnicodeError,
                    ):
                        return JSONResponse(
                            {"detail": "Invalid registration status request"},
                            status_code=400,
                        )
                    reply = await registration_status_reader.read(parsed)
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity registration status unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/invitations/execute")
    async def execute_invitation_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                {"detail": "Invalid identity invitation command"}, status_code=400
            )
        if invitations is None:
            return JSONResponse(
                {"detail": "Identity invitation commands unavailable"},
                status_code=503,
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    body = await request.body()
                    try:
                        parsed = decode_invitation_request(body)
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            {"detail": "Invalid identity invitation command"},
                            status_code=400,
                        )
                    reply = await invitations.execute(parsed)
        except InvitationFailure as error:
            return JSONResponse(invitation_failure_reply(parsed, error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity invitation commands unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/groups/execute")
    async def execute_group_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(group_failure_reply("invalid_request"), status_code=400)
        if groups is None:
            return JSONResponse(
                {"detail": "Identity group commands unavailable"},
                status_code=503,
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    body = await request.body()
                    try:
                        parsed = decode_group_request(body)
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            group_failure_reply("invalid_request"), status_code=400
                        )
                    reply = await groups.execute(parsed)
        except GroupFailure as error:
            return JSONResponse(group_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity group commands unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/directory/execute")
    async def execute_directory_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                directory_failure_reply(DirectoryFailure("invalid_request", 400)),
                status_code=400,
            )
        if directory is None:
            return JSONResponse(
                {"detail": "Identity directory commands unavailable"},
                status_code=503,
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    body = await request.body()
                    try:
                        parsed = decode_directory_request(body)
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            directory_failure_reply(
                                DirectoryFailure("invalid_request", 400)
                            ),
                            status_code=400,
                        )
                    reply = await directory.execute(parsed)
        except DirectoryFailure as error:
            return JSONResponse(directory_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity directory commands unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/briefs/execute")
    async def execute_briefs_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                briefs_failure_reply("invalid_request"), status_code=400
            )
        if briefs is None:
            return JSONResponse(
                {"detail": "Identity brief reads unavailable"}, status_code=503
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    body = await request.body()
                    parsed = decode_briefs_request(body)
                    reply = await briefs.execute(parsed)
        except BriefsFailure as error:
            return JSONResponse(briefs_failure_reply(error), status_code=400)
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity brief reads unavailable"}, status_code=503
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/admin-passwords/execute")
    async def execute_admin_password_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                admin_password_failure_reply("invalid_request"), status_code=400
            )
        if admin_passwords is None:
            return JSONResponse(
                {"detail": "Identity administrator password service unavailable"},
                status_code=503,
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    try:
                        body = bytearray()
                        async for chunk in request.stream():
                            body.extend(chunk)
                            if len(body) > 65536:
                                return JSONResponse(
                                    admin_password_failure_reply("invalid_request"),
                                    status_code=400,
                                )
                        parsed = decode_admin_password_request(bytes(body))
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            admin_password_failure_reply("invalid_request"),
                            status_code=400,
                        )
                    reply = await admin_passwords.execute(parsed)
        except AdminPasswordFailure as error:
            return JSONResponse(admin_password_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity administrator password service unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/platform-roles/execute")
    async def execute_platform_role_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                platform_role_failure_reply("invalid_request"), status_code=400
            )
        if platform_roles is None:
            return JSONResponse(
                {"detail": "Identity platform-role commands unavailable"},
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
                                platform_role_failure_reply("invalid_request"),
                                status_code=400,
                            )
                    try:
                        parsed = decode_platform_role_request(bytes(body))
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            platform_role_failure_reply("invalid_request"),
                            status_code=400,
                        )
                    reply = await platform_roles.execute(parsed)
        except PlatformRoleFailure as error:
            return JSONResponse(platform_role_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity platform-role commands unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/deactivation/execute")
    async def execute_deactivation_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                deactivation_failure_reply("invalid_request"), status_code=400
            )
        if deactivation_manager is None:
            return JSONResponse(
                {"detail": "Identity deactivation commands unavailable"},
                status_code=503,
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    body = await request.body()
                    try:
                        parsed = decode_deactivation_request(body)
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            deactivation_failure_reply("invalid_request"),
                            status_code=400,
                        )
                    reply = await deactivation_manager.execute(parsed)
        except DeactivationFailure as error:
            return JSONResponse(deactivation_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity deactivation commands unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/suspensions/execute")
    async def execute_suspension_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                suspension_failure_reply("invalid_request"), status_code=400
            )
        if suspensions is None:
            return JSONResponse(
                {"detail": "Identity suspension commands unavailable"},
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
                                suspension_failure_reply("invalid_request"),
                                status_code=400,
                            )
                    try:
                        parsed = decode_suspension_request(bytes(body))
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            suspension_failure_reply("invalid_request"),
                            status_code=400,
                        )
                    reply = await suspensions.execute(parsed)
        except SuspensionFailure as error:
            return JSONResponse(suspension_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity suspension commands unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/deletions/execute")
    async def execute_deletion_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                deletion_failure_reply("invalid_request"), status_code=400
            )
        if deletions is None:
            return JSONResponse(
                {"detail": "Identity deletion commands unavailable"},
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
                                deletion_failure_reply("invalid_request"),
                                status_code=400,
                            )
                    try:
                        parsed = decode_deletion_request(bytes(body))
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            deletion_failure_reply("invalid_request"),
                            status_code=400,
                        )
                    reply = await deletions.execute(parsed)
        except DeletionFailure as error:
            return JSONResponse(deletion_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity deletion commands unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/offboarding/execute")
    async def execute_offboarding_command(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                offboarding_failure_reply("invalid_request"), status_code=400
            )
        if offboardings is None:
            return JSONResponse(
                {"detail": "Identity offboarding commands unavailable"},
                status_code=503,
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    # The legacy stale response must echo unbounded raw preview
                    # versions, including NUL. This private route has no small body cap.
                    body = await request.body()
                    try:
                        parsed = decode_offboarding_request(body)
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            offboarding_failure_reply("invalid_request"),
                            status_code=400,
                        )
                    reply = await offboardings.execute(parsed)
        except OffboardingFailure as error:
            return JSONResponse(offboarding_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity offboarding commands unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    @app.post("/v1/identity/deactivation/process-due")
    async def process_due_deactivations(request: Request):
        supplied = request.headers.get("authorization", "").encode()
        expected = ("Bearer " + settings.token.get_secret_value()).encode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return JSONResponse(
                deactivation_due_failure_reply("invalid_request"), status_code=400
            )
        if deactivation_due_data is None:
            return JSONResponse(
                {"detail": "Identity deactivation operator unavailable"},
                status_code=503,
            )
        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                async with slots:
                    body = await request.body()
                    try:
                        parsed = decode_deactivation_due_request(body)
                    except (ValidationError, ValueError, TypeError, UnicodeError):
                        return JSONResponse(
                            deactivation_due_failure_reply("invalid_request"),
                            status_code=400,
                        )
                    reply = await deactivation_due_data.process_due_deactivations(
                        parsed
                    )
        except DeactivationDueFailure as error:
            return JSONResponse(deactivation_due_failure_reply(error))
        except (IdentityUnavailable, TimeoutError):
            return JSONResponse(
                {"detail": "Identity deactivation operator unavailable"},
                status_code=503,
            )
        return JSONResponse(reply.model_dump(mode="json", by_alias=True))

    return app
