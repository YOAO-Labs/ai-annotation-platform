"""Independent, at-least-once Notifications Redis publisher."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from datetime import UTC

from .notification_delivery_contract import (
    NotificationDeliveryUnavailable,
    NotificationLease,
    NOTIFICATION_DELIVERY_POLICY,
)
from .notification_delivery_postgres import NotificationDeliveryPostgresData

from .delivery_config import NotificationPublisherSettings
from .realtime_event_contract import notification_event_message

log = logging.getLogger("aap_notifications.delivery_worker")


def _notification_message(lease: NotificationLease) -> tuple[str, str]:
    """Build the existing WebSocket channel and message shape without user_id."""
    message = notification_event_message(
        id=str(lease.id),
        type=lease.type,
        target_type=lease.target_type,
        target_id=str(lease.target_id),
        payload=lease.payload.model_dump(mode="json", by_alias=True),
        created_at=lease.created_at.astimezone(UTC).isoformat(),
    )
    try:
        encoded = json.dumps(
            message,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError):
        raise NotificationDeliveryUnavailable() from None
    return f"notify:{lease.user_id}", encoded


class NotificationPublisher:
    """Claim durable rows, publish with stable IDs, then acknowledge the lease."""

    def __init__(
        self,
        data,
        redis,
        *,
        worker_id: uuid.UUID | None = None,
        claim_limit: int = 16,
        lease_seconds: int = 60,
        concurrency: int = 4,
        redis_timeout_seconds: float = 5,
    ):
        if worker_id is not None and type(worker_id) is not uuid.UUID:
            raise TypeError("worker_id must be a UUID")
        if (
            type(claim_limit) is not int
            or not 1 <= claim_limit <= 100
            or type(lease_seconds) is not int
            or not 1 <= lease_seconds <= 300
            or type(concurrency) is not int
            or not 1 <= concurrency <= 4
            or type(redis_timeout_seconds) not in (int, float)
            or not 0 < redis_timeout_seconds <= 5
        ):
            raise ValueError("Invalid notification publisher bounds")
        self.data = data
        self.redis = redis
        self.worker_id = worker_id or uuid.uuid4()
        self.claim_limit = claim_limit
        self.lease_seconds = lease_seconds
        self.concurrency = concurrency
        self.redis_timeout_seconds = float(redis_timeout_seconds)

    async def _retry(self, lease: NotificationLease) -> str:
        try:
            accepted = await self.data.finish_delivery(
                lease, outcome="retry", reason="redis_error"
            )
        except NotificationDeliveryUnavailable:
            # The lease expires and can be reclaimed; never log the Redis URL or
            # driver exception from a delivery failure.
            return "unavailable"
        return "retry" if accepted else "stale"

    async def _publish_one(self, lease: NotificationLease, semaphore) -> str:
        async with semaphore:
            try:
                channel, message = _notification_message(lease)
                async with asyncio.timeout(self.redis_timeout_seconds):
                    # A zero subscriber count still means Redis accepted PUBLISH;
                    # inbox rows are durable and clients can fetch them later.
                    await self.redis.publish(channel, message)
            except asyncio.CancelledError:
                # The lease will expire and another worker will retry. A publish
                # interrupted in flight may be delivered twice with the same ID.
                raise
            except Exception:
                return await self._retry(lease)

            try:
                accepted = await self.data.finish_delivery(
                    lease, outcome="delivered", reason="published"
                )
            except NotificationDeliveryUnavailable:
                # Redis may already have delivered. Lease expiry provides the
                # recovery path and the stable notification ID deduplicates it.
                return "unavailable"
            return "published" if accepted else "stale"

    async def run_once(self) -> dict[str, int]:
        """Claim one bounded page; the claim transaction commits before PUBLISH."""
        leases = await self.data.claim_deliveries(
            self.worker_id,
            limit=self.claim_limit,
            lease_seconds=self.lease_seconds,
        )
        if len(leases) > self.claim_limit:
            raise NotificationDeliveryUnavailable()
        ids = [lease.id for lease in leases]
        lease_ids = [lease.lease_id for lease in leases]
        if (
            len(ids) != len(set(ids))
            or len(lease_ids) != len(set(lease_ids))
            or any(lease.worker_id != self.worker_id for lease in leases)
        ):
            raise NotificationDeliveryUnavailable()
        if not leases:
            return {
                "claimed": 0,
                "published": 0,
                "retry": 0,
                "stale": 0,
                "unavailable": 0,
            }
        semaphore = asyncio.Semaphore(self.concurrency)
        outcomes = await asyncio.gather(
            *(self._publish_one(lease, semaphore) for lease in leases)
        )
        return {
            "claimed": len(leases),
            "published": outcomes.count("published"),
            "retry": outcomes.count("retry"),
            "stale": outcomes.count("stale"),
            "unavailable": outcomes.count("unavailable"),
        }


async def run_forever(
    settings: NotificationPublisherSettings,
    *,
    data=None,
    redis=None,
    stop_event: asyncio.Event | None = None,
    ready_event: asyncio.Event | None = None,
) -> None:
    """Poll the durable queue until cancelled or the supplied event is set."""
    owns_redis = redis is None
    if data is None:
        data = NotificationDeliveryPostgresData(
            settings.database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    if redis is None:
        from redis.asyncio import Redis

        redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_connect_timeout=settings.redis_timeout_seconds,
            socket_timeout=settings.redis_timeout_seconds,
            decode_responses=False,
        )
    if stop_event is None:
        stop_event = asyncio.Event()
    if ready_event is not None:
        ready_event.clear()

    try:
        policy = await data.delivery_policy()
        if policy.policy != NOTIFICATION_DELIVERY_POLICY:
            raise NotificationDeliveryUnavailable()
        async with asyncio.timeout(settings.redis_timeout_seconds):
            await redis.ping()
        if ready_event is not None:
            ready_event.set()
        publisher = NotificationPublisher(
            data,
            redis,
            claim_limit=settings.claim_limit,
            lease_seconds=settings.lease_seconds,
            concurrency=settings.concurrency,
            redis_timeout_seconds=settings.redis_timeout_seconds,
        )
        while not stop_event.is_set():
            try:
                result = await publisher.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not log exception text: driver errors can contain DSNs.
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=settings.poll_interval_seconds
                    )
                except TimeoutError:
                    pass
                continue
            if result["claimed"] < settings.claim_limit:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=settings.poll_interval_seconds
                    )
                except TimeoutError:
                    pass
    finally:
        if ready_event is not None:
            ready_event.clear()
        if owns_redis:
            try:
                await redis.aclose()
            except Exception:
                if not asyncio.current_task().cancelling():
                    log.warning("notification publisher Redis close failed")


async def run_once(
    settings: NotificationPublisherSettings,
    *,
    data=None,
    redis=None,
) -> dict[str, int]:
    """Validate the owner policy and publish at most one bounded claim page."""
    owns_redis = redis is None
    if data is None:
        data = NotificationDeliveryPostgresData(
            settings.database_url.get_secret_value(),
            connect_timeout=settings.connect_timeout_seconds,
            command_timeout=settings.command_timeout_seconds,
        )
    if redis is None:
        from redis.asyncio import Redis

        redis = Redis.from_url(
            settings.redis_url.get_secret_value(),
            socket_connect_timeout=settings.redis_timeout_seconds,
            socket_timeout=settings.redis_timeout_seconds,
            decode_responses=False,
        )
    try:
        policy = await data.delivery_policy()
        if policy.policy != NOTIFICATION_DELIVERY_POLICY:
            raise NotificationDeliveryUnavailable()
        publisher = NotificationPublisher(
            data,
            redis,
            claim_limit=settings.claim_limit,
            lease_seconds=settings.lease_seconds,
            concurrency=settings.concurrency,
            redis_timeout_seconds=settings.redis_timeout_seconds,
        )
        return await publisher.run_once()
    finally:
        if owns_redis:
            try:
                await redis.aclose()
            except Exception:
                if not asyncio.current_task().cancelling():
                    raise NotificationDeliveryUnavailable() from None


async def _serve(settings: NotificationPublisherSettings, *, once: bool) -> None:
    if once:
        result = await run_once(settings)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return
    if settings.api_token is None:
        await run_forever(settings)
        return

    import uvicorn
    from redis.asyncio import Redis

    from .notification_delivery_postgres import NotificationDeliveryPostgresData
    from .publication_runtime import create_app

    stop_event = asyncio.Event()
    ready_event = asyncio.Event()
    data = NotificationDeliveryPostgresData(
        settings.database_url.get_secret_value(),
        connect_timeout=settings.connect_timeout_seconds,
        command_timeout=settings.command_timeout_seconds,
    )
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=settings.redis_timeout_seconds,
        socket_timeout=settings.redis_timeout_seconds,
        decode_responses=False,
    )
    server = None
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(
                    settings,
                    redis=redis,
                    data=data,
                    publisher_ready=ready_event,
                ),
                host=settings.host,
                port=settings.port,
                access_log=False,
                log_level="warning",
            )
        )

        async def serve_api() -> None:
            await ready_event.wait()
            try:
                await server.serve()
            finally:
                stop_event.set()

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                run_forever(
                    settings,
                    data=data,
                    redis=redis,
                    stop_event=stop_event,
                    ready_event=ready_event,
                )
            )
            tasks.create_task(serve_api())
    finally:
        stop_event.set()
        if server is not None:
            server.should_exit = True
        try:
            await redis.aclose()
        except Exception:
            if not asyncio.current_task().cancelling():
                log.warning("notification publisher Redis close failed")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish committed Notifications deliveries"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="claim and publish one bounded page, then exit",
    )
    args = parser.parse_args()
    try:
        settings = NotificationPublisherSettings()
        asyncio.run(_serve(settings, once=args.once))
    except KeyboardInterrupt:
        return
    except Exception:
        raise SystemExit(
            "Notification publisher configuration or owner unavailable"
        ) from None


if __name__ == "__main__":
    main()
