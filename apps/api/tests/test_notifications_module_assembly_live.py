"""Cold API, Notifications publisher, and socket composition plus database restore."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from types import SimpleNamespace
import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import websockets

from aap_assets_source.import_delivery_contract import (
    decode_dataset_import_delivery_claim,
)
from aap_notifications.realtime_event_contract import notification_sync_message
from aap_notifications.websocket import (
    _NOTIFICATION_GATE_ATTEMPTS,
    _NOTIFICATION_GATE_RETRY_SECONDS,
)
from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.user import User
from tests.factory import DEFAULT_PASSWORD, create_membership
from tests.identity_module_assembly_helpers import IdentityModuleStack
from tests.identity_recovery_live_helpers import SMTPInbox
from tests.notifications_module_acceptance_helpers import (
    actor_socket_redis_url,
    api_redis_url,
    new_fernet_mail_key,
    required_module_inputs,
    verify_notification_publish_database_role,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_deactivation_live import _notification_rows
from tests.test_identity_module_assembly_live import (
    _publisher_redis,
    _restored_database,
)
from tests.test_identity_recovery import _postgres_tool
from tests.test_identity_sessions_live import _login_redis
from tests.test_notifications_publisher_image_live import (
    _create_deactivation,
    _delivery_rows,
    _expire_dead_leases,
    _publisher_environment,
)
from tests.test_notifications_socket_runtime_live import (
    _assert_role_capabilities,
    asyncpg_connection_url,
    _expect_rejected_socket,
    _wait_for_subscribers,
    _websocket_url,
)

pytest_plugins = ("tests.test_notifications_socket_live",)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned disposable PostgreSQL and Redis",
    ),
]


async def _request(client, method, path, *, expected=200, **kwargs):
    response = await client.request(method, path, **kwargs)
    assert response.status_code == expected, f"{method} {path}: {response.status_code}"
    return response.json() if response.content else None


async def _next_published(pubsub, *, timeout=8):
    async with asyncio.timeout(timeout):
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=0.2
            )
            if message and message.get("type") == "message":
                return json.loads(message["data"])


async def _assert_no_published(pubsub, *, timeout=0.5):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        message = await pubsub.get_message(
            ignore_subscribe_messages=True,
            timeout=min(0.1, max(0, deadline - asyncio.get_running_loop().time())),
        )
        assert not (message and message.get("type") == "message")


async def _publish_from_node_consumer(
    stack, api_container_id, token, request, *, expected_published
):
    raw_request = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    provider = (
        Path(__file__).resolve().parents[3]
        / "apps/notifications/conformance/node/publication_provider.mjs"
    )
    environment = {
        name: os.environ[name]
        for name in (
            "PATH",
            "HOME",
            "DOCKER_CONFIG",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "XDG_CONFIG_HOME",
        )
        if name in os.environ
    }
    environment.update(
        {
            "AAP_PUBLICATION_PEER_CONTAINER": api_container_id,
            "AAP_PUBLICATION_PEER_PROJECT": stack.project_name,
            "AAP_PUBLICATION_PEER_ORIGIN": (
                "http://identity-notification-publisher:8181"
            ),
            "AAP_PUBLICATION_PEER_TOKEN": token,
            "AAP_PUBLICATION_PEER_EXPECTED": str(expected_published).lower(),
        }
    )
    result = await asyncio.to_thread(
        subprocess.run,
        ["node", str(provider), "--container-http"],
        input=raw_request,
        capture_output=True,
        timeout=45,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    response = json.loads(result.stdout)
    assert response["protocol"] == "aap-notifications-immediate-publication/v1"
    assert response["published"] is expected_published
    assert response["request_sha256"] == hashlib.sha256(raw_request).hexdigest()
    assert response["transport"] == "owned-container HTTP byte bridge, no owner SDK"
    return response


async def _feedback_issue(client, *, project_id, body):
    result = await _request(
        client,
        "POST",
        "/api/v1/feedbacks",
        json={
            "kind": "issue",
            "anchor_type": "project",
            "project_id": str(project_id),
            "body": body,
        },
    )
    return uuid.UUID(result["id"])


def _batch_predict_terminal_job(job_id, *, user_id, project_id):
    return AsyncJob(
        id=job_id,
        kind="batch_predict",
        user_id=user_id,
        project_id=project_id,
        status="completed",
        payload={},
        result={},
    )


async def _feedback_reply(client, issue_id, *, body, headers=None):
    result = await _request(
        client,
        "POST",
        f"/api/v1/feedbacks/{issue_id}/replies",
        json={"body": body},
        headers=headers,
    )
    return uuid.UUID(result["id"])


async def _listed_reply(client, reply_id):
    result = await _request(client, "GET", "/api/v1/notifications?limit=100")
    return next(
        item
        for item in result["items"]
        if item["type"] == "feedback.reply_created"
        and item["payload"].get("reply_id") == str(reply_id)
    )


def _terminal_script(job_id: uuid.UUID, *, commit: bool, delay: float) -> str:
    finish = (
        "await db.commit()"
        if commit
        else "await savepoint.rollback()\n        await db.rollback()"
    )
    savepoint = "savepoint = await db.begin_nested()\n        " if not commit else ""
    return f"""import asyncio
