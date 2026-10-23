"""Account-owner fixtures for HTTP composition and stale-admission races."""

from __future__ import annotations

import copy
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from starlette.requests import Request

from aap_identity.account_manager import AccountManager
from aap_identity.authenticator import VerifiedCredential
from aap_identity.contract import Principal
from app.api.v1.me import update_preferences
from app.config import settings
from app.core.security import create_access_token
from app.schemas.user import UserOut
from app.services.identity_account_data import IdentityAccountCommandData
from app.services.identity_gateway import get_identity_gateway


class FixedAuthenticator:
    def __init__(self, verified):
        self.verified = verified

    async def authenticate_bound(self, _token):
        return self.verified


def _http_writer(commands, principal, token):
    async def write(payload):
        request = Request(
            {
                "type": "http",
                "method": "PATCH",
                "path": "/api/v1/auth/me/preferences",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "state": {"identity_principal": principal},
            }
        )
        return await update_preferences(
            payload, request, principal, SimpleNamespace(credentials=token), commands
        )

    return write


async def preauthenticated_preferences_writer(db, user_id):
    """Resolve admission now; the native transaction must recheck it on each call."""
    token = create_access_token(str(user_id), "employee")
    verified = (
        await get_identity_gateway()
        .embedded_authenticator(db)
        .authenticate_bound(token)
    )
    manager = AccountManager(
        FixedAuthenticator(verified),
        IdentityAccountCommandData(db),
        settings.secret_key,
    )
    return _http_writer(manager, verified.principal, token)


def memory_preferences_writer(existing):
    """Run the real manager and HTTP adapter with a value-only native test double."""
    user = SimpleNamespace(
        id=uuid.uuid4(), preferences=copy.deepcopy(existing), is_active=True
    )
    session = SimpleNamespace(commit=AsyncMock())
    principal = Principal(
        actor_id=user.id,
        name="Preference fixture",
        email="prefs@example.test",
        role="employee",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=0,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    class Data:
        @asynccontextmanager
        async def transaction(self):
            yield self
            await session.commit()

        async def prepare_account_command(self, command):
            profile = UserOut(
                id=user.id,
                email=principal.email,
                name=principal.name,
                role=principal.role,
                group_name=None,
                status="offline",
                preferences=copy.deepcopy(user.preferences),
                created_at=datetime.now(UTC),
            ).model_dump(mode="json")
            return {
                "schema": "aap-identity-accounts-native/v1",
                "ok": True,
                "operation": command["operation"],
                "operation_id": command["operation_id"],
                "principal": principal.model_dump(mode="json"),
                "data": {"account": profile, "password_hash": None, "replay": None},
            }

        async def apply_account_command(self, command):
            assert command["operation"] == "patch_preferences"
            assert command["base_value"] == user.preferences
            user.preferences = copy.deepcopy(command["new_value"])
            return {
                "schema": "aap-identity-accounts-native/v1",
                "ok": True,
                "operation": command["operation"],
                "operation_id": command["operation_id"],
                "principal": principal.model_dump(mode="json"),
                "data": {
                    "account": None,
                    "preferences": command["response_preferences"],
                    "changed": None,
                    "previous_avatar_ref": None,
                },
            }

    manager = AccountManager(
        FixedAuthenticator(VerifiedCredential(principal, None)),
        Data(),
        settings.secret_key,
    )
    write_preferences = _http_writer(manager, principal, "fixture-credential")

    async def write(shortcuts):
        return await write_preferences({"workbench": {"shortcuts": shortcuts}})

    return write, user, session
