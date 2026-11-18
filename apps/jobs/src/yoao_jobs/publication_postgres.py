"""Session-held Jobs publication authority, separate from the dispatch ledger."""

from __future__ import annotations

import asyncio
from builtins import BaseExceptionGroup
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit


class PublicationUnavailable(RuntimeError):
    """The native owner cannot admit a publisher without losing evidence."""


def publication_database_url(direct: str, file_path: str) -> str:
    if direct and file_path:
        raise ValueError("Jobs publication database selectors conflict")
    if file_path:
        try:
            with Path(file_path).open("rb") as source:
                raw = source.read(4097)
            if len(raw) > 4096:
                raise ValueError("Jobs publication database credential is invalid")
            value = raw.decode("utf-8").strip()
        except (OSError, UnicodeError):
            raise ValueError(
                "Jobs publication database credential is invalid"
            ) from None
    else:
        value = direct
    try:
        parsed = urlsplit(value)
        parsed.port
    except (TypeError, ValueError):
        raise ValueError("Jobs publication database URL is invalid") from None
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("Jobs publication database URL is invalid")
    return value


@asynccontextmanager
async def _publication_connection(database_url):
    from asyncpg import connect

    connection = await connect(dsn=database_url)
    failure = None
    try:
        yield connection
    except BaseException as exc:
        failure = exc
        raise
    finally:
        close = asyncio.create_task(connection.close())
        try:
            await asyncio.shield(close)
        except BaseException as exc:
            while not close.done():
                try:
                    await asyncio.shield(close)
                except BaseException:
                    continue
            try:
                close.result()
            except BaseException as closing:
                if failure is not None:
                    failure.add_note(
                        f"Jobs publication connection close failed: {closing!r}"
                    )
                else:
                    raise
            if failure is None:
                raise exc


@asynccontextmanager
async def publication_session(database_url, lease):
    """Persist entry before publish; record acceptance or a proved pre-request refusal."""
    async with _publication_connection(database_url) as connection:
        owned = await connection.fetchval(
            "SELECT pg_catalog.pg_try_advisory_lock("
            "pg_catalog.hashtextextended('aap-jobs:publication:' || $1::uuid::text, 0))",
            lease.intent_id,
        )
        if owned is not True:
            raise PublicationUnavailable("Jobs publication is already owned")
        arguments = (
            lease.intent_id,
            lease.job_id,
            lease.execution_id,
            lease.generation,
            lease.lease_token,
        )
        entered = await connection.fetchval(
            "SELECT yoao_jobs_v1.begin_dispatch_publication($1, $2, $3, $4, $5)",
            *arguments,
        )
        if entered not in {"entered", "settled"}:
            raise PublicationUnavailable(
                "Jobs publication authority is unavailable or unsettled"
            )
        yield connection, arguments, entered == "entered"


@asynccontextmanager
async def retry_publication_session(
    database_url, job_id, execution_id, task_name, args, kwargs, retry
):
    """Fence a Celery retry against the original immutable dispatch and retirement."""
    encoded_args = json.dumps(args, allow_nan=False, separators=(",", ":"))
    encoded_kwargs = json.dumps(kwargs, allow_nan=False, separators=(",", ":"))
    async with _publication_connection(database_url) as connection:
        intent = await connection.fetchval(
            "SELECT yoao_jobs_v1.retry_publication_intent($1, $2, $3, $4::jsonb, $5::jsonb)",
            job_id,
            execution_id,
            task_name,
            encoded_args,
            encoded_kwargs,
        )
        if intent is None:
            raise RuntimeError("Celery retry has no unique immutable Jobs dispatch")
        await connection.fetchval(
            "SELECT pg_catalog.pg_advisory_lock("
            "pg_catalog.hashtextextended('aap-jobs:publication:' || $1::uuid::text, 0))",
            intent,
        )
        nonce = uuid.uuid4()
        state = await connection.fetchval(
            "SELECT yoao_jobs_v1.begin_retry_publication("
            "$1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8)",
            intent,
            job_id,
            execution_id,
            task_name,
            encoded_args,
            encoded_kwargs,
            retry,
            nonce,
        )
        if state not in {"entered", "settled"}:
            raise RuntimeError("Celery retry publication is unavailable or unsettled")
        yield (
            connection,
            (intent, job_id, execution_id, retry, nonce),
            state == "entered",
        )


