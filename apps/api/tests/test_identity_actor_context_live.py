"""Installed Identity owner and deterministic API actor-context qualification."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import delete, select, text

from aap_identity.security import create_access_token
from app.db.models.api_key import ApiKey
from app.db.models.user import User
from tests.factory import create_api_key
from tests.test_identity_admission_live import ROOT, _start_identity, _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_ACTOR_CONTEXT_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity actor-context verifier",
    ),
]


def _actor_request(actor_id, role="employee"):
    return {
        "schema": "aap-identity-actor/v1",
        "actor_id": str(actor_id),
        "admitted_role": role,
    }


def _node(identity, control, request):
    result = subprocess.run(
        [
            "node",
            str(ROOT / "apps/api/tests/fixtures/identity_actor_context_peer.mjs"),
        ],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": request},
            ensure_ascii=False,
            allow_nan=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, "Independent Node actor-context peer failed"
    assert control not in result.stdout and control not in result.stderr
    return json.loads(result.stdout)


def _request_with_admitted_state(principal):
    is_key = principal.credential_type == "api_key"
    return Request(
        {
            "type": "http",
            "headers": [],
            "state": {
                "identity_principal": principal,
                "api_key_scopes": list(principal.scopes or []) if is_key else None,
                "api_key_id": str(principal.credential_id) if is_key else None,
            },
        }
    )


async def _assert_get_current_user_rejected(request, principal, gateway):
    from app.deps import get_current_user

    with pytest.raises(HTTPException) as error:
        await get_current_user(request, principal=principal, db=None, identity=gateway)
    assert error.value.status_code == 401
    assert error.value.headers == {"WWW-Authenticate": "Bearer"}
    for field in ("identity_principal", "api_key_scopes", "api_key_id"):
        assert not hasattr(request.state, field)


async def _cleanup_actor(maker, actor_id):
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(delete(ApiKey).where(ApiKey.user_id == actor_id))
        await db.execute(delete(User).where(User.id == actor_id))


async def test_installed_owner_node_and_admitted_actor_context_preserve_fences(
    test_engine, tmp_path
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = None
    api_key_value = None
    api_key_id = None
    api_key_principal = None
    api_key_last_used = None
    try:
        async with maker.begin() as db:
            actor = await db.get(User, actor_id)
            actor.role = "employee"
            actor.name = "Initial actor name"
            actor.email = f"actor-context-{actor_id}@example.test"
            actor.weekly_target_default = 17
            api_key, api_key_value = await create_api_key(
                db, actor_id, "Actor-context state preservation", ["*"]
            )
            api_key_id = api_key.id
        jwt = create_access_token(
            str(actor_id), "employee", secret=secret, expire_minutes=15, gen=0
        )
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=True,
            enable_sessions=False,
            enable_accounts=False,
            enable_presence=False,
        )

        from app.services.identity_gateway import IdentityGateway

        gateway = IdentityGateway(
            identity_url=identity.origin,
            identity_token=control,
            timeout_seconds=5,
            signing_secret=secret,
        )
        principal = await gateway.authenticate(jwt)
        assert principal.actor_id == actor_id
        assert principal.role == "employee"
        assert principal.generation == 0
        assert principal.name == "Initial actor name"
        assert principal.email == f"actor-context-{actor_id}@example.test"

        initial = await asyncio.to_thread(
            _node, identity, control, _actor_request(actor_id, "employee")
        )
        assert initial == {
            "schema": "aap-identity-actor/v1",
            "ok": True,
            "data": {
                "id": str(actor_id),
                "name": "Initial actor name",
                "email": f"actor-context-{actor_id}@example.test",
                "role": "employee",
                "is_active": True,
                "weekly_target_default": 17,
            },
        }

        api_key_principal = await gateway.authenticate(api_key_value)
        assert api_key_principal.credential_type == "api_key"
        assert api_key_principal.scopes == ["*"]
        async with maker() as db:
            api_key_last_used = await db.scalar(
                select(ApiKey.last_used_at).where(ApiKey.id == api_key_id)
            )
        assert api_key_last_used is not None

        async with maker.begin() as db:
            actor = await db.get(User, actor_id)
            actor.name = "  ß Fresh actor  "
            actor.email = " fresh-actor@example.test "
            actor.weekly_target_default = 91
            actor.credential_generation = 1
            key = await db.get(ApiKey, api_key_id)
            key.scopes = ["changed-after-admission"]

        from app.deps import get_current_user

        jwt_request = _request_with_admitted_state(principal)
        fresh_jwt_actor = await get_current_user(
            jwt_request, principal=principal, db=None, identity=gateway
        )
        assert fresh_jwt_actor.id == actor_id
        assert fresh_jwt_actor.name == "  ß Fresh actor  "
        assert fresh_jwt_actor.email == " fresh-actor@example.test "
        assert fresh_jwt_actor.role == "employee"
        assert fresh_jwt_actor.weekly_target_default == 91
        assert jwt_request.state.identity_principal is principal
        assert principal.name == "Initial actor name"
        assert principal.email == f"actor-context-{actor_id}@example.test"

        key_request = _request_with_admitted_state(api_key_principal)
        fresh_key_actor = await get_current_user(
            key_request, principal=api_key_principal, db=None, identity=gateway
        )
        assert fresh_key_actor.name == "  ß Fresh actor  "
        assert fresh_key_actor.weekly_target_default == 91
        assert key_request.state.identity_principal is api_key_principal
        assert api_key_principal.scopes == ["*"]
        assert key_request.state.api_key_scopes == ["*"]
        assert key_request.state.api_key_id == str(api_key_id)
        async with maker() as db:
            last_used_after_actor_read = await db.scalar(
                select(ApiKey.last_used_at).where(ApiKey.id == api_key_id)
            )
        assert last_used_after_actor_read == api_key_last_used

        refreshed = await asyncio.to_thread(
            _node, identity, control, _actor_request(actor_id, "employee")
        )
        assert refreshed["ok"] is True
        assert refreshed["data"]["name"] == "  ß Fresh actor  "
        assert refreshed["data"]["email"] == " fresh-actor@example.test "
        assert refreshed["data"]["weekly_target_default"] == 91

        unavailable_gateway = IdentityGateway(
            identity_url="http://127.0.0.1:0",
            identity_token=control,
            timeout_seconds=0.5,
            signing_secret=secret,
        )
        async with maker() as db:
            unavailable_request = _request_with_admitted_state(principal)
            with pytest.raises(HTTPException) as unavailable_error:
                await get_current_user(
                    unavailable_request,
                    principal=principal,
                    db=db,
                    identity=unavailable_gateway,
                )
        assert unavailable_error.value.status_code == 503
        assert unavailable_request.state.identity_principal is principal

        async with maker.begin() as db:
            actor = await db.get(User, actor_id)
            actor.role = "viewer"
        role_changed = await asyncio.to_thread(
            _node, identity, control, _actor_request(actor_id, "employee")
        )
        assert role_changed == {
            "schema": "aap-identity-actor/v1",
            "ok": False,
            "error": {"code": "credential_invalid"},
        }
        await _assert_get_current_user_rejected(
            _request_with_admitted_state(principal), principal, gateway
        )

        async with maker.begin() as db:
            actor = await db.get(User, actor_id)
            actor.role = "employee"
            actor.is_active = False
        inactive = await asyncio.to_thread(
            _node, identity, control, _actor_request(actor_id, "employee")
        )
        assert inactive["error"]["code"] == "credential_invalid"
        await _assert_get_current_user_rejected(
            _request_with_admitted_state(principal), principal, gateway
        )

        async with maker.begin() as db:
            await db.execute(delete(ApiKey).where(ApiKey.user_id == actor_id))
            await db.execute(delete(User).where(User.id == actor_id))
        missing = await asyncio.to_thread(
            _node, identity, control, _actor_request(actor_id, "employee")
        )
        assert missing["error"]["code"] == "credential_invalid"
        await _assert_get_current_user_rejected(
            _request_with_admitted_state(principal), principal, gateway
        )
    finally:
        if identity:
            identity.stop()
        await _cleanup_actor(maker, actor_id)
