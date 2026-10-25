"""Run alone on a fresh verifier-owned private DB with no other producers/workers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import secrets
import subprocess
import sys
import time
import uuid

import pytest
import redis.asyncio as redis
from redis.exceptions import ResponseError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.factory import create_user
from tests.notifications_publisher_acceptance_helpers import (
    assert_no_owned_containers,
    connect_owned_dependency,
    container_logs,
    create_live_container,
    create_publisher_acl_user,
    create_private_network,
    delete_attempted_publisher_acl_user,
    disconnect_owned_dependency,
    inspect_publisher_image,
    prepare_secret_files,
    publisher_redis_urls,
    remove_owned_container,
    remove_owned_network,
    remove_secret_files,
    run_once_container,
    run_owned_cleanup_step,
    verify_live_container,
    wait_for_container_log,
)
from tests.test_asset_storage_cleanup_runtime import (
    _inspect_docker,
    _verify_postgres,
)
from tests.test_identity_admission_live import (
    _dsn,
    _start_identity,
    _token,
    _world,
)
from tests.test_identity_deactivation_live import (
    _cleanup_deactivation,
    _collect_notifications,
    _command,
    _due_command,
    _node,
    _notification_rows,
    _wait_claimed,
)
from tests.test_identity_sessions_live import _verify_redis


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_NOTIFICATIONS_DELIVERY_LIVE_ACCEPTANCE") != "1"
        or not os.getenv("AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IMAGE")
        or not os.getenv("AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IDENTITY")
        or not os.getenv("AAP_NOTIFICATIONS_DELIVERY_RUNTIME_SOURCE_REVISION"),
        reason="requires a fresh exclusive verifier-owned Notifications acceptance DB",
    ),
]


_BLACKHOLE_SERVER = """import socket, threading
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("0.0.0.0", 16379))
listener.listen(16)
print("listening", flush=True)
def park(connection):
    try:
        if connection.recv(4096):
            print("handshake", flush=True)
        while connection.recv(4096):
            pass
    except OSError:
        pass
while True:
    connection, _ = listener.accept()
    threading.Thread(target=park, args=(connection,), daemon=True).start()