from uuid import UUID
from app.services.async_job_notify import notify_job_terminal
from app.workers._db import task_session

async def main():
    async with task_session() as db:
        {savepoint}await notify_job_terminal(db, job_id=UUID({str(job_id)!r}))
        await asyncio.sleep({delay!r})
        {finish}

asyncio.run(main())
"""


async def _run_terminal_in_api(stack, job_id, *, commit, delay):
    source = _terminal_script(job_id, commit=commit, delay=delay)
    await asyncio.to_thread(
        stack._compose,
        "exec",
        "-T",
        "identity-api",
        "python",
        "-c",
        source,
        timeout=45,
    )


async def _append_deferred_notification_in_api(stack, *, user_id, issue_id, project_id):
    source = f"""import asyncio
from uuid import UUID
from app.services.notification import NotificationService
from app.workers._db import task_session

async def main():
    async with task_session() as db:
        row = await NotificationService(db).notify(
            user_id=UUID({str(user_id)!r}),
            type="feedback.reply_created",
            target_type="feedback",
            target_id=UUID({str(issue_id)!r}),
            payload={{"project_id": {str(project_id)!r}, "source": "feedback"}},
        )
        if row is None:
            raise RuntimeError("deferred Notifications append was muted")
        await db.commit()
        print(str(row.id))

asyncio.run(main())
"""
    return uuid.UUID(
        await asyncio.to_thread(
            stack._compose,
            "exec",
            "-T",
            "identity-api",
            "python",
            "-c",
            source,
            timeout=45,
        )
    )


async def _publish_committed_in_api(stack, notification_id):
    source = f"""import asyncio
from uuid import UUID
from app.services.notification import NotificationService
from app.workers._db import task_session

async def main():
    async with task_session() as db:
        service = NotificationService(db)
        row = await service.notification_by_id(UUID({str(notification_id)!r}))
        if row is None:
            raise RuntimeError("deferred Notifications row is missing")
        await service.publish_committed([row])
        print("processed")

asyncio.run(main())
"""
    return await asyncio.to_thread(
        stack._compose,
        "exec",
        "-T",
        "identity-api",
        "python",
        "-c",
        source,
        timeout=45,
    )


async def _deliver_notification_import_in_api(stack, delivery):
    source = """import asyncio, json, sys
from aap_assets_source.import_delivery_contract import DatasetImportDelivery
from aap_notifications.publication_contract import ImmediatePublicationUnavailable
from app.workers._db import task_session
from app.workers.dataset_import_delivery import _deliver_one

async def main():
    delivery = DatasetImportDelivery.model_validate(json.loads(sys.argv[1]))
    async with task_session() as db:
        try:
            await _deliver_one(db, delivery, None)
        except ImmediatePublicationUnavailable:
            print("deferred")
            return
        raise RuntimeError("unavailable Notifications owner was acknowledged")

