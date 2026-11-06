"""Retire the exact managed Inference scopes owned by a sealed fixture run."""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID, uuid5

import asyncpg

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yoao_inference_contract.routing import RoutingUnavailable
from yoao_inference_contract.scope_retirement import (
    ScopeRetirementView,
    make_scope_retirement_request,
)

from app.services.ml_routing.authority import RoutingAuthority


class FixturePredictionRetirementBlocked(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def _inventory(session_factory, run_id, project_ids, task_ids):
    try:
        async with session_factory() as db:
            value = await db.scalar(
                text(
                    "SELECT aap_inference_routing_v1.fixture_prediction_scope_inventory("
                    "CAST(:run_id AS uuid), CAST(:project_ids AS uuid[]), CAST(:task_ids AS uuid[]))"
                ),
                {
                    "run_id": run_id,
                    "project_ids": list(project_ids),
                    "task_ids": list(task_ids),
                },
            )
    except SQLAlchemyError as exc:
        raise FixturePredictionRetirementBlocked("scope_inventory_unavailable") from exc
    if (
        type(value) is not dict
        or set(value) != {"schema", "scopes"}
        or value["schema"] != "aap-fixture-prediction-scopes/v1"
        or type(value["scopes"]) is not list
    ):
        raise FixturePredictionRetirementBlocked("scope_inventory_invalid")
    scopes = []
    seen = set()
    for item in value["scopes"]:
        try:
            if type(item) is not dict or set(item) != {
                "scope_id",
                "capability",
                "retirement_id",
            }:
                raise ValueError("invalid scope fields")
            scope_id = UUID(item["scope_id"])
            retirement_id = (
                UUID(item["retirement_id"])
                if item["retirement_id"] is not None
                else uuid5(scope_id, "aap-fixture-prediction-retirement/v1")
            )
            if (
                str(scope_id) != item["scope_id"]
                or scope_id in seen
                or type(item["capability"]) is not str
                or not item["capability"]
            ):
                raise ValueError("invalid scope identity")
            request = make_scope_retirement_request(
                retirement_id, scope_id, item["capability"]
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise FixturePredictionRetirementBlocked("scope_inventory_invalid") from exc
        seen.add(scope_id)
        scopes.append(request)
    return scopes


# The individual private request has a 60-second transport deadline. The
# suite caller supplies a fresh shorter deadline for each reader wait.
_READER_DRAIN_SECONDS = 45
_LISTENER_CLOSE_SECONDS = 0.25


async def wait_for_fixture_original_readers(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    *,
    deadline: float | None = None,
) -> None:
    """Wait only for owner-settled reads, using the fixture owner's engine."""
    channel = f"aap_fixture_reader_{run_id.hex}"
    engine = session_factory.kw["bind"]
    wake = asyncio.Event()

    def notified(_connection, _pid, _channel, _payload):
        wake.set()

    def disconnected(_connection):
        wake.set()

    if deadline is None:
        deadline = asyncio.get_running_loop().time() + _READER_DRAIN_SECONDS
    observed_pending = False
    drain_timeout = asyncio.timeout_at(deadline)
    try:
        async with drain_timeout:
            async with engine.connect() as listener:
                proxy = await listener.get_raw_connection()
                raw = proxy.driver_connection
                raw.add_termination_listener(disconnected)
                subscribed = False
                try:
                    # asyncpg commits LISTEN outside a transaction. Subscribe
                    # before the first fresh status snapshot.
                    await raw.add_listener(channel, notified)
                    subscribed = True
                    while True:
                        wake.clear()
                        if raw.is_closed():
                            raise FixturePredictionRetirementBlocked(
                                "original_reader_listener_unavailable"
                            )
                        async with session_factory() as db:
                            await db.execute(
                                text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                            )
                            empty = await db.scalar(
                                text(
                                    "SELECT aap_assets_v1.fixture_original_readers_empty("
                                    "CAST(:run_id AS uuid))"
                                ),
                                {"run_id": run_id},
                            )
                        observed_pending = empty is False
                        if raw.is_closed():
                            raise FixturePredictionRetirementBlocked(
                                "original_reader_listener_unavailable"
                            )
                        if empty is True:
                            return
                        if empty is not False:
                            raise FixturePredictionRetirementBlocked(
                                "original_reader_scope_invalid"
                            )
                        await wake.wait()
                finally:
                    failure = sys.exception()
                    raw.remove_termination_listener(disconnected)
                    try:
                        if not subscribed or raw.is_closed():
                            raise FixturePredictionRetirementBlocked(
                                "original_reader_listener_unavailable"
                            )
                        # asyncpg updates its local listener map before the
                        # server acknowledges UNLISTEN.
                        async with asyncio.timeout(_LISTENER_CLOSE_SECONDS):
                            await raw.remove_listener(channel, notified)
                        if raw.is_closed():
                            raise FixturePredictionRetirementBlocked(
                                "original_reader_listener_unavailable"
                            )
                    except BaseException as close_error:
                        # Never return an unproved subscription to the pool.
                        proxy.detach()
                        raw.terminate()
                        if failure is not None and not isinstance(
                            close_error, Exception
                        ):
                            cause = failure
                            if (
                                isinstance(failure, asyncio.CancelledError)
                                and drain_timeout.expired()
                            ):
                                cause = FixturePredictionRetirementBlocked(
                                    "original_reader_pending"
                                    if observed_pending
                                    else "original_reader_deadline"
                                )
                                cause.__cause__ = failure
                            raise close_error from cause
                        if failure is None:
                            if not isinstance(close_error, Exception) or isinstance(
                                close_error, FixturePredictionRetirementBlocked
                            ):
                                raise
                            raise FixturePredictionRetirementBlocked(
                                "original_reader_listener_unavailable"
                            ) from close_error
    except TimeoutError as exc:
        reason = (
            "original_reader_pending"
            if observed_pending
            else "original_reader_deadline"
        )
        raise FixturePredictionRetirementBlocked(reason) from exc
    except (
        SQLAlchemyError,
        asyncpg.PostgresError,
        asyncpg.InterfaceError,
        OSError,
        ConnectionError,
    ) as exc:
        raise FixturePredictionRetirementBlocked(
            "original_reader_status_unavailable"
        ) from exc


async def retire_fixture_prediction_work(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    project_ids: tuple[UUID, ...],
    task_ids: tuple[UUID, ...],
) -> None:
    requests = await _inventory(session_factory, run_id, project_ids, task_ids)
    authority = RoutingAuthority()
    for request in requests:
        try:
            result = await authority.retire_scope(request)
        except RoutingUnavailable as exc:
            raise FixturePredictionRetirementBlocked(
                "scope_retirement_unavailable"
            ) from exc
        if (
            result.state != "committed"
            or not isinstance(result.result, ScopeRetirementView)
            or result.result.phase != "completed"
        ):
            raise FixturePredictionRetirementBlocked("scope_retirement_pending")
    try:
        async with session_factory() as db:
            retired = await db.scalar(
                text(
                    "SELECT aap_inference_routing_v1.fixture_prediction_tests_retired("
                    "CAST(:run_id AS uuid), CAST(:project_ids AS uuid[]), CAST(:task_ids AS uuid[]))"
                ),
                {
                    "run_id": run_id,
                    "project_ids": list(project_ids),
                    "task_ids": list(task_ids),
                },
            )
    except SQLAlchemyError as exc:
        raise FixturePredictionRetirementBlocked("scope_inventory_unavailable") from exc
    if retired is not True:
        raise FixturePredictionRetirementBlocked("scope_retirement_pending")
