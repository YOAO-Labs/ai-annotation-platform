"""Owned Redis and database checks for the cold Notifications composition."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import secrets
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import asyncpg
from kombu import Connection, Exchange, Producer, Queue
import pytest
import redis.asyncio as redis
from redis.exceptions import ResponseError
from sqlalchemy.engine import URL, make_url

from tests.test_notifications_publisher_image_live import _redis_dependency
from tests.test_notifications_socket_runtime_live import (
    asyncpg_connection_url,
)


_NOTIFICATION_PUBLISH_FUNCTIONS = {
    ("identity_delivery_policy", ""),
    ("claim_identity_deliveries", "jsonb"),
    ("finish_identity_delivery", "jsonb"),
}
_API_REDIS_ACL_COMMANDS = (
    "-@all",
    "+@connection",
    "+@read",
    "+@write",
    "+@blocking",
    "+@scripting",
    "+@transaction",
    "+@pubsub",
    "-@admin",
    "-@dangerous",
    "+eval",
    "+evalsha",
    "+script|load",
)


def api_redis_acl_commands() -> tuple[str, ...]:
    """Allow cache, Celery, and narrowly channel-scoped fanout operations."""

    return _API_REDIS_ACL_COMMANDS


def kombu_fanout_channel_pattern(redis_url: str | URL) -> str:
    """Match Kombu Redis's default `/{db}.` fanout topic prefix."""

    url = make_url(redis_url)
    database = url.database or "0"
    if (
        not database.isascii()
        or not database.isdecimal()
        or url.query.get("global_keyprefix")
    ):
        raise ValueError("API Redis URL has an unsupported Kombu fanout prefix")
    return f"&/{database}.*"


def _verify_kombu_api_redis(host_url: str) -> None:
    marker = uuid4().hex
    queue_name = "notifications-module-probe-" + marker
    fanout_name = "notifications-module-fanout-" + marker
    direct = Exchange(queue_name, type="direct", durable=False, auto_delete=True)
    queue = Queue(
        queue_name,
        exchange=direct,
        routing_key=queue_name,
        durable=False,
        auto_delete=True,
    )
    fanout = Exchange(fanout_name, type="fanout", durable=False, auto_delete=True)
    with Connection(host_url) as connection:
        channel = connection.channel()
        bound_queue = queue.bind(channel)
        try:
            bound_queue.declare()
            Producer(channel, exchange=direct, routing_key=queue_name).publish(
                {"marker": marker}, declare=[queue]
            )
            message = bound_queue.get(no_ack=True)
            assert message is not None and message.payload == {"marker": marker}
            Producer(channel, exchange=fanout, routing_key="").publish(
                {"marker": marker}, declare=[fanout]
            )
        finally:
            bound_queue.delete()


def required_module_inputs() -> dict[str, str]:
    from tests.test_notifications_socket_runtime_live import _required_native_inputs

    values = _required_native_inputs()
    names = (
        "AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IMAGE",
        "AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IDENTITY",
        "AAP_NOTIFICATIONS_DELIVERY_RUNTIME_SOURCE_REVISION",
        "AAP_MODULE_DATA_IDENTITY_NOTIFICATION_PUBLISH_URL",
    )
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.fail(
            "Owned Notifications module inputs are missing: " + ", ".join(missing)
        )
    values.update({name: os.environ[name] for name in names})
    return values


def notification_publish_database_url(dsn: str, reference_url: URL) -> URL:
    url = make_url(dsn)
    if (
        url.drivername not in {"postgresql", "postgresql+asyncpg"}
        or (url.host, url.port, url.database)
        != (reference_url.host, reference_url.port, reference_url.database)
        or url.username != "notification_publish_client"
        or not url.password
        or not url.database
        or not url.database.endswith("_test")
    ):
        raise ValueError("Notifications publisher DSN left the owned test database")
    return asyncpg_connection_url(url)


