"""Cold Notifications owner behind the existing public API WebSocket binding."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
import secrets
import subprocess
import time
import uuid
from urllib.parse import urlencode, urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import redis.asyncio as redis
from redis.exceptions import ResponseError
from sqlalchemy import delete, update
from sqlalchemy.engine import URL, make_url
import websockets

from aap_notifications.realtime_event_contract import notification_event_message
from aap_notifications.websocket import (
    _NOTIFICATION_GATE_ATTEMPTS,
    _NOTIFICATION_GATE_RETRY_SECONDS,
)
from app.db.models.notification import Notification
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import DEFAULT_PASSWORD, create_api_key
from tests.identity_module_assembly_helpers import (
    IdentityModuleStack,
    _expected_secret_mount_names,
    _identity_optional_secret_selectors,
    verifier_database_url,
)
from tests.identity_recovery_live_helpers import SMTPInbox
from tests.test_identity_admission_live import ROOT
from tests.test_identity_module_assembly_live import _publisher_redis
from tests.test_identity_sessions_live import _login_redis, _verify_redis


pytest_plugins = ("tests.test_notifications_socket_live",)


_NOTIFICATION_CAPABILITIES = {
    ("read_notification", "uuid"),
    ("find_terminal_notification", "uuid, text, uuid"),
    ("delivery_job_scopes", "uuid[]"),
    ("delivery_member_pairs", "jsonb"),
    ("delivery_reviewer_pairs", "jsonb"),
    ("delivery_owner_pairs", "jsonb"),
    ("delivery_super_pairs", "jsonb"),
}
_HEARTBEAT_SECONDS = 30


@pytest.mark.parametrize(
    ("role", "container"),
    [
        ("notification_delivery_read_client", "owned-postgres"),
        ("identity_actor_status_client", "owned-postgres"),
    ],
)
def test_verifier_role_database_url_is_bound_to_owned_test_database(role, container):
    reference = URL.create(
        "postgresql",
        username="test_owner",
        password="owner-secret",
        host="127.0.0.1",
        port=55432,
        database="notifications_fixture_test",
    )
    supplied = URL.create(
        "postgresql",
        username=role,
        password="scoped-secret",
        host="127.0.0.1",
        port=55432,
        database="notifications_fixture_test",
    )

    bound = verifier_database_url(
        supplied.render_as_string(hide_password=False),
        reference,
        expected_role=role,
        container_name=container,
    )

    assert (bound.host, bound.port, bound.database, bound.username) == (
        container,
        5432,
        "notifications_fixture_test",
        role,
    )
    assert bound.password == "scoped-secret"


@pytest.mark.parametrize(
    "changes",
    [
        {"username": "identity_api_consumer"},
        {"host": "localhost"},
        {"port": 55433},
        {"database": "production"},
        {"password": ""},
    ],
)
def test_verifier_role_database_url_rejects_cross_database_inputs(changes):
    reference = URL.create(
        "postgresql",
        username="test_owner",
        password="owner-secret",
        host="127.0.0.1",
        port=55432,
        database="notifications_fixture_test",
    )
    supplied = URL.create(
        "postgresql",
        username="notification_delivery_read_client",
        password="scoped-secret",
        host="127.0.0.1",
        port=55432,
        database="notifications_fixture_test",
    ).set(**changes)

    with pytest.raises(RuntimeError, match="disposable database"):
        verifier_database_url(
            supplied.render_as_string(hide_password=False),
            reference,
            expected_role="notification_delivery_read_client",
            container_name="owned-postgres",
        )


def test_optional_identity_secret_mounts_are_empty_by_default_and_selected_only_for_socket(
    tmp_path,
):
    optional_mounts = {
        "identity_admission_token",
        "identity_actor_status_database_url",
    }
    default_selectors = _identity_optional_secret_selectors(
        tmp_path, notifications_socket_enabled=False
    )
    enabled_selectors = _identity_optional_secret_selectors(
        tmp_path, notifications_socket_enabled=True
    )

    assert (
        set(_expected_secret_mount_names("identity-admission", False))
        >= optional_mounts
    )
    assert _expected_secret_mount_names("notifications-socket", False) is None
    assert _expected_secret_mount_names("notifications-socket", True) == {
        "notification_socket_database_url",
        "notification_socket_redis_url",
        "notification_socket_identity_token",
    }
    assert set(default_selectors) == {
        "IDENTITY_RUNTIME_ADMISSION_TOKEN_FILE",
        "IDENTITY_RUNTIME_ACTOR_STATUS_DATABASE_URL_FILE",
    }
    assert set(default_selectors.values()) == {""}
    assert not any((tmp_path / name).exists() for name in optional_mounts)
    assert enabled_selectors == {
        "IDENTITY_RUNTIME_ADMISSION_TOKEN_FILE": str(
            tmp_path / "identity_admission_token"
        ),
        "IDENTITY_RUNTIME_ACTOR_STATUS_DATABASE_URL_FILE": str(
            tmp_path / "identity_actor_status_database_url"
        ),
    }
    compose = (ROOT / "docker-compose.identity.yml").read_text(encoding="utf-8")
    assert "file: ${IDENTITY_RUNTIME_ADMISSION_TOKEN_FILE:-/dev/null}" in compose
    assert (
        "file: ${IDENTITY_RUNTIME_ACTOR_STATUS_DATABASE_URL_FILE:-/dev/null}" in compose
    )


def _required_native_inputs() -> dict[str, str]:
    names = (
        "AAP_IDENTITY_RUNTIME_IMAGE",
        "AAP_IDENTITY_RUNTIME_IDENTITY",
        "AAP_IDENTITY_API_RUNTIME_IMAGE",
        "AAP_IDENTITY_API_RUNTIME_IDENTITY",
        "AAP_NOTIFICATIONS_SOCKET_RUNTIME_IMAGE",
        "AAP_NOTIFICATIONS_SOCKET_RUNTIME_IDENTITY",
        "AAP_MODULE_DATA_NOTIFICATION_DELIVERY_READ_URL",
        "AAP_MODULE_DATA_IDENTITY_ACTOR_STATUS_URL",
    )
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.fail("Owned cold runtime inputs are missing: " + ", ".join(missing))
    return {name: os.environ[name] for name in names}


def _websocket_url(api_origin: str, token: str | None) -> str:
    parsed = urlsplit(api_origin)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme)
    if scheme is None:
        raise ValueError("API test origin must use HTTP or HTTPS")
    query = urlencode({"token": token}) if token is not None else ""
    return urlunsplit((scheme, parsed.netloc, "/ws/notifications", query, ""))


async def _wait_for_subscribers(owner, channel: str, count: int) -> None:
    async with asyncio.timeout(8):
        while True:
            result = await owner.pubsub_numsub(channel)
            if result and result[0][1] == count:
                return
            await asyncio.sleep(0.025)


async def _expect_rejected_socket(url: str) -> None:
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(
            url,
            proxy=None,
            open_timeout=8,
            close_timeout=2,
            ping_interval=None,
            max_size=None,
        ):
            pytest.fail("Rejected Notifications credentials completed a handshake")


async def _insert_notification(
    seed,
    actor_id: uuid.UUID,
    *,
    kind: str,
    payload,
    target_id: uuid.UUID | None = None,
) -> Notification:
    async with seed.begin() as db:
        row = Notification(
            user_id=actor_id,
            type=kind,
            target_type="task",
            target_id=target_id or uuid.uuid4(),
            payload=payload,
        )
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


def _event_bytes(row: Notification) -> bytes:
    message = notification_event_message(
        id=str(row.id),
        type=row.type,
        target_type=row.target_type,
        target_id=str(row.target_id),
        payload=row.payload,
        created_at=(row.created_at or datetime.now(UTC)).isoformat(),
    )
    return json.dumps(
        message,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


async def _assert_role_capabilities(
    dsn: str,
    *,
    expected_role: str,
    actor_id: uuid.UUID,
) -> None:
    from aap_owner_data.postgres import verify_function_only_role

    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        assert await connection.fetchval("SELECT session_user") == expected_role
        if expected_role == "identity_actor_status_client":
            await verify_function_only_role(
                connection,
                schema="aap_identity_v1",
                functions={("read_actor_facts", "uuid")},
                role=expected_role,
            )
            status = await connection.fetchrow(
                "SELECT actor_id, is_active FROM aap_identity_v1.read_actor_facts($1::uuid)",
                actor_id,
            )
            assert status is not None and status["actor_id"] == actor_id
            assert status["is_active"] is True
            denied_query = "SELECT * FROM public.users LIMIT 1"
        else:
            await verify_function_only_role(
                connection,
                schema="aap_notifications_v1",
                functions=_NOTIFICATION_CAPABILITIES,
                role=expected_role,
            )
            assert not await connection.fetch(
                "SELECT * FROM aap_notifications_v1.read_notification($1::uuid)",
                uuid.uuid4(),
            )
            denied_query = "SELECT * FROM public.notifications LIMIT 1"
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as denied:
            await connection.fetch(denied_query)
        assert denied.value.sqlstate == "42501"
    finally:
        await connection.close(timeout=3)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the verifier-owned disposable PostgreSQL and Redis",
)
async def test_cold_notifications_owner_behind_api_bridge(
    test_engine, socket_world, tmp_path, record_property
):
    """Exercise the cold owner, Identity RPC, and same-origin API path together."""
    inputs = _required_native_inputs()
    seed, actor, project, membership = socket_world
    actor_status_host_url = inputs["AAP_MODULE_DATA_IDENTITY_ACTOR_STATUS_URL"]
    delivery_host_url = inputs["AAP_MODULE_DATA_NOTIFICATION_DELIVERY_READ_URL"]
    for dsn, role in (
        (actor_status_host_url, "identity_actor_status_client"),
        (delivery_host_url, "notification_delivery_read_client"),
    ):
        parsed = make_url(dsn)
        assert (parsed.host, parsed.port, parsed.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
        assert parsed.username == role and parsed.database.endswith("_test")
    await _assert_role_capabilities(
        actor_status_host_url,
        expected_role="identity_actor_status_client",
        actor_id=actor.id,
    )
    await _assert_role_capabilities(
        delivery_host_url,
        expected_role="notification_delivery_read_client",
        actor_id=actor.id,
    )

    channel = f"notify:{actor.id}"
    acl_user = "notification-socket-" + uuid.uuid4().hex
    acl_password = secrets.token_urlsafe(32)
    redis_host_url = _verify_redis()
    redis_admin = redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
    acl_created = False
    stack = None
    inbox = SMTPInbox()
    try:
        await redis_admin.execute_command(
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
        acl_created = True
        socket_redis_host_url = redis_host_url.set(
            username=acl_user, password=acl_password
        ).render_as_string(hide_password=False)
        async with redis.from_url(socket_redis_host_url) as scoped:
            assert await scoped.ping()
            with pytest.raises(ResponseError):
                await scoped.publish(channel, b"forbidden")
            with pytest.raises(ResponseError):
                await scoped.get("private:notification-socket")
            other = scoped.pubsub()
            try:
                with pytest.raises(ResponseError):
                    await other.subscribe(f"notify:{uuid.uuid4()}")
                    await other.get_message(timeout=2)
            finally:
                await other.aclose()

        identity_secret, api_secret, control, admission_token = (
            secrets.token_urlsafe(32) for _ in range(4)
        )
        assert len({identity_secret, api_secret, control, admission_token}) == 4

        async with (
            _login_redis() as (login_redis_url, _),
            _publisher_redis() as (publisher_redis_url, redis_publisher),
        ):
            stack = IdentityModuleStack(
                test_engine,
                tmp_path,
                secret=identity_secret,
                api_secret=api_secret,
                control=control,
                mail_key=secrets.token_urlsafe(32),
                login_redis_url=login_redis_url,
                publisher_redis_url=publisher_redis_url,
                smtp_port=inbox.port,
                notifications_socket_image=inputs[
                    "AAP_NOTIFICATIONS_SOCKET_RUNTIME_IMAGE"
                ],
                notifications_socket_identity=inputs[
                    "AAP_NOTIFICATIONS_SOCKET_RUNTIME_IDENTITY"
                ],
                notification_delivery_database_url=delivery_host_url,
                identity_actor_status_database_url=actor_status_host_url,
                notifications_socket_redis_url=socket_redis_host_url,
                admission_token=admission_token,
            )
            try:
                await stack.start()
                evidence = stack.inspection_evidence
                socket_evidence = evidence["notifications-socket"]
                assert socket_evidence["user"] == "10004:10004"
                assert socket_evidence["read_only"] is True
                assert socket_evidence["running"] is True
                assert socket_evidence["health"] == "healthy"
                assert socket_evidence["secret_mount_targets"] == [
                    "/run/secrets/notification_socket_database_url",
                    "/run/secrets/notification_socket_identity_token",
                    "/run/secrets/notification_socket_redis_url",
                ]

                async with httpx.AsyncClient(
                    base_url=stack.api_origin,
                    trust_env=False,
                    timeout=15,
                ) as api:
                    login = await api.post(
                        "/api/v1/auth/login",
                        json={"email": actor.email, "password": DEFAULT_PASSWORD},
                    )
                    assert login.status_code == 200, stack.redact(login.text)
                    token = login.json()["access_token"]
                    api.headers["Authorization"] = "Bearer " + token

                    async with httpx.AsyncClient(
                        trust_env=False, timeout=8
                    ) as identity:
                        admitted = await identity.post(
                            stack.identity_origin + "/v1/identity/authenticate",
                            headers={"Authorization": "Bearer " + admission_token},
                            json={"token": token},
                        )
                        assert admitted.status_code == 200
                        assert admitted.json()["ok"] is True
                        denied = await identity.post(
                            stack.identity_origin + "/v1/identity/actor-context",
                            headers={"Authorization": "Bearer " + admission_token},
                            json={},
                        )
                        assert denied.status_code == 401

                    async with seed.begin() as db:
                        _key, api_key = await create_api_key(
                            db,
                            actor.id,
                            "Notifications socket rejection",
                            ["*"],
                        )
                    await _expect_rejected_socket(
                        _websocket_url(stack.api_origin, None)
                    )
                    await _expect_rejected_socket(
                        _websocket_url(stack.api_origin, "not-a-jwt")
                    )
                    await _expect_rejected_socket(
                        _websocket_url(stack.api_origin, api_key)
                    )
                    await _wait_for_subscribers(redis_publisher, channel, 0)

                    socket = await websockets.connect(
                        _websocket_url(stack.api_origin, token),
                        proxy=None,
                        open_timeout=10,
                        close_timeout=3,
                        ping_interval=None,
                        max_size=None,
                    )
                    await _wait_for_subscribers(redis_publisher, channel, 1)
                    try:
                        payloads = [
                            [None, 9007199254740993, "跨语言"],
                            "遗留",
                            None,
                            0,
                            False,
                        ]
                        raw_frames = []
                        for index, payload in enumerate(payloads):
                            row = await _insert_notification(
                                seed,
                                actor.id,
                                kind=f"future.native_{index}",
                                payload=payload,
                            )
                            frame_bytes = _event_bytes(row)
                            assert (
                                await redis_publisher.publish(channel, frame_bytes) == 1
                            )
                            received = await asyncio.wait_for(socket.recv(), 5)
                            assert received.encode("utf-8") == frame_bytes
                            raw_frames.append(frame_bytes)
                        node_peer = (
                            ROOT
                            / "apps/notifications/conformance/node/realtime_event_provider.mjs"
                        )
                        node_result = await asyncio.to_thread(
                            subprocess.run,
                            ["node", str(node_peer), "--stdio"],
                            input=raw_frames[0],
                            capture_output=True,
                            timeout=8,
                            check=False,
                        )
                        assert node_result.returncode == 0, stack.redact(
                            node_result.stderr.decode("utf-8", errors="replace")
                        )
                        assert node_result.stdout == raw_frames[0]
                        assert (
                            await redis_publisher.publish(channel, node_result.stdout)
                            == 1
                        )
                        assert (await asyncio.wait_for(socket.recv(), 5)).encode(
                            "utf-8"
                        ) == node_result.stdout

                        started = time.monotonic()
                        missing_id = uuid.uuid4()
                        missing_frame = json.dumps(
                            {"id": str(missing_id), "type": "future.missing"},
                            separators=(",", ":"),
                        ).encode()
                        sync_frame = json.dumps(
                            {
                                "type": "notifications.sync",
                                "marker": "after-four-reads",
                            },
                            separators=(",", ":"),
                        ).encode()
                        assert (
                            await redis_publisher.publish(channel, missing_frame) == 1
                        )
                        assert await redis_publisher.publish(channel, sync_frame) == 1
                        retry_result = json.loads(
                            await asyncio.wait_for(socket.recv(), 5)
                        )
                        retry_elapsed = time.monotonic() - started
                        assert retry_result["marker"] == "after-four-reads"
                        assert _NOTIFICATION_GATE_ATTEMPTS == 4
                        assert _NOTIFICATION_GATE_RETRY_SECONDS == 0.15
                        assert retry_elapsed >= (
                            (_NOTIFICATION_GATE_ATTEMPTS - 1)
                            * _NOTIFICATION_GATE_RETRY_SECONDS
                            - 0.02
                        )

                        pending = seed()
                        await pending.begin()
                        pending_row = Notification(
                            user_id=actor.id,
                            type="future.precommit",
                            target_type="task",
                            target_id=uuid.uuid4(),
                            payload={"retry": [None, 9007199254740993]},
                        )
                        pending.add(pending_row)
                        await pending.flush()
                        await pending.refresh(pending_row)
                        pending_frame = _event_bytes(pending_row)
                        precommit_started = time.monotonic()
                        try:
                            assert (
                                await redis_publisher.publish(channel, pending_frame)
                                == 1
                            )
                            await asyncio.sleep(0.42)
                            await pending.commit()
                        finally:
                            await pending.close()
                        assert (await asyncio.wait_for(socket.recv(), 5)).encode(
                            "utf-8"
                        ) == pending_frame
                        assert time.monotonic() - precommit_started >= 0.40

                        restricted = await _insert_notification(
                            seed,
                            actor.id,
                            kind="task.rejected",
                            payload={"project_id": str(project.id)},
                        )
                        restricted_frame = _event_bytes(restricted)
                        assert (
                            await redis_publisher.publish(channel, restricted_frame)
                            == 1
                        )
                        assert (await asyncio.wait_for(socket.recv(), 5)).encode(
                            "utf-8"
                        ) == restricted_frame
                        async with seed.begin() as db:
                            await db.execute(
                                delete(ProjectMember).where(
                                    ProjectMember.id == membership.id
                                )
                            )
                        assert (
                            await redis_publisher.publish(channel, restricted_frame)
                            == 1
                        )
                        assert (
                            await redis_publisher.publish(
                                channel,
                                b'{"type":"notifications.sync","marker":"revoked"}',
                            )
                            == 1
                        )
                        assert json.loads(await asyncio.wait_for(socket.recv(), 5)) == {
                            "type": "notifications.sync",
                            "marker": "revoked",
                        }

                        async with seed.begin() as db:
                            await db.execute(
                                update(User)
                                .where(User.id == actor.id)
                                .values(role="viewer")
                            )
                        assert (
                            await redis_publisher.publish(
                                channel,
                                b'{"type":"notifications.sync","marker":"role-drift"}',
                            )
                            == 1
                        )
                        assert json.loads(await asyncio.wait_for(socket.recv(), 5)) == {
                            "type": "notifications.sync",
                            "marker": "role-drift",
                        }
                        async with seed.begin() as db:
                            await db.execute(
                                update(User)
                                .where(User.id == actor.id)
                                .values(role="employee")
                            )

                        await stack.kill_service("notifications-socket")
                        with pytest.raises(websockets.exceptions.ConnectionClosed):
                            await asyncio.wait_for(socket.recv(), 5)
                        await socket.close()
                        await _wait_for_subscribers(redis_publisher, channel, 0)
                        durable = await _insert_notification(
                            seed,
                            actor.id,
                            kind="future.durable",
                            payload={"offline": True},
                        )
                        await stack.start_service("notifications-socket")
                        inbox_response = await api.get("/api/v1/notifications")
                        assert inbox_response.status_code == 200, stack.redact(
                            inbox_response.text
                        )
                        assert any(
                            item["id"] == str(durable.id)
                            for item in inbox_response.json()["items"]
                        )

                        socket = await websockets.connect(
                            _websocket_url(stack.api_origin, token),
                            proxy=None,
                            open_timeout=10,
                            close_timeout=3,
                            ping_interval=None,
                            max_size=None,
                        )
                        await _wait_for_subscribers(redis_publisher, channel, 1)
                        heartbeat_started = time.monotonic()
                        heartbeat = json.loads(
                            await asyncio.wait_for(
                                socket.recv(), _HEARTBEAT_SECONDS + 12
                            )
                        )
                        heartbeat_elapsed = time.monotonic() - heartbeat_started
                        assert heartbeat == {"type": "ping"}
                        assert 25 <= heartbeat_elapsed <= 42

                        async with seed.begin() as db:
                            await db.execute(
                                update(User)
                                .where(User.id == actor.id)
                                .values(is_active=False)
                            )
                        assert (
                            await redis_publisher.publish(
                                channel,
                                b'{"type":"notifications.sync","marker":"disabled"}',
                            )
                            == 1
                        )
                        with pytest.raises(TimeoutError):
                            await asyncio.wait_for(socket.recv(), 0.4)
                    finally:
                        await socket.close()
                    await _wait_for_subscribers(redis_publisher, channel, 0)
                    await _expect_rejected_socket(
                        _websocket_url(stack.api_origin, token)
                    )
                record_property(
                    "notifications_socket_cold_native",
                    json.dumps(
                        {
                            "api_same_origin_bridge": True,
                            "identity_admission_rpc": True,
                            "scoped_bearer_rejects_actor_context": True,
                            "api_key_and_missing_jwt_rejected": True,
                            "notification_role_function_only": True,
                            "actor_status_role_function_only": True,
                            "actor_channel_redis_acl": True,
                            "jsonb_payload_shapes": len(payloads),
                            "node_echo_byte_identical": True,
                            "missing_event_retry_elapsed_seconds": round(
                                retry_elapsed, 3
                            ),
                            "configured_missing_event_reads": _NOTIFICATION_GATE_ATTEMPTS,
                            "configured_retry_waits": _NOTIFICATION_GATE_ATTEMPTS - 1,
                            "precommit_event_delivered_after_commit": True,
                            "current_scope_revocation_and_role_drift": True,
                            "owner_restart_durable_inbox_reconciled": True,
                            "heartbeat_elapsed_seconds": round(heartbeat_elapsed, 3),
                            "disabled_actor_rejected": True,
                        }
                    ),
                )
            finally:
                await stack.stop()
    except Exception:
        if stack is not None:
            record_property("runtime_diagnostics", stack.capture_diagnostics())
        raise
    finally:
        inbox.close()
        try:
            if acl_created:
                _verify_redis()
                assert (
                    await redis_admin.execute_command("ACL", "DELUSER", acl_user) == 1
                )
        finally:
            await redis_admin.aclose()