asyncio.run(main())
"""
    output = await asyncio.to_thread(
        stack._compose,
        "exec",
        "-T",
        "identity-api",
        "python",
        "-c",
        source,
        delivery.model_dump_json(),
        timeout=60,
    )
    return output


async def _claim_notification_import_delivery(test_engine, notification_id):
    connection = await asyncpg.connect(
        asyncpg_connection_url(test_engine.url).render_as_string(hide_password=False),
        timeout=5,
    )
    job_id = uuid.uuid4()
    lease_token = uuid.uuid4()
    try:
        await connection.fetchval(
            "SELECT aap_assets_v1.queue_dataset_import_delivery($1::uuid, 'notification', $2::uuid)",
            job_id,
            notification_id,
        )
        raw_claim = await connection.fetchval(
            "SELECT aap_assets_v1.claim_dataset_import_delivery($1::uuid, 120)",
            lease_token,
        )
        claim = json.loads(raw_claim) if isinstance(raw_claim, str) else raw_claim
        delivery = decode_dataset_import_delivery_claim(claim)
        assert delivery is not None
        assert delivery.target_id == notification_id
        return delivery
    finally:
        await connection.close(timeout=3)


async def _wait_for_delivery_state(maker, operation_id, state, *, timeout=20):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        rows = await _delivery_rows(maker, operation_id)
        if rows and all(row["state"] == state for row in rows):
            return rows
        await asyncio.sleep(0.1)
    raise AssertionError(f"Notifications outbox did not reach {state}")


@pytest.mark.asyncio
async def test_notifications_module_cold_composition_and_full_restore(
    test_engine, socket_world, tmp_path, record_property
):
    inputs = required_module_inputs()
    publisher_image = _publisher_environment()
    seed, actor, project, membership = socket_world
    async with seed() as db:
        owner = await db.get(User, project.owner_id)
    assert owner is not None
    owner_id = owner.id

    _verify_postgres(test_engine.url)
    await verify_notification_publish_database_role(
        inputs["AAP_MODULE_DATA_IDENTITY_NOTIFICATION_PUBLISH_URL"],
        reference_url=test_engine.url,
    )
    await _assert_role_capabilities(
        inputs["AAP_MODULE_DATA_IDENTITY_ACTOR_STATUS_URL"],
        expected_role="identity_actor_status_client",
        actor_id=actor.id,
    )
    await _assert_role_capabilities(
        inputs["AAP_MODULE_DATA_NOTIFICATION_DELIVERY_READ_URL"],
        expected_role="notification_delivery_read_client",
        actor_id=actor.id,
    )

    identity_secret, api_secret, control, admission_token, publication_token = (
        secrets.token_urlsafe(32) for _ in range(5)
    )
    inbox = SMTPInbox()
    stack = None
    pubsub = None
    publisher_admin = None
    socket = None
    diagnostics_captured = False
    snapshot = tmp_path / "notifications-module.dump"
    operation_ids = []
    try:
        async with (
            _login_redis() as (login_redis_url, _login_owner),
            _publisher_redis() as (publisher_redis_url, publisher_admin),
            api_redis_url((actor.id, owner_id)) as api_redis_dsn,
            actor_socket_redis_url(actor.id) as socket_redis_dsn,
        ):
            stack = IdentityModuleStack(
                test_engine,
                tmp_path,
                secret=identity_secret,
                api_secret=api_secret,
                control=control,
                mail_key=new_fernet_mail_key(),
                login_redis_url=login_redis_url,
                publisher_redis_url=publisher_redis_url,
                smtp_port=inbox.port,
                notifications_socket_image=inputs[
                    "AAP_NOTIFICATIONS_SOCKET_RUNTIME_IMAGE"
                ],
                notifications_socket_identity=inputs[
                    "AAP_NOTIFICATIONS_SOCKET_RUNTIME_IDENTITY"
                ],
                notification_delivery_database_url=inputs[
                    "AAP_MODULE_DATA_NOTIFICATION_DELIVERY_READ_URL"
                ],
                identity_actor_status_database_url=inputs[
                    "AAP_MODULE_DATA_IDENTITY_ACTOR_STATUS_URL"
                ],
                notifications_socket_redis_url=socket_redis_dsn,
                admission_token=admission_token,
                notifications_publisher_image=inputs[
                    "AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IMAGE"
                ],
                notifications_publisher_identity=inputs[
                    "AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IDENTITY"
                ],
                notification_publication_token=publication_token,
                notification_api_redis_url=api_redis_dsn,
            )
            try:
                await stack.start()
                evidence = stack.inspection_evidence
                publisher = evidence["identity-notification-publisher"]
                socket_evidence = evidence["notifications-socket"]
                api_evidence = evidence["identity-api"]
                assert publisher["image_id"] == publisher_image["id"]
                assert publisher["user"] == "10004:10004"
                assert publisher["running"] is True
                assert publisher["read_only"] is True
                assert publisher["health"] == "healthy"
                assert {
                    "/run/secrets/notification_publish_database_url",
                    "/run/secrets/notification_publish_redis_url",
                    "/run/secrets/notification_publication_token",
                } <= set(publisher["secret_mount_targets"])
                publisher_environment = set(publisher["environment_names"])
                assert {
                    "NOTIFICATION_PUBLISH_API_TOKEN_FILE",
                    "NOTIFICATION_PUBLISH_HOST",
                    "NOTIFICATION_PUBLISH_PORT",
                } <= publisher_environment
                assert "NOTIFICATION_PUBLISH_API_TOKEN" not in publisher_environment
                assert socket_evidence["user"] == "10004:10004"
                assert socket_evidence["running"] is True
                assert socket_evidence["health"] == "healthy"
                assert api_evidence["user"] == "10003:10003"
                assert "/run/secrets/notification_publication_token" in set(
                    api_evidence["secret_mount_targets"]
                )
                assert "NOTIFICATION_PUBLICATION_TOKEN_FILE" in set(
                    api_evidence["environment_names"]
                )
                assert "NOTIFICATION_PUBLICATION_TOKEN" not in set(
                    api_evidence["environment_names"]
                )
                containers = stack._verify_project_ownership()
                for service in (
                    "identity-notification-publisher",
                    "notifications-socket",
                ):
                    ports = (containers[service].get("HostConfig") or {}).get(
                        "PortBindings"
                    ) or {}
                    assert not any(ports.values())

                async with httpx.AsyncClient(
                    base_url=stack.api_origin,
                    trust_env=False,
                    timeout=30,
                ) as api:
                    actor_login = await _request(
                        api,
                        "POST",
                        "/api/v1/auth/login",
                        json={"email": actor.email, "password": DEFAULT_PASSWORD},
                    )
                    actor_token = actor_login["access_token"]
                    owner_login = await _request(
                        api,
                        "POST",
                        "/api/v1/auth/login",
                        json={"email": owner.email, "password": DEFAULT_PASSWORD},
                    )
                    owner_headers = {
                        "Authorization": "Bearer " + owner_login["access_token"]
                    }
                    api.headers["Authorization"] = "Bearer " + actor_token
                    await _expect_rejected_socket(
                        _websocket_url(stack.api_origin, "not-a-jwt")
                    )

                    channel = f"notify:{actor.id}"
                    owner_subscriber = publisher_admin.pubsub()
                    pubsub = owner_subscriber
                    await owner_subscriber.subscribe(channel)
                    await _wait_for_subscribers(publisher_admin, channel, 1)
                    socket = await websockets.connect(
                        _websocket_url(stack.api_origin, actor_token),
                        proxy=None,
                        open_timeout=10,
                        close_timeout=3,
                        ping_interval=None,
                        max_size=None,
                    )
                    await _wait_for_subscribers(publisher_admin, channel, 2)
                    api_container_id = containers["identity-api"]["Id"]
                    try:
                        node_sync = {
                            "type": "notifications.sync",
                            "reason": "node_peer",
                            "native": [9007199254740993, None, False, "中文"],
                        }
                        positive_request = {
                            "schema": "aap-notifications-immediate-publication/v1",
                            "user_id": str(actor.id),
                            "authorization": None,
                            "message": node_sync,
                        }
                        positive_result = await _publish_from_node_consumer(
                            stack,
                            api_container_id,
                            publication_token,
                            positive_request,
                            expected_published=True,
                        )
                        observer_event = await _next_published(owner_subscriber)
                        socket_event = json.loads(
                            await asyncio.wait_for(socket.recv(), 5)
                        )
                        assert positive_result["published"] is True
                        assert observer_event == node_sync
                        assert socket_event == node_sync
                        assert observer_event["native"][0] == 9007199254740993

                        denied_id = uuid.uuid4()
                        denied_request = {
                            "schema": "aap-notifications-immediate-publication/v1",
                            "user_id": str(actor.id),
                            "authorization": {
                                "restricted": True,
                                "project_id": None,
                                "export_required": False,
                                "member": False,
                                "reviewer": False,
                                "owner": False,
                                "super_admin": False,
                            },
                            "message": {
                                "id": str(denied_id),
                                "type": "feedback.reply_created",
                                "target_type": "feedback_reply",
                                "target_id": str(uuid.uuid4()),
                                "payload": {"reply_id": str(denied_id)},
                                "created_at": datetime.now(timezone.utc).isoformat(),
                            },
                        }
                        denied_result = await _publish_from_node_consumer(
                            stack,
                            api_container_id,
                            publication_token,
                            denied_request,
                            expected_published=False,
                        )
                        assert denied_result["published"] is False
                        await _assert_no_published(owner_subscriber)
                        with pytest.raises(TimeoutError):
                            await asyncio.wait_for(socket.recv(), 0.5)

                        preferences = await _request(
                            api, "GET", "/api/v1/notification-preferences"
                        )
                        assert preferences["items"]
                        await _request(
                            api,
                            "PUT",
                            "/api/v1/notification-preferences",
                            json={
                                "type": "feedback.reply_created",
                                "in_app": True,
                                "toast": False,
                            },
                        )
                        preference_sync = await _next_published(owner_subscriber)
                        assert preference_sync == notification_sync_message(
                            reason="preferences"
                        )
                        assert json.loads(await asyncio.wait_for(socket.recv(), 5)) == (
                            preference_sync
                        )

                        issue_id = await _feedback_issue(
                            api,
                            project_id=project.id,
                            body="Notifications cold composition root",
                        )
                        first_reply_id = await _feedback_reply(
                            api,
                            issue_id,
                            body="Owner-published reply one",
                            headers=owner_headers,
                        )
                        first_event = await _next_published(owner_subscriber)
                        first_frame = json.loads(
                            await asyncio.wait_for(socket.recv(), 5)
                        )
                        assert first_event == first_frame
                        assert first_event["type"] == "feedback.reply_created"
                        assert first_event["payload"]["reply_id"] == str(first_reply_id)
                        first_notice = await _listed_reply(api, first_reply_id)
                        first_notice_id = uuid.UUID(first_notice["id"])

                        unread = await _request(
                            api, "GET", "/api/v1/notifications/unread-count"
                        )
                        assert unread["unread"] >= 1
                        await _request(
                            api,
                            "POST",
                            f"/api/v1/notifications/{first_notice_id}/read",
                        )
                        read_sync = await _next_published(owner_subscriber)
                        assert read_sync == notification_sync_message(reason="read")
                        assert json.loads(await asyncio.wait_for(socket.recv(), 5)) == (
                            read_sync
                        )
                        listed = await _request(
                            api, "GET", "/api/v1/notifications?limit=100"
                        )
                        saved_read = next(
                            row
                            for row in listed["items"]
                            if row["id"] == str(first_notice_id)
                        )
                        assert saved_read["read_at"] is not None

                        deleted_reply_id = await _feedback_reply(
                            api,
                            issue_id,
                            body="Owner-published reply to delete",
                            headers=owner_headers,
                        )
                        deleted_event = await _next_published(owner_subscriber)
                        assert deleted_event["payload"]["reply_id"] == str(
                            deleted_reply_id
                        )
                        assert json.loads(await asyncio.wait_for(socket.recv(), 5)) == (
                            deleted_event
                        )
                        deleted_notice = await _listed_reply(api, deleted_reply_id)
                        deleted_notice_id = uuid.UUID(deleted_notice["id"])
                        await _request(
                            api,
                            "DELETE",
                            f"/api/v1/notifications/{deleted_notice_id}",
                        )
                        deleted_sync = await _next_published(owner_subscriber)
                        assert deleted_sync == notification_sync_message(
                            reason="deleted"
                        )
                        assert json.loads(await asyncio.wait_for(socket.recv(), 5)) == (
                            deleted_sync
                        )

                        async with seed.begin() as db:
                            await db.execute(
                                text("DELETE FROM project_members WHERE id=:id"),
                                {"id": membership.id},
                            )
                        denied_notice_id = await _append_deferred_notification_in_api(
                            stack,
                            user_id=actor.id,
                            issue_id=issue_id,
                            project_id=project.id,
                        )
                        assert (
                            await _publish_committed_in_api(stack, denied_notice_id)
                            == "processed"
                        )
                        denied_inbox = await _request(
                            api, "GET", "/api/v1/notifications?limit=100"
                        )
                        assert not any(
                            item["id"] == str(denied_notice_id)
                            for item in denied_inbox["items"]
                        )
                        async with seed() as db:
                            assert (
                                await db.scalar(
                                    select(Notification.id).where(
                                        Notification.id == denied_notice_id
                                    )
                                )
                                == denied_notice_id
                            )
                        await _assert_no_published(owner_subscriber, timeout=0.6)
                        with pytest.raises(TimeoutError):
                            await asyncio.wait_for(socket.recv(), 0.6)

                        async with seed.begin() as db:
                            await create_membership(
                                db,
                                project_id=project.id,
                                user_id=actor.id,
                                role="reviewer",
                                assigned_by=owner_id,
                            )

                        leased_reply_id = await _feedback_reply(
                            api,
                            issue_id,
                            body="Reply handed to the import delivery relay",
                            headers=owner_headers,
                        )
                        leased_event = await _next_published(owner_subscriber)
                        assert leased_event["payload"]["reply_id"] == str(
                            leased_reply_id
                        )
                        assert json.loads(await asyncio.wait_for(socket.recv(), 5)) == (
                            leased_event
                        )
                        leased_notice = await _listed_reply(api, leased_reply_id)
                        leased_notice_id = uuid.UUID(leased_notice["id"])
                        delivery = await _claim_notification_import_delivery(
                            test_engine, leased_notice_id
                        )
                        await stack.stop_service("identity-notification-publisher")
                        assert (
                            await _deliver_notification_import_in_api(stack, delivery)
                            == "deferred"
                        )
                        async with seed() as db:
                            delivery_state = (
                                await db.execute(
                                    text(
                                        "SELECT attempts, lease_token, delivered_at "
                                        "FROM public.dataset_import_deliveries WHERE id=:id"
                                    ),
                                    {"id": delivery.delivery_id},
                                )
                            ).one()
                        assert delivery_state.attempts == 1
                        assert delivery_state.lease_token == delivery.lease_token
                        assert delivery_state.delivered_at is None

                        failed_reply_id = await _feedback_reply(
                            api,
                            issue_id,
                            body="Best-effort reply while owner is offline",
                            headers=owner_headers,
                        )
                        failed_notice = await _listed_reply(api, failed_reply_id)
                        failed_notice_id = uuid.UUID(failed_notice["id"])
                        async with seed() as db:
                            outbox_count = await db.scalar(
                                text(
                                    "SELECT count(*) FROM public.notification_delivery_outbox "
                                    "WHERE notification_id=:id"
                                ),
                                {"id": failed_notice_id},
                            )
                        assert outbox_count == 0
                        await stack.start_service("identity-notification-publisher")
                        await _assert_no_published(owner_subscriber, timeout=0.6)
                        with pytest.raises(TimeoutError):
                            await asyncio.wait_for(socket.recv(), 0.6)

                        commit_job_id = uuid.uuid4()
                        rollback_job_id = uuid.uuid4()
                        terminal_jobs = [
                            _batch_predict_terminal_job(
                                job_id, user_id=actor.id, project_id=project.id
                            )
                            for job_id in (commit_job_id, rollback_job_id)
                        ]
                        assert all(
                            job.project_id == project.id for job in terminal_jobs
                        )
                        async with seed.begin() as db:
                            db.add_all(terminal_jobs)
                        precommit_started = asyncio.get_running_loop().time()
                        await _run_terminal_in_api(
                            stack, commit_job_id, commit=True, delay=0.20
                        )
                        terminal_event = await _next_published(owner_subscriber)
                        terminal_frame = json.loads(
                            await asyncio.wait_for(socket.recv(), 5)
                        )
                        assert terminal_event == terminal_frame
                        assert terminal_event["type"] == "job.completed"
                        assert terminal_event["target_id"] == str(commit_job_id)
                        assert (
                            asyncio.get_running_loop().time() - precommit_started
                            >= 0.18
                        )
                        assert _NOTIFICATION_GATE_ATTEMPTS == 4
                        assert _NOTIFICATION_GATE_RETRY_SECONDS == 0.15

                        await _run_terminal_in_api(
                            stack, rollback_job_id, commit=False, delay=0.65
                        )
                        rollback_event = await _next_published(owner_subscriber)
                        assert rollback_event["type"] == "job.completed"
                        assert rollback_event["target_id"] == str(rollback_job_id)
                        with pytest.raises(TimeoutError):
                            await asyncio.wait_for(socket.recv(), 0.3)
                        async with seed() as db:
                            assert (
                                await db.scalar(
                                    select(Notification.id).where(
                                        Notification.target_id == rollback_job_id
                                    )
                                )
                                is None
                            )

                        await socket.close()
                        socket = None
                        await _wait_for_subscribers(publisher_admin, channel, 1)

                        await stack.stop_service("identity-notification-publisher")
                        operation_id, pending_rows = await _create_deactivation(
                            async_sessionmaker(test_engine, expire_on_commit=False),
                            SimpleNamespace(origin=stack.identity_origin),
                            control,
                            actor.id,
                            identity_secret,
                            operation_ids,
                            reason="notifications module restore lease",
                        )
                        assert len(pending_rows) == 2
                        notification_ids = [
                            uuid.UUID(row["id"]) for row in pending_rows
                        ]
                        async with seed() as db:
                            pending_notifications = list(
                                (
                                    await db.scalars(
                                        select(Notification).where(
                                            Notification.id.in_(notification_ids)
                                        )
                                    )
                                ).all()
                            )
                        recipient_ids = {row.user_id for row in pending_notifications}
                        actor_notification_ids = {
                            str(row.id)
                            for row in pending_notifications
                            if row.user_id == actor.id
                        }
                        other_recipient_notification_ids = {
                            str(row.id)
                            for row in pending_notifications
                            if row.user_id != actor.id
                        }
                        assert actor_notification_ids
                        assert other_recipient_notification_ids
                        assert actor_notification_ids.isdisjoint(
                            other_recipient_notification_ids
                        )
                        assert (
                            actor_notification_ids | other_recipient_notification_ids
                        ) == {str(value) for value in notification_ids}
                        await owner_subscriber.subscribe(
                            *(f"notify:{value}" for value in recipient_ids)
                        )

                        publisher_dsn = inputs[
                            "AAP_MODULE_DATA_IDENTITY_NOTIFICATION_PUBLISH_URL"
                        ]
                        claim_connection = await asyncpg.connect(
                            asyncpg_connection_url(publisher_dsn).render_as_string(
                                hide_password=False
                            ),
                            timeout=5,
                        )
                        worker_id = uuid.uuid4()
                        try:
                            raw_claim = await claim_connection.fetchval(
                                "SELECT aap_notifications_v1.claim_identity_deliveries($1::jsonb)",
                                json.dumps(
                                    {
                                        "schema": "aap-notifications-delivery/v1",
                                        "worker_id": str(worker_id),
                                        "limit": 10,
                                        "lease_seconds": 120,
                                    }
                                ),
                            )
                            claim = (
                                json.loads(raw_claim)
                                if isinstance(raw_claim, str)
                                else raw_claim
                            )
                            assert claim["ok"] is True
                            assert {
                                uuid.UUID(item["notification_id"])
                                for item in claim["data"]["items"]
                            } == set(notification_ids)
                        finally:
                            await claim_connection.close(timeout=3)

                        leased_rows = await _notification_rows(
                            async_sessionmaker(test_engine, expire_on_commit=False),
                            operation_id,
                        )
                        assert len(leased_rows) == 2
                        assert all(
                            row["state"] == "pending" and row["lease_id"] is not None
                            for row in leased_rows
                        )

                        await api.aclose()
                        await stack.stop()
                        snapshot.touch(mode=0o600)
                        snapshot.write_bytes(
                            await asyncio.to_thread(
                                _postgres_tool,
                                test_engine,
                                [
                                    "pg_dump",
                                    "-U",
                                    "owner",
                                    "-Fc",
                                    test_engine.url.database,
                                ],
                            )
                        )
                        assert snapshot.read_bytes().startswith(b"PGDMP")
                        record_property(
                            "notifications_restore_backup_sha256",
                            hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                        )

                        async with _restored_database(test_engine, snapshot) as clone:
                            clone_engine = create_async_engine(
                                test_engine.url.set(database=clone),
                                hide_parameters=True,
                            )
                            clone_maker = async_sessionmaker(
                                clone_engine, expire_on_commit=False
                            )
                            try:
                                await _expire_dead_leases(
                                    clone_maker,
                                    operation_id,
                                    expected_count=len(notification_ids),
                                )
                                await stack.switch_database(clone)
                                done_rows = await _wait_for_delivery_state(
                                    clone_maker, operation_id, "done"
                                )
                                assert {
                                    uuid.UUID(row["notification_id"])
                                    for row in done_rows
                                } == set(notification_ids)
                                assert all(
                                    row["attempt"] == 2
                                    and row["last_reason"] == "published"
                                    and row["lease_id"] is None
                                    for row in done_rows
                                )
                                restored_events = []
                                async with asyncio.timeout(12):
                                    while len(
                                        {
                                            uuid.UUID(event["id"])
                                            for event in restored_events
                                            if event.get("id")
                                        }
                                        & set(notification_ids)
                                    ) < len(notification_ids):
                                        event = await _next_published(owner_subscriber)
                                        if event.get("id"):
                                            restored_events.append(event)
                                assert {
                                    uuid.UUID(event["id"])
                                    for event in restored_events
                                    if event.get("id")
                                } >= set(notification_ids)

                                async with httpx.AsyncClient(
                                    base_url=stack.api_origin,
                                    trust_env=False,
                                    timeout=30,
                                    headers={"Authorization": "Bearer " + actor_token},
                                ) as restored_api:
                                    restored_preferences = await _request(
                                        restored_api,
                                        "GET",
                                        "/api/v1/notification-preferences",
                                    )
                                    saved_preference = next(
                                        item
                                        for item in restored_preferences["items"]
                                        if item["type"] == "feedback.reply_created"
                                    )
                                    assert saved_preference["in_app"] is True
                                    assert saved_preference["toast"] is False
                                    restored_inbox = await _request(
                                        restored_api,
                                        "GET",
                                        "/api/v1/notifications?limit=100",
                                    )
                                    restored_by_id = {
                                        item["id"]: item
                                        for item in restored_inbox["items"]
                                    }
                                    assert str(first_notice_id) in restored_by_id
                                    assert (
                                        restored_by_id[str(first_notice_id)]["read_at"]
                                        is not None
                                    )
                                    assert str(deleted_notice_id) not in restored_by_id
                                    restored_ids = set(restored_by_id)
                                    assert actor_notification_ids <= restored_ids
                                    assert other_recipient_notification_ids.isdisjoint(
                                        restored_ids
                                    )

                                    async with websockets.connect(
                                        _websocket_url(stack.api_origin, actor_token),
                                        proxy=None,
                                        open_timeout=10,
                                        close_timeout=3,
                                        ping_interval=None,
                                        max_size=None,
                                    ) as restored_socket:
                                        await _wait_for_subscribers(
                                            publisher_admin, channel, 2
                                        )
                                        new_reply_id = await _feedback_reply(
                                            restored_api,
                                            issue_id,
                                            body="Ordinary reply after restore",
                                            headers=owner_headers,
                                        )
                                        new_event = await _next_published(
                                            owner_subscriber
                                        )
                                        new_frame = json.loads(
                                            await asyncio.wait_for(
                                                restored_socket.recv(), 5
                                            )
                                        )
                                        assert new_event == new_frame
                                        assert new_event["type"] == (
                                            "feedback.reply_created"
                                        )
                                        assert new_event["payload"]["reply_id"] == str(
                                            new_reply_id
                                        )
                                        await _listed_reply(restored_api, new_reply_id)
                            finally:
                                await stack.stop()
                                await clone_engine.dispose()

                        record_property(
                            "notifications_module_cold_restore",
                            json.dumps(
                                {
                                    "publisher_source_revision": publisher_image[
                                        "source_revision"
                                    ],
                                    "api_same_origin_socket_bridge": True,
                                    "node_consumer_publication_byte_preserved": True,
                                    "node_consumer_scope_denial_suppressed": True,
                                    "ordinary_api_publication": True,
                                    "preference_read_and_sync": True,
                                    "notification_read_delete_and_sync": True,
                                    "current_scope_revocation": True,
                                    "ordinary_failure_best_effort_no_outbox": True,
                                    "precommit_terminal_publication": True,
                                    "terminal_rollback_dropped_by_gate": True,
                                    "gate_attempts": _NOTIFICATION_GATE_ATTEMPTS,
                                    "restored_durable_ids": [
                                        str(value) for value in notification_ids
                                    ],
                                    "restored_owner_attempt": 2,
                                    "new_ordinary_event_after_restore": True,
                                },
                                sort_keys=True,
                            ),
                        )
                    except Exception:
                        diagnostics_captured = True
                        if stack is not None:
                            with suppress(Exception):
                                record_property(
                                    "runtime_diagnostics", stack.capture_diagnostics()
                                )
                        raise
                    finally:
                        if socket is not None:
                            with suppress(Exception):
                                await socket.close()
                        await api.aclose()
                        await stack.stop()
            except Exception:
                if stack is not None and not diagnostics_captured:
                    with suppress(Exception):
                        record_property(
                            "runtime_diagnostics", stack.capture_diagnostics()
                        )
                raise
            finally:
                if pubsub is not None:
                    await pubsub.aclose()
                await stack.stop()
    finally:
        inbox.close()
        if stack is not None:
            await stack.stop()