async def verify_notification_publish_database_role(
    dsn: str,
    *,
    reference_url: URL,
) -> None:
    url = notification_publish_database_url(dsn, reference_url)

    from aap_owner_data.postgres import verify_function_only_role

    connection = await asyncpg.connect(
        url.render_as_string(hide_password=False), timeout=5
    )
    try:
        assert await connection.fetchval("SELECT session_user") == (
            "notification_publish_client"
        )
        await verify_function_only_role(
            connection,
            schema="aap_notifications_v1",
            functions=_NOTIFICATION_PUBLISH_FUNCTIONS,
            role="notification_publish_client",
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as denied:
            await connection.fetch("SELECT * FROM public.notifications LIMIT 1")
        assert denied.value.sqlstate == "42501"
    finally:
        await connection.close(timeout=3)


@asynccontextmanager
async def api_redis_url(actor_ids: tuple[UUID, ...]) -> AsyncIterator[str]:
    """Create and prove a verifier-Redis user that cannot publish notifications."""

    base_url, redis_container_id = _redis_dependency()
    name = "notifications-api-" + uuid4().hex
    password = secrets.token_urlsafe(32)
    owner = redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
    client = None
    attempted = False
    host_url = base_url.set(username=name, password=password).render_as_string(
        hide_password=False
    )
    try:
        if await owner.acl_getuser(name) is not None:
            pytest.fail("Generated API Redis ACL name already exists")
        attempted = True
        await owner.execute_command(
            "ACL",
            "SETUSER",
            name,
            "reset",
            "on",
            ">" + password,
            "resetkeys",
            "~*",
            "resetchannels",
            kombu_fanout_channel_pattern(base_url),
            *api_redis_acl_commands(),
        )
        client = redis.from_url(host_url, decode_responses=False)
        assert await client.ping()

        cache_key = "notifications-module-cache-probe:" + uuid4().hex
        queue_key = "notifications-module-celery-probe:" + uuid4().hex
        try:
            assert await client.set(cache_key, b"ready", ex=15)
            assert await client.get(cache_key) == b"ready"
            assert await client.lpush(queue_key, b"task") == 1
            assert await client.rpop(queue_key) == b"task"
            await asyncio.to_thread(_verify_kombu_api_redis, host_url)
            for actor_id in actor_ids:
                with pytest.raises(ResponseError):
                    await client.publish(f"notify:{actor_id}", b"forbidden")
                with pytest.raises(ResponseError):
                    await client.eval(
                        "return redis.call('PUBLISH', KEYS[1], ARGV[1])",
                        1,
                        f"notify:{actor_id}",
                        "forbidden",
                    )
                async with client.pubsub() as pubsub:
                    with pytest.raises(ResponseError):
                        await pubsub.subscribe(f"notify:{actor_id}")
                        await pubsub.get_message(timeout=2)
            for command in (("CONFIG", "GET", "maxmemory"),):
                with pytest.raises(ResponseError):
                    await client.execute_command(*command)
            escape_name = "notifications-api-acl-escape-" + uuid4().hex
            try:
                with pytest.raises(ResponseError):
                    await client.execute_command("ACL", "SETUSER", escape_name, "on")
            finally:
                if await owner.acl_getuser(escape_name) is not None:
                    await owner.acl_deluser(escape_name)
        finally:
            await client.delete(cache_key, queue_key)

        yield host_url
    finally:
        try:
            if client is not None:
                await client.aclose()
            if attempted:
                _, observed_id = _redis_dependency()
                if observed_id != redis_container_id:
                    pytest.fail("Refusing to remove API ACL user from another Redis")
                if await owner.acl_getuser(name) is not None:
                    assert await owner.acl_deluser(name) == 1
        finally:
            await owner.aclose()


@asynccontextmanager
async def actor_socket_redis_url(actor_id: UUID) -> AsyncIterator[str]:
    """Create an actor-only socket subscriber identity on the verifier Redis."""

    base_url, redis_container_id = _redis_dependency()
    name = "notification-socket-" + uuid4().hex
    password = secrets.token_urlsafe(32)
    channel = f"notify:{actor_id}"
    owner = redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
    client = None
    attempted = False
    host_url = base_url.set(username=name, password=password).render_as_string(
        hide_password=False
    )
    try:
        if await owner.acl_getuser(name) is not None:
            pytest.fail("Generated socket Redis ACL name already exists")
        attempted = True
        await owner.execute_command(
            "ACL",
            "SETUSER",
            name,
            "reset",
            "on",
            ">" + password,
            "resetchannels",
            "&" + channel,
            "-@all",
            "+subscribe",
            "+unsubscribe",
            "+ping",
            "+select",
            "+client|setinfo",
        )
        client = redis.from_url(host_url, decode_responses=False)
        assert await client.ping()
        with pytest.raises(ResponseError):
            await client.publish(channel, b"forbidden")
        with pytest.raises(ResponseError):
            await client.get("private:notification-socket")
        async with client.pubsub() as pubsub:
            with pytest.raises(ResponseError):
                await pubsub.subscribe(f"notify:{uuid4()}")
                await pubsub.get_message(timeout=2)
            await pubsub.subscribe(channel)
            await pubsub.get_message(timeout=2)
        yield host_url
    finally:
        try:
            if client is not None:
                await client.aclose()
            if attempted:
                _, observed_id = _redis_dependency()
                if observed_id != redis_container_id:
                    pytest.fail("Refusing to remove socket ACL user from another Redis")
                if await owner.acl_getuser(name) is not None:
                    assert await owner.acl_deluser(name) == 1
        finally:
            await owner.aclose()
