"""AUTH-only API and Celery consumers use the selected deactivation owners."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import secrets
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings
from tests.factory import create_user
from tests.test_identity_admission_live import (
    _dsn,
    _start_api,
    _start_container_identity,
    _start_identity,
    _token,
    _world,
)
from tests.test_identity_deactivation_live import _cleanup_deactivation

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_DEACTIVATION_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned deactivation verifier",
    ),
]


def _auth_dsn(test_engine):
    return (
        make_url(_dsn("AUTH", test_engine))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_table_free_deactivation_api_and_two_celery_event_loops(
    test_engine, tmp_path, monkeypatch, binding, record_property
):
    from app.workers.deactivation import process_deactivation_requests
    from app.workers.notification_delivery import relay_identity_notifications

    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = api = None
    actor_ids, due_ids = [actor], []
    try:
        async with maker.begin() as db:
            successor = await create_user(
                db,
                "super_admin",
                f"api-successor-{uuid.uuid4()}@test.local",
                "Successor",
            )
        actor_ids.append(successor.id)
        if binding == "remote_container":
            identity = await _start_container_identity(
                test_engine,
                tmp_path,
                secret,
                control,
                enable_accounts=False,
                enable_presence=False,
                enable_deactivation=True,
                enable_deactivation_operator=True,
            )
            origin, configuration = identity.origin, {}
        else:
            origin = ""
            configuration = {
                "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
                "IDENTITY_DEACTIVATION_DATABASE_URL": _dsn("DEACTIVATION", test_engine),
            }
        auth_dsn = _auth_dsn(test_engine)
        api = _start_api(
            tmp_path,
            secret,
            control,
            origin,
            database_url=auth_dsn,
            configuration=configuration,
        )
        async with httpx.AsyncClient(
            base_url=api.origin,
            trust_env=False,
            timeout=30,
            headers={"Authorization": f"Bearer {_token(actor, secret)}"},
        ) as client:
            path = "/api/v1/auth/me/deactivation-request"
            accepted = await client.post(
                path, json={"reason": "Keep accepted operation"}
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["id"] == str(actor)
            assert accepted.json()["deactivation_requested_at"] is not None
            assert "deactivation_reason" not in accepted.json()
            assert (await client.post(path, json={})).status_code == 400
            cancelled = await client.delete(path)
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["deactivation_scheduled_at"] is None
            assert (await client.post(path, json={})).status_code == 200

            # Model an old accepted request becoming due without waiting seven days.
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "UPDATE users SET deactivation_requested_at=:requested, deactivation_scheduled_at=:scheduled WHERE id=:actor"
                    ),
                    {
                        "actor": actor,
                        "requested": datetime.now(UTC) - timedelta(days=8),
                        "scheduled": datetime.now(UTC) - timedelta(days=1),
                    },
                )
            monkeypatch.setattr(settings, "database_url", auth_dsn)
            monkeypatch.setattr(settings, "identity_url", origin)
            monkeypatch.setattr(settings, "identity_token", control)
            monkeypatch.setattr(settings, "identity_timeout_seconds", 15.0)
            monkeypatch.setattr(
                settings,
                "identity_deactivation_operator_database_url",
                "" if origin else _dsn("DEACTIVATION_OPERATOR", test_engine),
            )
            monkeypatch.setattr(
                settings, "identity_deactivation_operator_database_url_file", ""
            )
            monkeypatch.setattr(
                settings,
                "notification_publish_database_url",
                _dsn("NOTIFICATION_PUBLISH", test_engine),
            )
            monkeypatch.setattr(settings, "notification_publish_database_url_file", "")

            # Real Celery task entry points each construct and dispose a fresh loop/engine.
            for expected in (1, 0):
                run_id = uuid.uuid4()
                due_ids.append(uuid.uuid5(run_id, "batch:0"))

                def run_due():
                    return process_deactivation_requests.apply(
                        task_id=str(run_id), throw=True
                    ).get()

                assert await asyncio.to_thread(run_due) == {"processed": expected}

            def publish():
                return relay_identity_notifications.apply(throw=True).get()

            first = await asyncio.to_thread(publish)
            second = await asyncio.to_thread(publish)
            assert first["published"] == first["claimed"] == 5
            assert second["state"] == "idle" and second["claimed"] == 0
            assert (await client.post(path, json={})).status_code == 401

        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT has_table_privilege('identity_auth_client','public.users','SELECT')"
                    )
                )
                is False
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM identity_deactivation_operation_receipts WHERE actor_id=:actor"
                    ),
                    {"actor": actor},
                )
                == 3
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM notification_delivery_outbox WHERE state <> 'done'"
                    )
                )
                == 0
            )
        record_property(
            "deactivation_api_and_celery",
            binding
            + ",AUTH-only API database,two due task loops,two publisher task loops,no User hydration",
        )
    finally:
        if api is not None:
            api.stop()
        if identity is not None:
            identity.stop()
        await _cleanup_deactivation(
            maker, actor_ids=actor_ids, due_operation_ids=due_ids
        )


async def test_selected_remote_owner_without_deactivation_capability_fails_closed(
    test_engine, tmp_path
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = api = None
    try:
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
        )
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity.origin,
            database_url=_auth_dsn(test_engine),
        )
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            result = await client.post(
                api.origin + "/api/v1/auth/me/deactivation-request",
                headers={"Authorization": f"Bearer {_token(actor, secret)}"},
                json={},
            )
            assert result.status_code == 503, result.text
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM identity_deactivation_operation_receipts WHERE actor_id=:actor"
                    ),
                    {"actor": actor},
                )
                == 0
            )
    finally:
        if api is not None:
            api.stop()
        if identity is not None:
            identity.stop()
        await _cleanup_deactivation(maker, actor_ids=[actor])
