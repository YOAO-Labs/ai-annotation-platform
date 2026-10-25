"""Real notification frames and socket cleanup on verifier-owned PG/Redis."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uvicorn
from collections import Counter
import json
import os
from pathlib import Path
import secrets
import subprocess
import uuid

from fastapi import FastAPI
import pytest
import redis.asyncio as redis
from redis.exceptions import ResponseError
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import websockets

from app.db.models.audit_log import AuditLog
from app.api.v1 import ws
from app.core.security import create_access_token
from app.db.models.notification import Notification
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.identity_gateway import IdentityGateway
from app.services.notification import NotificationService
from tests.factory import create_membership, create_project, create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_sessions_live import _verify_redis


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned disposable PostgreSQL and Redis",
    ),
]


# Cleanup uses the verified test owner; this candidate has no standalone Audit package.
audit_events = AuditLog.__table__


async def _start_uvicorn(app: FastAPI):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            lifespan="off",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    task.result()
                    raise AssertionError("backend HTTP server stopped before startup")
                await asyncio.sleep(0.01)
    except BaseException:
        server.should_exit = True
        listener.close()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, timeout=5)
        raise
    return server, task, listener, f"http://127.0.0.1:{port}"


async def _stop_uvicorn(server, task, listener) -> None:
    if server is not None:
        server.should_exit = True
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=10)
        except asyncio.TimeoutError:
            server.force_exit = True
            await asyncio.wait_for(task, timeout=5)
    if listener is not None:
        listener.close()


def _socket_fixture_user_cleanup_statement(user_ids):
    audited_user = (
        select(1)
        .select_from(audit_events)
        .where(audit_events.c.actor_id == User.id)
        .correlate(User)
        .exists()
    )
    return delete(User).where(User.id.in_(user_ids), ~audited_user)


@pytest.fixture
async def socket_world(test_engine, request):
    _verify_postgres(test_engine.url)
    # Restore cases keep protected history until the verifier drops the owned DB.
    retain_history = getattr(request, "param", None) == "retain_history"
    seed = async_sessionmaker(test_engine, expire_on_commit=False)
    user_ids = []
    project_id = None
    try:
        async with seed.begin() as db:
            suffix = uuid.uuid4().hex
            owner = await create_user(
                db, "super_admin", f"owner-{suffix}@test.local", "Owner"
            )
            user_ids.append(owner.id)
            actor = await create_user(
                db, "employee", f"socket-{suffix}@test.local", "Socket"
            )
            user_ids.append(actor.id)
            project = await create_project(db, owner_id=owner.id)
            project_id = project.id
            membership = await create_membership(
                db,
                project_id=project.id,
                user_id=actor.id,
                role="reviewer",
                assigned_by=owner.id,
            )
        yield seed, actor, project, membership
    finally:
        if not retain_history:
            async with seed.begin() as db:
                if project_id is not None:
                    await db.execute(delete(Project).where(Project.id == project_id))
                if user_ids:
                    # Keep immutable Audit actors until disposable database teardown.
                    await db.execute(_socket_fixture_user_cleanup_statement(user_ids))


async def test_notification_socket_real_frames_revocation_and_cleanup(
    test_engine, socket_world, monkeypatch, record_property
):
    consumer_url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL"])
    auth_url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_AUTH_URL"])
    for url in (consumer_url, auth_url):
        assert (url.host, url.port, url.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
        _verify_postgres(url)
    assert consumer_url.username == "identity_api_consumer"
    assert auth_url.username == "identity_auth_client"
    redis_url = _verify_redis()
    seed, actor, project, membership = socket_world
    engine = create_async_engine(consumer_url, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.base.async_session", sessions)
    gateway = IdentityGateway(
        auth_database_url=auth_url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )
    )
    monkeypatch.setattr(ws, "get_identity_gateway", lambda: gateway)
    channel = f"notify:{actor.id}"
    acl_user, acl_password = (
        "notification-socket-" + uuid.uuid4().hex,
        secrets.token_urlsafe(32),
    )
    redis_owner = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    scoped_url = redis_url.set(
        username=acl_user, password=acl_password
    ).render_as_string(hide_password=False)
    pool = redis.ConnectionPool.from_url(scoped_url, max_connections=1)
    monkeypatch.setattr(ws, "_get_redis_pool", lambda: pool)
    app = FastAPI()
    app.include_router(ws.router)
    server = server_task = listener = None
    acl_attempted = False

    async def subscriptions(count):
        async with asyncio.timeout(5):
            while (await redis_owner.pubsub_numsub(channel))[0][1] != count:
                await asyncio.sleep(0.01)

    try:
        acl_attempted = True
        await redis_owner.execute_command(
            "ACL",
            "SETUSER",
            acl_user,
            "reset",
            "on",
            ">" + acl_password,
            "resetchannels",
            "&" + channel,
            "-@all",
            "+subscribe",
            "+unsubscribe",
            "+ping",
            "+select",
            "+client|setinfo",
        )
        async with redis.from_url(scoped_url) as scoped:
            assert await scoped.ping()
            with pytest.raises(ResponseError):
                await scoped.publish(channel, "denied")
            with pytest.raises(ResponseError):
                await scoped.get("private:notification-socket")
            async with scoped.pubsub() as forbidden:
                with pytest.raises(ResponseError):
                    await forbidden.subscribe(f"notify:{uuid.uuid4()}")
                    await forbidden.get_message(timeout=2)
        auth_engine = create_async_engine(auth_url, hide_parameters=True)
        try:
            async with auth_engine.connect() as connection:
                assert (
                    await connection.scalar(text("SELECT current_user"))
                    == "identity_auth_client"
                )
                assert not await connection.scalar(
                    text(
                        "SELECT has_any_column_privilege(current_user, 'public.users', 'SELECT')"
                    )
                )
        finally:
            await auth_engine.dispose()
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT current_user"))
                == "identity_api_consumer"
            )
            with pytest.raises(DBAPIError) as denied:
                await connection.execute(
                    text("SELECT * FROM public.notifications LIMIT 1")
                )
            assert denied.value.orig.sqlstate == "42501"

        server, server_task, listener, origin = await _start_uvicorn(app)
        token = create_access_token(str(actor.id), "employee")
        socket_url = (
            origin.replace("http://", "ws://") + "/ws/notifications?token=" + token
        )
        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(socket_url + "invalid"):
                pytest.fail("invalid JWT completed a socket handshake")

        async with websockets.connect(socket_url) as socket:
            await subscriptions(1)
            async with sessions() as db:
                service = NotificationService(db)
                notice = await service.notify(
                    user_id=actor.id,
                    type="task.assigned",
                    target_type="task",
                    target_id=uuid.uuid4(),
                    payload={"text": "跨语言", "count": 7},
                )
                assert notice is not None
                await db.commit()
                await service.publish_committed([notice])
                frame_bytes = await asyncio.wait_for(socket.recv(), 5)
                frame = json.loads(frame_bytes)
                assert frame == {
                    "id": str(notice.id),
                    "type": notice.type,
                    "target_type": notice.target_type,
                    "target_id": str(notice.target_id),
                    "payload": notice.payload,
                    "created_at": notice.created_at.isoformat(),
                }
                peer = (
                    Path(__file__).resolve().parents[3]
                    / "apps/notifications/conformance/node/realtime_event_provider.mjs"
                )
                node = await asyncio.to_thread(
                    subprocess.run,
                    ["node", str(peer), "--stdio"],
                    input=frame_bytes,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                assert node.returncode == 0, node.stderr
                assert node.stdout == frame_bytes
                await redis_owner.publish(channel, node.stdout)
                assert await asyncio.wait_for(socket.recv(), 5) == node.stdout
                legacy_payloads = [["legacy", None, 7], "legacy", None, 0, False]
                async with seed.begin() as fixture:
                    legacy_rows = [
                        Notification(
                            user_id=actor.id,
                            type="future.legacy",
                            target_type="task",
                            target_id=uuid.uuid4(),
                            payload=payload,
                        )
                        for payload in legacy_payloads
                    ]
                    fixture.add_all(legacy_rows)
                for legacy, payload in zip(legacy_rows, legacy_payloads):
                    assert await service.publish_by_id(legacy.id)
                    legacy_frame = json.loads(await asyncio.wait_for(socket.recv(), 5))
                    assert legacy_frame["id"] == str(legacy.id)
                    assert legacy_frame["payload"] == payload
                await service.publish_sync(actor.id, reason="future.reason")
                assert json.loads(await asyncio.wait_for(socket.recv(), 5)) == {
                    "type": "notifications.sync",
                    "reason": "future.reason",
                }

                pending = await service.notify(
                    user_id=actor.id,
                    type="task.rejected",
                    target_type="task",
                    target_id=uuid.uuid4(),
                    payload={"project_id": str(project.id)},
                )
                assert pending is not None
                original_lookup = NotificationService.notification_by_id
                missing = asyncio.Event()
                lookups = Counter()

                async def observe_lookup(self, notification_id):
                    row = await original_lookup(self, notification_id)
                    lookups[notification_id] += 1
                    if notification_id == pending.id and row is None:
                        missing.set()
                    return row

                monkeypatch.setattr(
                    NotificationService, "notification_by_id", observe_lookup
                )
                await service.publish_committed([pending])
                await asyncio.wait_for(missing.wait(), 5)
                await db.commit()
                restricted_frame = await asyncio.wait_for(socket.recv(), 5)
                assert json.loads(restricted_frame)["id"] == str(pending.id)
                assert lookups[pending.id] >= 2

            async with seed.begin() as db:
                await db.execute(
                    delete(ProjectMember).where(ProjectMember.id == membership.id)
                )
            await redis_owner.publish(channel, restricted_frame)
            await redis_owner.publish(
                channel,
                json.dumps(
                    {"type": "notifications.sync", "marker": "after-revocation"}
                ),
            )
            assert json.loads(await asyncio.wait_for(socket.recv(), 5)) == {
                "type": "notifications.sync",
                "marker": "after-revocation",
            }
            missing_id = uuid.uuid4()
            await redis_owner.publish(
                channel, json.dumps({"id": str(missing_id), "type": "future.notice"})
            )
            await redis_owner.publish(
                channel,
                json.dumps({"type": "notifications.sync", "marker": "after-missing"}),
            )
            assert (
                json.loads(await asyncio.wait_for(socket.recv(), 5))["marker"]
                == "after-missing"
            )
            assert lookups[missing_id] == 4
            async with seed.begin() as db:
                await db.execute(
                    update(User).where(User.id == actor.id).values(is_active=False)
                )
            await redis_owner.publish(
                channel,
                json.dumps({"type": "notifications.sync", "marker": "disabled"}),
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(socket.recv(), 0.3)
        await subscriptions(0)
        assert not pool._in_use_connections
        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(socket_url):
                pytest.fail("disabled account completed a socket handshake")

        async with seed.begin() as db:
            await db.execute(
                update(User).where(User.id == actor.id).values(is_active=True)
            )
        async with websockets.connect(socket_url) as socket:
            await subscriptions(1)
            handlers = [task for task in server.server_state.tasks if not task.done()]
            assert len(handlers) == 1
            handlers[0].cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(socket.recv(), 5)
        await subscriptions(0)
        assert not pool._in_use_connections
        record_property(
            "notification_socket_live",
            json.dumps(
                {
                    "actual_api_login": "identity_api_consumer",
                    "actual_auth_login": "identity_auth_client",
                    "subscribe_only_redis": True,
                    "ordinary_and_sync_frames": True,
                    "node_stdio_frame_through_real_socket": True,
                    "native_legacy_payload_variants": len(legacy_payloads),
                    "precommit_row_visibility_retry": True,
                    "revoked_membership_and_disabled_account_denied": True,
                    "missing_row_looks": lookups[missing_id],
                    "disconnect_and_handler_cancel_cleanup": True,
                }
            ),
        )
    finally:
        try:
            await _stop_uvicorn(server, server_task, listener)
        finally:
            try:
                await pool.disconnect(inuse_connections=True)
            finally:
                try:
                    if acl_attempted:
                        _verify_redis()
                        await redis_owner.execute_command("ACL", "DELUSER", acl_user)
                finally:
                    try:
                        await redis_owner.aclose()
                    finally:
                        await engine.dispose()