"""


def _publisher_environment():
    image_reference = os.environ["AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IMAGE"]
    acceptance_id = os.environ["AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IDENTITY"]
    source_revision = os.environ["AAP_NOTIFICATIONS_DELIVERY_RUNTIME_SOURCE_REVISION"]
    return inspect_publisher_image(
        image_reference,
        acceptance_id=acceptance_id,
        source_revision=source_revision,
    )


def _redis_dependency():
    url = _verify_redis()
    container_name = os.environ["AAP_MODULE_DATA_REDIS_CONTAINER_NAME"]
    observed = _inspect_docker(container_name)
    labels = observed.get("Config", {}).get("Labels") or {}
    if not (
        observed.get("State", {}).get("Running") is True
        and observed.get("Config", {}).get("Labels", {}).get("org.aap.worktree-owner")
        == os.environ["AAP_MODULE_DATA_REDIS_OWNER"]
        and observed.get("Name") == f"/{container_name}"
        and observed.get("Id")
        and labels.get("org.aap.worktree-owner")
    ):
        pytest.fail("The verifier-owned Redis dependency did not match")
    return url, observed["Id"]


@asynccontextmanager
async def _publisher_redis_users(base_url, expected_container_id):
    owner = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    clients = []
    attempted = []
    urls = {}
    try:
        for allow_publish in (True, False):
            prefix = "notify-pub-" if allow_publish else "notify-deny-"
            name = prefix + uuid.uuid4().hex
            password = secrets.token_urlsafe(32)
            await create_publisher_acl_user(
                owner,
                name,
                password,
                allow_publish=allow_publish,
                attempted=attempted,
            )
            host_url, container_url = publisher_redis_urls(
                base_url,
                username=name,
                password=password,
            )
            client = redis.from_url(host_url, decode_responses=True)
            clients.append(client)
            assert await client.ping()
            urls["allowed" if allow_publish else "denied"] = container_url

        with pytest.raises(ResponseError):
            await clients[1].publish("notify:acl-proof", "denied")
        yield urls
    finally:
        cleanup_errors = []
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                cleanup_errors.append("scoped Redis client")
        try:
            if attempted:
                redis_url, redis_id = _redis_dependency()
                if not (
                    redis_id == expected_container_id
                    and redis_url.host in {"127.0.0.1", "localhost"}
                ):
                    pytest.fail("Refusing to remove ACL users from another Redis")
                for name in attempted:
                    try:
                        await delete_attempted_publisher_acl_user(owner, name)
                    except Exception:
                        cleanup_errors.append("scoped Redis ACL user")
        except Exception:
            cleanup_errors.append("Redis ownership check")
        finally:
            try:
                await owner.aclose()
            except Exception:
                cleanup_errors.append("Redis owner client")
        if cleanup_errors:
            pytest.fail("Notifications acceptance could not remove scoped Redis users")


async def _subscribe(pubsub, channels):
    await pubsub.subscribe(*channels)
    acknowledged = set()
    deadline = time.monotonic() + 5
    while acknowledged != set(channels) and time.monotonic() < deadline:
        message = await pubsub.get_message(timeout=0.2)
        if message and message.get("type") == "subscribe":
            channel = message["channel"]
            acknowledged.add(
                channel.decode() if isinstance(channel, bytes) else channel
            )
    assert acknowledged == set(channels)


async def _assert_no_notifications(pubsub, *, timeout=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = await pubsub.get_message(
            ignore_subscribe_messages=True,
            timeout=min(0.1, max(0, deadline - time.monotonic())),
        )
        if message and message.get("type") == "message":
            pytest.fail("Redis received a notification before publisher authorization")


async def _delivery_rows(maker, operation_id):
    async with maker() as db:
        return list(
            (
                await db.execute(
                    text(
                        "SELECT notification_id,state,attempt,available_at,worker_id,"
                        "lease_id,lease_expires_at,last_reason FROM "
                        "public.notification_delivery_outbox "
                        "WHERE operation_id=:operation_id ORDER BY notification_id"
                    ),
                    {"operation_id": operation_id},
                )
            ).mappings()
        )


async def _require_exclusive_delivery_database(maker):
    async with maker() as db:
        database_name = await db.scalar(text("SELECT current_database()"))
        count = await db.scalar(
            text(
                "SELECT count(*) FROM public.notification_delivery_outbox WHERE state='pending'"
            )
        )
        publisher_sessions = await db.scalar(
            text(
                "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                "WHERE datname=current_database() "
                "AND application_name='aap-notification-publisher'"
            )
        )
    assert database_name.endswith("_test")
    assert count == 0, "Exclusive acceptance database must have no pending deliveries"
    assert publisher_sessions == 0, (
        "Exclusive acceptance database has another publisher session"
    )


async def _expire_retry_rows(maker, operation_id, expected_count):
    async with maker.begin() as db:
        result = await db.execute(
            text(
                "UPDATE public.notification_delivery_outbox "
                "SET available_at=now()-interval '1 second' "
                "WHERE operation_id=:operation_id AND state='pending' "
                "AND last_reason='redis_error' AND lease_id IS NULL"
            ),
            {"operation_id": operation_id},
        )
    assert result.rowcount == expected_count


async def _expire_dead_leases(maker, operation_id, expected_count):
    async with maker.begin() as db:
        result = await db.execute(
            text(
                "UPDATE public.notification_delivery_outbox "
                "SET lease_expires_at=now()-interval '1 second', "
                "available_at=now()-interval '1 second' "
                "WHERE operation_id=:operation_id AND state='pending' "
                "AND lease_id IS NOT NULL"
            ),
            {"operation_id": operation_id},
        )
    assert result.rowcount == expected_count


async def _create_deactivation(
    maker,
    identity,
    control,
    actor,
    secret,
    operation_ids,
    *,
    reason,
):
    request = _command(
        "request_self_deactivation",
        _token(actor, secret),
        {"reason": reason},
    )
    operation_id = uuid.UUID(request["operation_id"])
    operation_ids.append(operation_id)
    reply = await asyncio.to_thread(_node, identity, control, request)
    assert reply["ok"] and reply["operation_id"] == str(operation_id)
    assert reason not in json.dumps(reply, ensure_ascii=False)
    rows = await _notification_rows(maker, operation_id)
    assert len(rows) == 2
    assert all(row["state"] == "pending" for row in rows)
    assert all(row["type"] == "user.deactivation_requested" for row in rows)
    assert all(
        row["target_type"] == "user"
        and row["target_id"] == actor
        and set(row["payload"]) == {"email", "name", "role", "scheduled_at"}
        for row in rows
    )
    return operation_id, rows


async def test_standalone_publisher_acl_retry_restart_and_cold_lease_takeover(
    test_engine, tmp_path, record_property
):
    image = _publisher_environment()
    run_id = uuid.uuid4().hex[:24]
    image_id = image["id"]
    suffix = uuid.uuid4().hex
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor = None
    actor_ids = []
    operation_ids = []
    identity = None
    redis_owner = None
    pubsub = None
    containers = []
    secret_directories = []
    connected_dependencies = []
    network = None
    network_id = None
    database_url = ""
    postgres_url = None
    redis_base_url = None
    redis_container_id = None
    postgres_container_id = None

    try:
        database_url = _dsn("NOTIFICATION_PUBLISH", test_engine)
        postgres_url = make_url(database_url)
        _verify_postgres(postgres_url)
        redis_base_url, redis_container_id = _redis_dependency()
        postgres_container_id = os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"]
        await _require_exclusive_delivery_database(maker)
        maker, actor = await _world(test_engine)
        actor_ids.append(actor)
        async with maker.begin() as db:
            other_admin = await create_user(
                db,
                "super_admin",
                f"notifications-publisher-{suffix}@example.test",
                "Notifications acceptance recipient",
            )
        actor_ids.append(other_admin.id)
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
            enable_deactivation=True,
        )

        network, network_id = create_private_network(run_id)
        for dependency_id, alias in (
            (postgres_container_id, "notifications-postgres"),
            (redis_container_id, "notifications-redis"),
        ):
            connected_dependencies.append(dependency_id)
            connect_owned_dependency(network, network_id, dependency_id, alias)

        postgres_url = postgres_url.set(host="notifications-postgres", port=5432)
        async with _publisher_redis_users(
            redis_base_url, redis_container_id
        ) as redis_urls:
            database_value = postgres_url.render_as_string(hide_password=False)

            def make_secret_files(label, redis_url):
                secret_directories.append(
                    tmp_path / f"notifications-publisher-secrets-{label}"
                )
                return prepare_secret_files(
                    tmp_path,
                    label=label,
                    database_url=database_value,
                    redis_url=redis_url,
                )

            allowed_secrets = make_secret_files("allowed", redis_urls["allowed"])
            denied_secrets = make_secret_files("denied", redis_urls["denied"])
            blocked_url = "redis://:acceptance-blocked@notifications-blackhole:16379/0"
            blocked_secrets = make_secret_files("blocked", blocked_url)
            # URL contents are only mounted as read-only files into UID 10004.
            assert all(path.stat().st_mode & 0o777 == 0o444 for path in allowed_secrets)
            assert all(path.stat().st_mode & 0o777 == 0o444 for path in denied_secrets)
            assert all(path.stat().st_mode & 0o777 == 0o444 for path in blocked_secrets)

            redis_owner = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
            pubsub = redis_owner.pubsub()
            channels = [f"notify:{actor}", f"notify:{other_admin.id}"]
            await _subscribe(pubsub, channels)

            async def run_once_case(case, secret_paths, redis_url, private_reason):
                container_id = create_live_container(
                    image_id=image_id,
                    run_id=run_id,
                    network_name=network,
                    network_id=network_id,
                    case=case,
                    database_secret=secret_paths[0],
                    redis_secret=secret_paths[1],
                )
                containers.append(container_id)
                return await asyncio.to_thread(
                    run_once_container,
                    container_id,
                    run_id=run_id,
                    image_id=image_id,
                    network_name=network,
                    network_id=network_id,
                    secrets_to_hide=(database_value, redis_url, private_reason),
                    container_name=f"aap-notify-{run_id[:10]}-{case}",
                    test_case=case,
                )

            def assert_messages(messages, expected_ids, reason):
                assert set(messages) == {str(value) for value in expected_ids}
                for notification_id, (channel, body) in messages.items():
                    assert set(body) == {
                        "id",
                        "type",
                        "target_type",
                        "target_id",
                        "payload",
                        "created_at",
                    }
                    assert body["id"] == notification_id
                    assert body["type"] == "user.deactivation_requested"
                    assert body["target_type"] == "user"
                    assert body["target_id"] == str(actor)
                    assert set(body["payload"]) == {
                        "email",
                        "name",
                        "role",
                        "scheduled_at",
                    }
                    assert "user_id" not in body
                    assert reason not in json.dumps(body, ensure_ascii=False)
                    assert channel in channels

            async def assert_delivered(operation_id, notification_ids, attempts):
                rows = await _delivery_rows(maker, operation_id)
                assert len(rows) == len(notification_ids) == 2
                assert {str(row["notification_id"]) for row in rows} == {
                    str(value) for value in notification_ids
                }
                assert all(
                    row["state"] == "done"
                    and row["attempt"] == attempts
                    and row["last_reason"] == "published"
                    and row["worker_id"] is None
                    and row["lease_id"] is None
                    and row["lease_expires_at"] is None
                    for row in rows
                )

            private_reason = f"private-publisher-reason-{suffix}"
            first_id, first_rows = await _create_deactivation(
                maker,
                identity,
                control,
                actor,
                secret,
                operation_ids,
                reason=private_reason,
            )
            first_ids = [row["id"] for row in first_rows]
            denied_result = await run_once_case(
                "denied", denied_secrets, redis_urls["denied"], private_reason
            )
            assert denied_result == {
                "claimed": 2,
                "published": 0,
                "retry": 2,
                "stale": 0,
                "unavailable": 0,
            }
            retry_rows = await _delivery_rows(maker, first_id)
            assert len(retry_rows) == 2 and all(
                row["state"] == "pending"
                and row["attempt"] == 1
                and row["last_reason"] == "redis_error"
                and row["worker_id"] is None
                and row["lease_id"] is None
                and row["lease_expires_at"] is None
                for row in retry_rows
            )
            await _assert_no_notifications(pubsub)
            await _expire_retry_rows(maker, first_id, expected_count=2)

            allowed_result = await run_once_case(
                "allowed", allowed_secrets, redis_urls["allowed"], private_reason
            )
            assert allowed_result == {
                "claimed": 2,
                "published": 2,
                "retry": 0,
                "stale": 0,
                "unavailable": 0,
            }
            first_messages = await _collect_notifications(pubsub, first_ids)
            assert_messages(first_messages, first_ids, private_reason)
            await assert_delivered(first_id, first_ids, attempts=2)

            empty_result = await run_once_case(
                "empty-restart",
                allowed_secrets,
                redis_urls["allowed"],
                private_reason,
            )
            assert empty_result == {
                "claimed": 0,
                "published": 0,
                "retry": 0,
                "stale": 0,
                "unavailable": 0,
            }
            await _assert_no_notifications(pubsub)

            cancel = _command("cancel_self_deactivation", _token(actor, secret), {})
            operation_ids.append(uuid.UUID(cancel["operation_id"]))
            assert (await asyncio.to_thread(_node, identity, control, cancel))["ok"]
            takeover_reason = f"private-takeover-reason-{suffix}"
            second_id, second_rows = await _create_deactivation(
                maker,
                identity,
                control,
                actor,
                secret,
                operation_ids,
                reason=takeover_reason,
            )
            second_ids = [row["id"] for row in second_rows]

            listener = create_live_container(
                image_id=image_id,
                run_id=run_id,
                network_name=network,
                network_id=network_id,
                case="parked-redis",
                network_alias="notifications-blackhole",
                entrypoint="python",
                command=("-c", _BLACKHOLE_SERVER),
            )
            containers.append(listener)
            subprocess.run(
                ["docker", "start", listener],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            await asyncio.to_thread(
                wait_for_container_log,
                listener,
                run_id=run_id,
                network_name=network,
                network_id=network_id,
                marker="listening",
            )
            listener_state = verify_live_container(
                listener,
                run_id=run_id,
                image_id=image_id,
                network_name=network,
                network_id=network_id,
                phase="running",
                container_name=f"aap-notify-{run_id[:10]}-parked-redis",
                test_case="parked-redis",
            )
            assert (
                "notifications-blackhole"
                in listener_state["NetworkSettings"]["Networks"][network]["Aliases"]
            )

            blocked_container = create_live_container(
                image_id=image_id,
                run_id=run_id,
                network_name=network,
                network_id=network_id,
                case="cold-publisher",
                database_secret=blocked_secrets[0],
                redis_secret=blocked_secrets[1],
            )
            containers.append(blocked_container)
            subprocess.run(
                ["docker", "start", blocked_container],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            await _wait_claimed(maker, second_id)
            await asyncio.to_thread(
                wait_for_container_log,
                listener,
                run_id=run_id,
                network_name=network,
                network_id=network_id,
                marker="handshake",
            )
            blocked_observed = verify_live_container(
                blocked_container,
                run_id=run_id,
                image_id=image_id,
                network_name=network,
                network_id=network_id,
                phase="running",
                container_name=f"aap-notify-{run_id[:10]}-cold-publisher",
                test_case="cold-publisher",
                secret_destinations=(
                    "/run/secrets/database-url",
                    "/run/secrets/redis-url",
                ),
            )
            assert blocked_observed["State"]["Running"] is True
            leased_rows = await _delivery_rows(maker, second_id)
            assert len(leased_rows) == 2 and all(
                row["state"] == "pending"
                and row["attempt"] == 1
                and row["worker_id"] is not None
                and row["lease_id"] is not None
                and row["lease_expires_at"] is not None
                and row["last_reason"] is None
                for row in leased_rows
            )
            # The lease is committed; the stub received handshake bytes and
            # withholds its response, before the publisher can issue PUBLISH.
            subprocess.run(
                ["docker", "kill", blocked_container],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            killed = verify_live_container(
                blocked_container,
                run_id=run_id,
                image_id=image_id,
                network_name=network,
                network_id=network_id,
                phase="stopped",
                container_name=f"aap-notify-{run_id[:10]}-cold-publisher",
                test_case="cold-publisher",
                secret_destinations=(
                    "/run/secrets/database-url",
                    "/run/secrets/redis-url",
                ),
            )
            assert killed["State"]["Running"] is False
            blocked_stdout, blocked_stderr = container_logs(blocked_container)
            if any(
                value in blocked_stdout + blocked_stderr
                for value in (database_value, blocked_url, takeover_reason)
            ):
                pytest.fail("Killed publisher output disclosed a secret URL")
            await _expire_dead_leases(maker, second_id, expected_count=2)

            replay_result = await run_once_case(
                "lease-replay",
                allowed_secrets,
                redis_urls["allowed"],
                takeover_reason,
            )
            assert replay_result == {
                "claimed": 2,
                "published": 2,
                "retry": 0,
                "stale": 0,
                "unavailable": 0,
            }
            second_messages = await _collect_notifications(pubsub, second_ids)
            assert_messages(second_messages, second_ids, takeover_reason)
            await assert_delivered(second_id, second_ids, attempts=2)
            record_property("notifications_publisher_image_id", image_id)
            record_property(
                "notifications_publisher_image_acceptance_id",
                image["acceptance_id"],
            )
            record_property(
                "notifications_publisher_image_source_revision",
                image["source_revision"],
            )
            record_property(
                "notifications_publisher_container_ids",
                ",".join(containers),
            )
            record_property("notifications_publisher_network_id", network_id)
            record_property(
                "notifications_delivery_acceptance",
                "real-identity-outbox,deny-publish-retry,allowed-publish,empty-once,cold-kill-after-claim-before-redis-handshake-response,stable-ids",
            )
            record_property(
                "notifications_publisher_postgres_container_id",
                postgres_container_id,
            )
    finally:
        primary_error = sys.exception()
        cleanup_errors = []
        cleanup_interrupts = []

        async def attempt_cleanup(label, operation):
            try:
                error = await run_owned_cleanup_step(operation)
            except BaseException as caught:
                error = caught
            if error is None:
                return True
            if isinstance(
                error,
                (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
            ):
                cleanup_interrupts.append(error)
            else:
                cleanup_errors.append(label)
            return False

        if pubsub is not None:
            await attempt_cleanup("Redis unsubscribe", pubsub.unsubscribe)
            await attempt_cleanup("Redis subscription close", pubsub.aclose)
        if redis_owner is not None:
            await attempt_cleanup("Redis owner client", redis_owner.aclose)
        if identity is not None:
            await attempt_cleanup("Identity producer", identity.stop)
        containers_clean = True
        for container_id in reversed(containers):
            if not await attempt_cleanup(
                "owned container",
                lambda container_id=container_id: remove_owned_container(
                    container_id,
                    run_id=run_id,
                    network_name=network,
                    network_id=network_id,
                ),
            ):
                containers_clean = False
        if containers_clean:
            if not await attempt_cleanup(
                "partially created owned container",
                lambda: assert_no_owned_containers(run_id),
            ):
                containers_clean = False
        if containers_clean:
            for secret_directory in reversed(secret_directories):
                await attempt_cleanup(
                    "publisher secret files",
                    lambda secret_directory=secret_directory: remove_secret_files(
                        tmp_path, secret_directory
                    ),
                )
        elif secret_directories:
            cleanup_errors.append(
                "publisher secrets retained because containers remain"
            )
        if network is not None and network_id is not None:
            for dependency_id in reversed(connected_dependencies):
                await attempt_cleanup(
                    "dependency network attachment",
                    lambda dependency_id=dependency_id: disconnect_owned_dependency(
                        network, network_id, dependency_id
                    ),
                )
            await attempt_cleanup(
                "private network",
                lambda: remove_owned_network(network, network_id, run_id),
            )
        if actor_ids or operation_ids:
            await attempt_cleanup(
                "committed producer fixture",
                lambda: _cleanup_deactivation(
                    maker,
                    actor_ids=actor_ids,
                    user_operation_ids=operation_ids,
                ),
            )
        if primary_error is not None:
            for label in cleanup_errors:
                primary_error.add_note(f"Notifications cleanup failed: {label}")
            for error in cleanup_interrupts:
                primary_error.add_note(
                    f"Notifications cleanup interrupted: {type(error).__name__}"
                )
        elif cleanup_interrupts:
            raise cleanup_interrupts[0]
        elif cleanup_errors:
            pytest.fail("Notifications acceptance could not complete owned cleanup")


async def test_standalone_publisher_delivers_real_deactivation_completed_event(
    test_engine, tmp_path, record_property
):
    image = _publisher_environment()
    run_id = uuid.uuid4().hex[:24]
    image_id = image["id"]
    suffix = uuid.uuid4().hex
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor = None
    actor_ids = []
    operation_ids = []
    due_operation_ids = []
    identity = None
    redis_owner = None
    pubsub = None
    containers = []
    secret_directories = []
    connected_dependencies = []
    network = None
    network_id = None
    database_value = ""
    redis_container_id = None
    postgres_container_id = None

    try:
        database_url = _dsn("NOTIFICATION_PUBLISH", test_engine)
        postgres_url = make_url(database_url)
        _verify_postgres(postgres_url)
        redis_base_url, redis_container_id = _redis_dependency()
        postgres_container_id = os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"]
        await _require_exclusive_delivery_database(maker)

        maker, actor = await _world(test_engine)
        actor_ids.append(actor)
        actor_email = f"identity-live-{actor}@example.test"
        actor_name = "Live Identity user"
        async with maker.begin() as db:
            other_admin = await create_user(
                db,
                "super_admin",
                f"notifications-completed-{suffix}@example.test",
                "Notifications completion recipient",
            )
        actor_ids.append(other_admin.id)
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
            enable_deactivation=True,
            enable_deactivation_operator=True,
        )

        network, network_id = create_private_network(run_id)
        for dependency_id, alias in (
            (postgres_container_id, "notifications-postgres"),
            (redis_container_id, "notifications-redis"),
        ):
            connected_dependencies.append(dependency_id)
            connect_owned_dependency(network, network_id, dependency_id, alias)

        postgres_url = postgres_url.set(host="notifications-postgres", port=5432)
        database_value = postgres_url.render_as_string(hide_password=False)
        async with _publisher_redis_users(
            redis_base_url, redis_container_id
        ) as redis_urls:
            secret_directories.append(
                tmp_path / "notifications-publisher-secrets-completed"
            )
            publisher_secrets = prepare_secret_files(
                tmp_path,
                label="completed",
                database_url=database_value,
                redis_url=redis_urls["allowed"],
            )
            assert all(
                path.stat().st_mode & 0o777 == 0o444 for path in publisher_secrets
            )

            redis_owner = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
            pubsub = redis_owner.pubsub()
            channels = [f"notify:{actor}", f"notify:{other_admin.id}"]
            await _subscribe(pubsub, channels)

            async def run_once_case(case):
                container_id = create_live_container(
                    image_id=image_id,
                    run_id=run_id,
                    network_name=network,
                    network_id=network_id,
                    case=case,
                    database_secret=publisher_secrets[0],
                    redis_secret=publisher_secrets[1],
                )
                containers.append(container_id)
                return await asyncio.to_thread(
                    run_once_container,
                    container_id,
                    run_id=run_id,
                    image_id=image_id,
                    network_name=network,
                    network_id=network_id,
                    secrets_to_hide=(database_value, redis_urls["allowed"]),
                    container_name=f"aap-notify-{run_id[:10]}-{case}",
                    test_case=case,
                )

            reason = f"private-completed-reason-{suffix}"
            request_id, request_rows = await _create_deactivation(
                maker,
                identity,
                control,
                actor,
                secret,
                operation_ids,
                reason=reason,
            )
            request_ids = [row["id"] for row in request_rows]
            request_result = await run_once_case("completed-request")
            assert request_result == {
                "claimed": 2,
                "published": 2,
                "retry": 0,
                "stale": 0,
                "unavailable": 0,
            }
            request_messages = await _collect_notifications(pubsub, request_ids)
            assert set(request_messages) == {str(value) for value in request_ids}
            for notification_id, (channel, body) in request_messages.items():
                assert body["id"] == notification_id
                assert body["type"] == "user.deactivation_requested"
                assert body["target_type"] == "user"
                assert body["target_id"] == str(actor)
                assert set(body["payload"]) == {
                    "email",
                    "name",
                    "role",
                    "scheduled_at",
                }
                assert "user_id" not in body
                assert reason not in json.dumps(body, ensure_ascii=False)
                assert channel in channels
            assert all(
                row["state"] == "done"
                for row in await _notification_rows(maker, request_id)
            )

            # Keep the accepted request and its correlation row in sync while
            # making only this fixture actor due now.
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "UPDATE public.users SET "
                        "deactivation_requested_at=now()-interval '8 days', "
                        "deactivation_scheduled_at=now()-interval '1 second' "
                        "WHERE id=:actor AND is_active"
                    ),
                    {"actor": actor},
                )
                reservation = await db.execute(
                    text(
                        "UPDATE public.identity_deactivation_requests SET "
                        "requested_at=now()-interval '8 days', "
                        "scheduled_at=now()-interval '1 second', updated_at=now() "
                        "WHERE request_id=:request_id AND user_id=:actor "
                        "AND state='pending'"
                    ),
                    {"request_id": request_id, "actor": actor},
                )
                assert reservation.rowcount == 1

            due_request = _due_command(limit=16)
            due_id = uuid.UUID(due_request["operation_id"])
            due_operation_ids.append(due_id)
            due_result = await asyncio.to_thread(_node, identity, control, due_request)
            assert due_result == {
                "schema": "aap-identity-deactivation-due/v1",
                "ok": True,
                "operation_id": str(due_id),
                "processed": 1,
                "examined": 1,
                "blocked_last_super_admin": 0,
                "has_more": False,
            }
            async with maker() as db:
                completed_actor = (
                    (
                        await db.execute(
                            text(
                                "SELECT is_active,status,disabled_kind,disabled_by,"
                                "disabled_reason,deactivation_requested_at,"
                                "deactivation_scheduled_at FROM public.users WHERE id=:actor"
                            ),
                            {"actor": actor},
                        )
                    )
                    .mappings()
                    .one()
                )
            assert completed_actor["is_active"] is False
            assert completed_actor["status"] == "offline"
            assert completed_actor["disabled_kind"] == "deleted"
            assert completed_actor["disabled_by"] == actor
            assert completed_actor["disabled_reason"] == reason
            assert completed_actor["deactivation_requested_at"] is None
            assert completed_actor["deactivation_scheduled_at"] is None

            completion_rows = [
                row
                for row in await _notification_rows(maker, request_id)
                if row["type"] == "user.deactivation_completed"
            ]
            assert len(completion_rows) == 1
            completion = completion_rows[0]
            assert completion["user_id"] == other_admin.id
            assert completion["target_type"] == "user"
            assert completion["target_id"] == actor
            assert completion["payload"] == {
                "email": actor_email,
                "name": actor_name,
                "auto": True,
            }
            assert completion["state"] == "pending"
            assert completion["attempt"] == 0
            completion_id = completion["id"]

            completion_result = await run_once_case("completed-event")
            assert completion_result == {
                "claimed": 1,
                "published": 1,
                "retry": 0,
                "stale": 0,
                "unavailable": 0,
            }
            completion_messages = await _collect_notifications(pubsub, [completion_id])
            assert set(completion_messages) == {str(completion_id)}
            channel, body = completion_messages[str(completion_id)]
            assert set(body) == {
                "id",
                "type",
                "target_type",
                "target_id",
                "payload",
                "created_at",
            }
            assert body["id"] == str(completion_id)
            assert body["type"] == "user.deactivation_completed"
            assert body["target_type"] == "user"
            assert body["target_id"] == str(actor)
            assert body["payload"] == {
                "email": actor_email,
                "name": actor_name,
                "auto": True,
            }
            assert "user_id" not in body
            assert "reason" not in body["payload"]
            assert reason not in json.dumps(body, ensure_ascii=False)
            assert channel == f"notify:{other_admin.id}"

            completion_delivery = [
                row
                for row in await _delivery_rows(maker, request_id)
                if row["notification_id"] == completion_id
            ]
            assert len(completion_delivery) == 1
            assert completion_delivery[0]["state"] == "done"
            assert completion_delivery[0]["attempt"] == 1
            assert completion_delivery[0]["last_reason"] == "published"
            assert completion_delivery[0]["worker_id"] is None
            assert completion_delivery[0]["lease_id"] is None
            assert completion_delivery[0]["lease_expires_at"] is None

            empty_result = await run_once_case("completed-empty")
            assert empty_result == {
                "claimed": 0,
                "published": 0,
                "retry": 0,
                "stale": 0,
                "unavailable": 0,
            }
            await _assert_no_notifications(pubsub)
            record_property("notifications_publisher_image_id", image_id)
            record_property(
                "notifications_publisher_image_acceptance_id",
                image["acceptance_id"],
            )
            record_property(
                "notifications_publisher_image_source_revision",
                image["source_revision"],
            )
            record_property(
                "notifications_publisher_container_ids",
                ",".join(containers),
            )
            record_property("notifications_publisher_network_id", network_id)
            record_property(
                "notifications_deactivation_completed_acceptance",
                "real-request,consistent-due-reservation,real-identity-operator,completion-only-active-admin,standalone-image-publish,stable-id,done,empty-restart",
            )
            record_property(
                "notifications_publisher_postgres_container_id",
                postgres_container_id,
            )
    finally:
        primary_error = sys.exception()
        cleanup_errors = []
        cleanup_interrupts = []

        async def attempt_cleanup(label, operation):
            try:
                error = await run_owned_cleanup_step(operation)
            except BaseException as caught:
                error = caught
            if error is None:
                return True
            if isinstance(
                error,
                (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
            ):
                cleanup_interrupts.append(error)
            else:
                cleanup_errors.append(label)
            return False

        if pubsub is not None:
            await attempt_cleanup("Redis unsubscribe", pubsub.unsubscribe)
            await attempt_cleanup("Redis subscription close", pubsub.aclose)
        if redis_owner is not None:
            await attempt_cleanup("Redis owner client", redis_owner.aclose)
        if identity is not None:
            await attempt_cleanup("Identity producer", identity.stop)
        containers_clean = True
        for container_id in reversed(containers):
            if not await attempt_cleanup(
                "owned container",
                lambda container_id=container_id: remove_owned_container(
                    container_id,
                    run_id=run_id,
                    network_name=network,
                    network_id=network_id,
                ),
            ):
                containers_clean = False
        if containers_clean:
            if not await attempt_cleanup(
                "partially created owned container",
                lambda: assert_no_owned_containers(run_id),
            ):
                containers_clean = False
        if containers_clean:
            for secret_directory in reversed(secret_directories):
                await attempt_cleanup(
                    "publisher secret files",
                    lambda secret_directory=secret_directory: remove_secret_files(
                        tmp_path, secret_directory
                    ),
                )
        elif secret_directories:
            cleanup_errors.append(
                "publisher secrets retained because containers remain"
            )
        if network is not None and network_id is not None:
            for dependency_id in reversed(connected_dependencies):
                await attempt_cleanup(
                    "dependency network attachment",
                    lambda dependency_id=dependency_id: disconnect_owned_dependency(
                        network, network_id, dependency_id
                    ),
                )
            await attempt_cleanup(
                "private network",
                lambda: remove_owned_network(network, network_id, run_id),
            )
        if actor_ids or operation_ids or due_operation_ids:
            await attempt_cleanup(
                "committed producer fixture",
                lambda: _cleanup_deactivation(
                    maker,
                    actor_ids=actor_ids,
                    user_operation_ids=operation_ids,
                    due_operation_ids=due_operation_ids,
                ),
            )
        if primary_error is not None:
            for label in cleanup_errors:
                primary_error.add_note(f"Notifications cleanup failed: {label}")
            for error in cleanup_interrupts:
                primary_error.add_note(
                    f"Notifications cleanup interrupted: {type(error).__name__}"
                )
        elif cleanup_interrupts:
            raise cleanup_interrupts[0]
        elif cleanup_errors:
            pytest.fail("Notifications acceptance could not complete owned cleanup")
