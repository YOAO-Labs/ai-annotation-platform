"""Actual API and Recovery processes agree on policy with distinct edge secrets."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import uuid

from cryptography.fernet import Fernet
import httpx
import pytest
from jose.exceptions import JWTError
from sqlalchemy import select, text

from aap_identity.security import decode_access_token
from app.db.models.user import User
from tests.identity_recovery_live_helpers import cleanup
from tests.test_identity_admission_live import ROOT, _start_api, _start_identity, _token
from tests.test_identity_auth_native import _seed
from tests.test_identity_read_relations_native import _grants
from tests.test_identity_registration_status_native import _ABSENT, _override


def _node_policy_read(origin, control, operation):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_registration_peer.mjs")],
        input=json.dumps({"url": origin, "control": control, "operation": operation}),
        text=True,
        capture_output=True,
        timeout=15,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, "Independent registration read peer failed"
    return json.loads(result.stdout)


def test_wrong_signer_is_rejected_by_the_declared_jose_verifier():
    actor = uuid.uuid4()
    token = _token(actor, "owned-signer-fixture")
    assert decode_access_token(token, secret="owned-signer-fixture")["sub"] == str(
        actor
    )
    with pytest.raises(JWTError, match="Signature verification failed"):
        decode_access_token(token, secret="edge-signer-fixture")


@pytest.mark.asyncio
async def test_real_api_status_settings_and_registration_share_the_remote_owner_default(
    test_engine, tmp_path
):
    if os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1" or not os.getenv(
        "AAP_IDENTITY_RUNTIME_PYTHON"
    ):
        pytest.skip(
            "requires the owned role verifier and explicit source runtime interpreter"
        )
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    actor = fixture["actor_id"]
    owner_secret, edge_secret, control = [secrets.token_urlsafe(32) for _ in range(3)]
    assert len({owner_secret, edge_secret, control}) == 3
    identity = api = None
    mail_key = Fernet.generate_key().decode()
    async with _override(test_engine, _ABSENT) as maker:
        async with maker() as db:
            initial = set(
                (
                    await db.execute(
                        text(
                            "SELECT operation_id FROM identity_recovery_operation_receipts"
                        )
                    )
                ).scalars()
            )
        actors = [actor]
        try:
            _grants(
                test_engine,
                "identity-consumer-grants.sql",
                identity_owner="identity_code",
                consumer_role="legacy_runtime",
            )
            identity = await asyncio.to_thread(
                _start_identity,
                test_engine,
                tmp_path,
                owner_secret,
                control,
                enable_keys=False,
                enable_sessions=False,
                enable_accounts=False,
                enable_presence=False,
                recovery_mail_key=mail_key,
                session_settings={
                    "IDENTITY_ALLOW_OPEN_REGISTRATION": "true",
                    "IDENTITY_EMAIL_VERIFICATION_REQUIRED": "false",
                },
            )
            api = await asyncio.to_thread(
                _start_api,
                tmp_path,
                edge_secret,
                control,
                identity.origin,
                database_url=os.environ["AAP_MODULE_DATA_LEGACY_RUNTIME_URL"],
                owner_signing_secret=owner_secret,
                configuration={
                    "ALLOW_OPEN_REGISTRATION": "false",
                    "AUDIT_ASYNC": "false",
                    "TURNSTILE_ENABLED": "false",
                    "IDENTITY_TIMEOUT_SECONDS": "10",
                },
            )
            headers = {"Authorization": "Bearer " + _token(actor, owner_secret)}
            assert (
                await asyncio.to_thread(
                    _node_policy_read, identity.origin, control, "read_status"
                )
                is True
            )
            assert (
                await asyncio.to_thread(
                    _node_policy_read,
                    identity.origin,
                    control,
                    "read_deployment_default",
                )
                is True
            )
            async with httpx.AsyncClient(
                base_url=api.origin, trust_env=False, timeout=20
            ) as client:
                status = await client.get("/api/v1/auth/registration-status")
                assert status.status_code == 200
                assert status.json() == {"open_registration_enabled": True}
                settings = await client.get("/api/v1/settings/system", headers=headers)
                assert settings.status_code == 200, settings.text
                settings = settings.json()
                assert settings["allow_open_registration"] is True
                assert (
                    settings["metadata"]["allow_open_registration"][
                        "deployment_default"
                    ]
                    is True
                )
                changed = await client.patch(
                    "/api/v1/settings/system",
                    headers=headers,
                    json={
                        "allow_open_registration": False,
                        "expected_version": settings["version"],
                    },
                )
                assert changed.status_code == 200, changed.text
                assert changed.json()["allow_open_registration"] is False
                assert (
                    await client.get("/api/v1/auth/registration-status")
                ).json() == {"open_registration_enabled": False}
                email = f"policy-live-{uuid.uuid4().hex}@example.test"
                registration = {
                    "email": email,
                    "name": "Policy source acceptance",
                    "password": "StrongPolicy123!",
                }
                disabled = await client.post(
                    "/api/v1/auth/register-open", json=registration
                )
                assert disabled.status_code == 403
                reset = await client.post(
                    "/api/v1/settings/system/reset",
                    headers=headers,
                    json={
                        "keys": ["allow_open_registration"],
                        "expected_version": changed.json()["version"],
                    },
                )
                assert reset.status_code == 200, reset.text
                assert reset.json()["allow_open_registration"] is True
                assert (
                    await client.get("/api/v1/auth/registration-status")
                ).json() == {"open_registration_enabled": True}
                async with maker.begin() as writer:
                    await writer.execute(
                        text(
                            "INSERT INTO system_settings(key,value_type,value_json) VALUES('allow_open_registration','bool','\"malformed\"'::jsonb) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json"
                        )
                    )
                assert (
                    await client.get("/api/v1/auth/registration-status")
                ).status_code == 503
                repair_view = await client.get(
                    "/api/v1/settings/system", headers=headers
                )
                assert repair_view.status_code == 200, repair_view.text
                assert repair_view.json()["allow_open_registration"] is True
                repaired = await client.patch(
                    "/api/v1/settings/system",
                    headers=headers,
                    json={
                        "allow_open_registration": True,
                        "expected_version": repair_view.json()["version"],
                    },
                )
                assert repaired.status_code == 200, repaired.text
                assert (
                    await client.get("/api/v1/auth/registration-status")
                ).json() == {"open_registration_enabled": True}
                registered = await client.post(
                    "/api/v1/auth/register-open", json=registration
                )
                assert registered.status_code == 201, registered.text
                new_actor = uuid.UUID(registered.json()["user"]["id"])
                actors.append(new_actor)
                token = registered.json()["access_token"]
                assert decode_access_token(token, secret=owner_secret)["sub"] == str(
                    new_actor
                )
                with pytest.raises(JWTError):
                    decode_access_token(token, secret=edge_secret)
                identity.stop()
                identity = None
                assert (
                    await client.get("/api/v1/auth/registration-status")
                ).status_code == 503
                rejected = await client.patch(
                    "/api/v1/settings/system",
                    headers=headers,
                    json={"allow_open_registration": False},
                )
                assert rejected.status_code == 503
                async with maker() as observer:
                    assert (
                        await observer.scalar(
                            text(
                                "SELECT value_json FROM system_settings WHERE key='allow_open_registration'"
                            )
                        )
                        is True
                    )
        finally:
            if api:
                api.stop()
            if identity:
                identity.stop()
            async with maker() as db:
                # Include a created account even if an assertion failed before its response was decoded.
                actors = list(
                    set(actors)
                    | set(
                        (
                            await db.execute(
                                select(User.id).where(
                                    User.email.like("policy-live-%@example.test")
                                )
                            )
                        ).scalars()
                    )
                )
                operations = set(
                    (
                        await db.execute(
                            text(
                                "SELECT operation_id FROM identity_recovery_operation_receipts"
                            )
                        )
                    ).scalars()
                ).difference(initial)
            await cleanup(maker, actors, operations)
            _grants(
                test_engine,
                "lan-production-grants.sql",
                application_role="legacy_runtime",
                asset_cleanup_role="asset_cleanup_client",
            )