async def settle_retry_publication(connection, arguments):
    settled = await connection.fetchval(
        "SELECT yoao_jobs_v1.finish_retry_publication($1, $2, $3, $4, $5)",
        *arguments,
    )
    if settled is not True:
        raise RuntimeError("Celery retry publication settlement was not acknowledged")


async def settle_publication(connection, arguments):
    settled = await connection.fetchval(
        "SELECT yoao_jobs_v1.finish_dispatch_publication($1, $2, $3, $4, $5)",
        *arguments,
    )
    if settled is not True:
        raise RuntimeError("Jobs publication settlement was not acknowledged")


async def refuse_publication(connection, arguments):
    recorded = await connection.fetchval(
        "SELECT yoao_jobs_v1.refuse_dispatch_publication($1, $2, $3, $4, $5)",
        *arguments,
    )
    if recorded is not True:
        raise RuntimeError("Jobs pre-request refusal was not acknowledged")


async def publish_fixture_media_delivery(database_url, delivery_id, publisher):
    """Publish only the owner-saved envelope while holding its physical fence."""
    from .queue_contract import (
        JobsQueueNotSent,
        JobsQueueUnavailable,
        QueueDispatch,
        decode_queue_request,
        encode_queue_request,
    )

    async with _publication_connection(database_url) as connection:
        owned = await connection.fetchval(
            "SELECT pg_catalog.pg_try_advisory_lock("
            "pg_catalog.hashtextextended('aap-jobs:fixture-media:' || $1::uuid::text, 0))",
            delivery_id,
        )
        if owned is not True:
            raise PublicationUnavailable("Fixture media publication is already owned")
        nonce = uuid.uuid4()
        raw = await connection.fetchval(
            "SELECT yoao_jobs_v1.begin_fixture_media_publication($1, $2)",
            delivery_id,
            nonce,
        )
        try:
            receipt = json.loads(raw) if isinstance(raw, str) else raw
            if (
                not isinstance(receipt, dict)
                or set(receipt)
                != {"state", "delivery_id", "payload_digest", "envelope"}
                or receipt["state"] not in {"entered", "accepted"}
                or receipt["delivery_id"] != str(delivery_id)
                or not isinstance(receipt["payload_digest"], str)
                or len(receipt["payload_digest"]) != 64
                or any(
                    char not in "0123456789abcdef" for char in receipt["payload_digest"]
                )
            ):
                raise ValueError
            operation = decode_queue_request(encode_queue_request(receipt["envelope"]))
            if not isinstance(
                operation, QueueDispatch
            ) or operation.execution_id != str(delivery_id):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise PublicationUnavailable(
                "Fixture media publication receipt is invalid"
            ) from None
        if receipt["state"] == "accepted":
            return str(delivery_id)
        try:
            published = await publisher.publish_envelope(receipt["envelope"])
        except JobsQueueNotSent as primary:
            try:
                recorded = await connection.fetchval(
                    "SELECT yoao_jobs_v1.finish_fixture_media_publication($1, $2, $3)",
                    delivery_id,
                    nonce,
                    "not_sent",
                )
                if recorded is not True:
                    raise PublicationUnavailable(
                        "Fixture media refusal was not acknowledged"
                    )
            except BaseException as closing:
                raise BaseExceptionGroup(
                    "Fixture media refusal and owner recording failed",
                    [primary, closing],
                ) from None
            raise
        if published != str(delivery_id):
            raise JobsQueueUnavailable()
        if (
            await connection.fetchval(
                "SELECT yoao_jobs_v1.finish_fixture_media_publication($1, $2, $3)",
                delivery_id,
                nonce,
                "accepted",
            )
            is not True
        ):
            raise PublicationUnavailable(
                "Fixture media publication was not acknowledged"
            )
        return str(delivery_id)
