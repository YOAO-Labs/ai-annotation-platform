"""Installed Identity owner and deterministic API actor-context qualification."""

from __future__ import annotations

import asyncio
import json
import os
import select as io_select
import secrets
import uuid
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import delete, select, text

from aap_identity.contract import Principal
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


class _ForbiddenCallerDatabase:
    def __getattr__(self, name):
        raise AssertionError(
            f"actor-context remote path touched caller database: {name}"
        )


def _start_node_owner(*, control, actor_id, role, scenarios):
    profile = {
        "name": "  Node-owned fresh name ß  ",
        "email": " node-owned@example.test ",
        "weekly_target_default": 2147483647,
    }
    config = {
        "schema": "aap-identity-actor/v1",
        "control_token": control,
        "actor_id": str(actor_id),
        "admitted_role": role,
        "profile": profile,
        "scenarios": scenarios,
    }
    process = subprocess.Popen(
        [
            "node",
            str(ROOT / "apps/api/tests/fixtures/identity_actor_context_peer.mjs"),
            "--serve",
        ],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(
            json.dumps(config, ensure_ascii=False, allow_nan=False) + "\n"
        )
        process.stdin.close()
        process.stdin = None
        ready_set, _, _ = io_select.select([process.stdout], [], [], 10)
        if not ready_set:
            pytest.fail("Independent Node actor-context owner did not become ready")
        ready_line = process.stdout.readline()
        assert control not in ready_line
        ready = json.loads(ready_line)
        assert set(ready) == {"ready", "origin"} and ready["ready"] is True
        assert ready["origin"].startswith("http://127.0.0.1:")
        return process, ready["origin"], profile
    except BaseException:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
        raise


def _stop_node_owner(process, control):
    if process.poll() is None:
        process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
    assert control not in stdout and control not in stderr


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


async def test_python_gateway_consumes_independent_node_actor_context_peer():
    actor_id = uuid.uuid4()
    control = secrets.token_urlsafe(32)
    scenarios = [
        "success",
        "malformed_json",
        "wrong_id",
        "wrong_role",
        "nonboolean_active",
        "extra_field",
        "credential_invalid",
        "invalid_request",
    ]
    process, origin, profile = _start_node_owner(
        control=control,
        actor_id=actor_id,
        role="employee",
        scenarios=scenarios,
    )
    try:
        from app.services.identity_gateway import IdentityGateway

        gateway = IdentityGateway(
            identity_url=origin,
            identity_token=control,
            timeout_seconds=3,
            signing_secret=secrets.token_urlsafe(32),
        )
        principal = Principal(
            actor_id=actor_id,
            name="Admitted snapshot name",
            email="admitted-snapshot@example.test",
            role="employee",
            is_active=True,
            credential_type="jwt",
            credential_id=uuid.uuid4(),
            generation=0,
            scopes=None,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        caller_db = _ForbiddenCallerDatabase()

        actor = await gateway.actor_context(principal, db=caller_db)
        assert actor.id == actor_id
        assert actor.name == profile["name"]
        assert actor.email == profile["email"]
        assert actor.role == "employee" and actor.is_active is True
        assert actor.weekly_target_default == 2147483647
        assert principal.name == "Admitted snapshot name"
        assert principal.email == "admitted-snapshot@example.test"

        from app.deps import get_current_user

        for scenario in scenarios[1:6]:
            request = _request_with_admitted_state(principal)
            with pytest.raises(HTTPException) as failure:
                await get_current_user(
                    request,
                    principal=principal,
                    db=caller_db,
                    identity=gateway,
                )
            assert failure.value.status_code == 503, scenario
            assert request.state.identity_principal is principal

        credential_request = _request_with_admitted_state(principal)
        with pytest.raises(HTTPException) as credential_failure:
            await get_current_user(
                credential_request,
                principal=principal,
                db=caller_db,
                identity=gateway,
            )
        assert credential_failure.value.status_code == 401
        assert credential_failure.value.headers == {"WWW-Authenticate": "Bearer"}
        for field in ("identity_principal", "api_key_scopes", "api_key_id"):
            assert not hasattr(credential_request.state, field)

        invalid_request = _request_with_admitted_state(principal)
        with pytest.raises(HTTPException) as invalid_failure:
            await get_current_user(
                invalid_request,
                principal=principal,
                db=caller_db,
                identity=gateway,
            )
        assert invalid_failure.value.status_code == 503
        assert invalid_request.state.identity_principal is principal
    finally:
        _stop_node_owner(process, control)
