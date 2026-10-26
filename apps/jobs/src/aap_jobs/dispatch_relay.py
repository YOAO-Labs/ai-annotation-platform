"""Opt-in broker-only relay for saved Jobs dispatch envelopes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .dispatch_data import JobsDispatchDataClient

_MAX_SECRET_BYTES = 4096


def _secret(env: Mapping[str, str], name: str) -> str:
    direct = env.get(name, "")
    file_path = env.get(f"{name}_FILE", "")
    if direct and file_path:
        raise ValueError("Jobs dispatch relay credential selectors conflict")
    if file_path:
        try:
            with Path(file_path).open("rb") as source:
                raw = source.read(_MAX_SECRET_BYTES + 1)
            if len(raw) > _MAX_SECRET_BYTES:
                raise ValueError("Jobs dispatch relay credential is invalid")
            value = raw.decode("utf-8").strip()
        except (OSError, UnicodeError):
            raise ValueError("Jobs dispatch relay credential is invalid") from None
    else:
        value = direct
    if not value:
        raise ValueError("Jobs dispatch relay credential is missing")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Jobs dispatch relay settings are invalid") from None
    if str(value) != raw or not 1 <= value <= maximum:
        raise ValueError("Jobs dispatch relay settings are invalid")
    return value


@dataclass(frozen=True, slots=True)
class JobsDispatchRelaySettings:
    database_url: str = field(repr=False)
    queue_url: str = field(repr=False)
    queue_token: str = field(repr=False)
    batch_size: int = 100
    lease_seconds: int = 30
    poll_seconds: int = 1

    def __post_init__(self) -> None:
        if not self.database_url or not self.queue_url or not self.queue_token:
            raise ValueError("Jobs dispatch relay settings are incomplete")
        try:
            database = urlsplit(self.database_url)
            queue = urlsplit(self.queue_url)
            token_bytes = self.queue_token.encode("ascii")
            queue.port
        except (AttributeError, UnicodeError, ValueError):
            raise ValueError("Jobs dispatch relay settings are invalid") from None
        if database.scheme not in {"postgres", "postgresql"}:
            raise ValueError("Jobs dispatch relay database URL is invalid")
        if (
            queue.scheme not in {"http", "https"}
            or not queue.netloc
            or queue.username is not None
            or queue.password is not None
            or queue.query
            or queue.fragment
        ):
            raise ValueError("Jobs dispatch relay queue URL is invalid")
        if not token_bytes or any(byte < 33 or byte > 126 for byte in token_bytes):
            raise ValueError("Jobs dispatch relay settings are invalid")
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 500:
            raise ValueError("Jobs dispatch relay settings are invalid")
        if type(self.lease_seconds) is not int or not 1 <= self.lease_seconds <= 300:
            raise ValueError("Jobs dispatch relay settings are invalid")
        if type(self.poll_seconds) is not int or not 1 <= self.poll_seconds <= 60:
            raise ValueError("Jobs dispatch relay settings are invalid")

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> JobsDispatchRelaySettings:
        values = os.environ if env is None else env
        if values.get("JOBS_DISPATCH_RELAY_ENABLED") != "true":
            raise ValueError("Jobs dispatch relay requires explicit enablement")
        return cls(
            database_url=_secret(values, "JOBS_DISPATCH_RELAY_DATABASE_URL"),
            queue_url=values.get("JOBS_QUEUE_URL", ""),
            queue_token=_secret(values, "JOBS_QUEUE_TOKEN"),
            batch_size=_positive_int(
                values, "JOBS_DISPATCH_RELAY_BATCH_SIZE", 100, 500
            ),
            lease_seconds=_positive_int(
                values, "JOBS_DISPATCH_RELAY_LEASE_SECONDS", 30, 300
            ),
            poll_seconds=_positive_int(
                values, "JOBS_DISPATCH_RELAY_POLL_SECONDS", 1, 60
            ),
        )


class QueueEnvelopePublisher(Protocol):
    async def publish_envelope(self, envelope: dict[str, Any]) -> str: ...


class JobsDispatchRelay:
    """Publish claims and acknowledge only the exact lease that was claimed."""

    def __init__(
        self,
        ledger: JobsDispatchDataClient,
        publisher: QueueEnvelopePublisher,
        *,
        batch_size: int = 100,
        lease_seconds: int = 30,
    ):
        self._ledger = ledger
        self._publisher = publisher
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds

    async def run_once(self) -> int:
        from .dispatch_contract import JobsDispatchRejected
        from .queue_contract import JobsQueueUnavailable

        accepted_count = 0
        for _ in range(self._batch_size):
            leases = await self._ledger.claim(
                limit=1, lease_seconds=self._lease_seconds
            )
            if not leases:
                break
            lease = leases[0]
            accepted = False
            try:
                accepted_id = await self._publisher.publish_envelope(lease.envelope)
                accepted = accepted_id == lease.execution_id
            except JobsQueueUnavailable:
                pass
            try:
                await self._ledger.finish(lease, accepted=accepted)
            except JobsDispatchRejected as rejection:
                if rejection.code != "stale_lease":
                    raise
                continue
            accepted_count += int(accepted)
        return accepted_count


async def run_forever(
    settings: JobsDispatchRelaySettings,
    *,
    provider=None,
    publisher: QueueEnvelopePublisher | None = None,
) -> None:
    """Run a separately configured relay; both collaborators are replaceable in tests."""
    from .dispatch_postgres import JobsDispatchPostgres
    from .queue_client import JobsQueueClient

    owns_provider = provider is None
    if provider is None:
        provider = await JobsDispatchPostgres.connect(settings.database_url)
    if publisher is None:
        publisher = JobsQueueClient(settings.queue_url, settings.queue_token)
    relay = JobsDispatchRelay(
        provider.client,
        publisher,
        batch_size=settings.batch_size,
        lease_seconds=settings.lease_seconds,
    )
    try:
        while True:
            accepted = await relay.run_once()
            if accepted == 0:
                await asyncio.sleep(settings.poll_seconds)
    finally:
        if owns_provider:
            await provider.close()


def main() -> None:
    settings = JobsDispatchRelaySettings.from_env()
    asyncio.run(run_forever(settings))
