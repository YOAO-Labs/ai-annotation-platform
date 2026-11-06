"""Exact fixture media execution fencing on a private worker connection."""

from __future__ import annotations

import asyncio
import builtins
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.services import storage_access
from app.services.content_snapshot import ContentError


_active = ContextVar("fixture_media_execution", default=None)


def current():
    return _active.get()

def request_soft_limit(request):
    limits = getattr(request, "timelimit", None)
    if limits is None:
        return None
    if not isinstance(limits, (tuple, list)) or len(limits) != 2:
        raise ContentError("fixture_media_scope_untracked", 409)
    return limits[1]


def _control_url(value=None):
    try:
        if value is None:
            selector = os.environ.get("AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE")
            if not selector or not Path(selector).is_absolute():
                raise ContentError("fixture_media_executor_unavailable", 503)
            descriptor = os.open(selector, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
                    raise ContentError("fixture_media_executor_unavailable", 503)
                value = stream.read(8193)
            if len(value) > 8192:
                raise ContentError("fixture_media_executor_unavailable", 503)
            value = value.strip()
        url = make_url(value)
        business = make_url(settings.database_url)
        if (
            url.drivername != "postgresql+asyncpg"
            or not url.username
            or not url.password
            or url.host != business.host
            or (url.port or 5432) != (business.port or 5432)
            or url.database != business.database
            or url.query
        ):
            raise ContentError("fixture_media_executor_unavailable", 503)
        return url
    except (OSError, ValueError, TypeError):
        raise ContentError("fixture_media_executor_unavailable", 503) from None


async def is_fixture(kind, identifier):
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            result = await connection.scalar(
                text("SELECT aap_jobs_v1.fixture_media_work_exists(:kind,:id)"),
                {"kind": kind, "id": str(identifier)},
            )
            if type(result) is not bool:
                raise ContentError("fixture_media_scope_unavailable", 503)
            return result
    finally:
        await engine.dispose()


async def is_fixture_owner(kind, identifier):
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            result = await connection.scalar(
                text("SELECT aap_assets_v1.fixture_media_owner_exists(:kind,:id)"),
                {"kind": kind, "id": identifier},
            )
            if type(result) is not bool:
                raise ContentError("fixture_media_scope_unavailable", 503)
            return result
    finally:
        await engine.dispose()


class Execution:
    def __init__(self, connection, kind, identifier, token):
        self.connection = connection
        self.kind = kind
        self.identifier = identifier
        self.token = token

    async def target(self):
        result = await self.connection.scalar(
            text("SELECT aap_assets_v1.fixture_legacy_execution_target(:kind,:id,:token)"),
            {"kind": self.kind, "id": self.identifier, "token": self.token},
        )
        if (
            not isinstance(result, dict)
            or set(result) != {
                "run_id", "endpoint", "bucket", "storage_identity", "receipt_sha256",
                "location_id", "binding_generation",
            }
            or type(result["binding_generation"]) is not int
            or result["binding_generation"] < 1
        ):
            raise ContentError("fixture_media_target_unavailable", 503)
        await self.connection.commit()
        return result

    def require_source(self, receipt):
        if self.kind == "independent" and receipt != self.independent_source:
            raise ContentError("fixture_media_source_changed", 409)

    @asynccontextmanager
    async def birth_scope(self, business, dataset, item, task, *, delivery=None):
        admission = uuid.uuid4()
        label = f"aap-fixture-native-source:{admission}"
        pid = await business.scalar(text("SELECT pg_catalog.pg_backend_pid()"))
        admitted = False
        locked = False
        try:
            locked = True
            await business.execute(
                text("SELECT pg_catalog.pg_advisory_lock_shared(pg_catalog.hashtextextended(:label,0))"),
                {"label": label},
            )
            accepted = await self.connection.scalar(
                text(
                    "SELECT aap_assets_v1.begin_fixture_legacy_birth_admission("
                    ":kind,:execution,:token,:dataset,:item,:task,:delivery,:pid,:admission)"
                ),
                {
                    "kind": self.kind, "execution": self.identifier, "token": self.token,
                    "dataset": dataset, "item": item, "task": task, "delivery": delivery,
                    "pid": pid, "admission": admission,
                },
            )
            if accepted is not True:
                raise ContentError("fixture_media_birth_unavailable", 503)
            admitted = True
            await self.connection.commit()
            yield
            finished = await self.connection.scalar(
                text("SELECT aap_assets_v1.finish_fixture_legacy_birth_admission(:admission)"),
                {"admission": admission},
            )
            if finished is not True:
                raise ContentError("fixture_media_birth_unsettled", 409)
            await self.connection.commit()
            admitted = False
        except BaseException as exc:
            if admitted or locked:
                storage_access.mark_unsettled(exc)
            raise
        finally:
            primary = sys.exception()
            try:
                released = True
                if locked:
                    released = await business.scalar(
                        text("SELECT pg_catalog.pg_advisory_unlock_shared(pg_catalog.hashtextextended(:label,0))"),
                        {"label": label},
                    )
                if released is not True:
                    raise ContentError("fixture_media_birth_unsettled", 409)
            except BaseException as close_error:
                storage_access.mark_unsettled(close_error)
                if primary is not None:
                    combined = builtins.BaseExceptionGroup(
                        "Fixture media birth and business lock release failed", [primary, close_error]
                    )
                    storage_access.mark_unsettled(combined)
                    raise combined from None
                raise


    async def bind_independent_child(self, child):
        try:
            result = await self.connection.scalar(
                text("SELECT aap_assets_v1.bind_fixture_independent_child(:parent,:token,:child)"),
                {"parent": self.identifier, "token": self.token, "child": child},
            )
            if result is not True:
                raise ContentError("fixture_media_child_unavailable", 503)
            await self.connection.commit()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)

    async def prepare_pyramid(self, item_id, delegation):
        child = await self.connection.scalar(
            text(
                "SELECT aap_jobs_v1.prepare_fixture_media_child("
                ":job,:token,:child,:item,CAST(:delegation AS jsonb))"
            ),
            {
                "job": self.identifier,
                "token": self.token,
                "child": uuid.uuid4(),
                "item": item_id,
                "delegation": json.dumps(delegation, allow_nan=False),
            },
        )
        if (
            not isinstance(child, dict)
            or set(child) != {"child_id", "queue"}
            or not isinstance(child["queue"], str)
            or not child["queue"]
        ):
            raise ContentError("fixture_media_child_unavailable", 503)
        try:
            identifier = uuid.UUID(child["child_id"])
        except (TypeError, ValueError):
            raise ContentError("fixture_media_child_unavailable", 503) from None
        await self.connection.commit()
        return identifier, child["queue"]

    async def bind_generation(self, asset_id, generation):
        try:
            name = (
                "aap_assets_v1.bind_fixture_independent_pyramid"
                if self.kind == "independent"
                else "aap_jobs_v1.bind_fixture_media_child_generation"
            )
            result = await self.connection.scalar(
                text(f"SELECT {name}(:child,:token,:asset,:generation)"),
                {
                    "child": self.identifier,
                    "token": self.token,
                    "asset": asset_id,
                    "generation": generation,
                },
            )
            if result is not True:
                raise ContentError("fixture_media_child_unavailable", 503)
            await self.connection.commit()
        except BaseException as exc:
            # The generation may already be durably born, or the binding
            # commit may have reached PostgreSQL despite a lost response.
            raise storage_access.mark_unsettled(exc)

    async def record_pyramid_attempt(self, asset_id, generation, lease, output):
        try:
            name = (
                "aap_assets_v1.record_fixture_independent_pyramid_attempt"
                if self.kind == "independent"
                else "aap_jobs_v1.record_fixture_pyramid_attempt"
            )
            result = await self.connection.scalar(
                text(f"SELECT {name}(:child,:token,:asset,:generation,:lease,:output)"),
                {
                    "child": self.identifier,
                    "token": self.token,
                    "asset": asset_id,
                    "generation": generation,
                    "lease": lease,
                    "output": output,
                },
            )
            if result is not True:
                raise ContentError("fixture_media_attempt_unavailable", 503)
            await self.connection.commit()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)

    async def record_video_attempt(self, output, lease):
        try:
            name = (
                "aap_assets_v1.record_fixture_independent_video_attempt"
                if self.kind == "independent"
                else "aap_jobs_v1.record_fixture_video_attempt"
            )
            result = await self.connection.scalar(
                text(f"SELECT {name}(:job,:token,:output,:lease)"),
                {"job": self.identifier, "token": self.token, "output": output, "lease": lease},
            )
            if result is not True:
                raise ContentError("fixture_media_attempt_unavailable", 503)
            await self.connection.commit()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)

    async def begin_version(self, owner_kind, owner_id, endpoint, bucket, key, receipt):
        identifier = await self.connection.scalar(
            text(
                "SELECT aap_assets_v1.begin_fixture_media_version("
                ":kind,:execution,:token,:owner_kind,:owner_id,:endpoint,:bucket,"
                ":key,:sha,:size,:id)"
            ),
            {
                "kind": self.kind,
                "execution": self.identifier,
                "token": self.token,
                "owner_kind": owner_kind,
                "owner_id": owner_id,
                "endpoint": endpoint,
                "bucket": bucket,
                "key": key,
                "sha": receipt["sha256"],
                "size": receipt["size_bytes"],
                "id": uuid.uuid4(),
            },
        )
        if not isinstance(identifier, uuid.UUID):
            raise ContentError("fixture_media_version_unavailable", 503)
        await self.connection.commit()
        return identifier

    async def record_version(self, identifier, etag, version):
        result = await self.connection.scalar(
            text(
                "SELECT aap_assets_v1.record_fixture_media_version("
                ":kind,:execution,:token,:id,:etag,:version)"
            ),
            {
                "kind": self.kind,
                "execution": self.identifier,
                "token": self.token,
                "id": identifier,
                "etag": etag,
                "version": version,
            },
        )
        if result is not True:
            raise ContentError("fixture_media_version_unavailable", 503)
        await self.connection.commit()

    async def verify_version(self, identifier, version):
        result = await self.connection.scalar(
            text(
                "SELECT aap_assets_v1.verify_fixture_media_version("
                ":kind,:execution,:token,:id,:version)"
            ),
            {
                "kind": self.kind,
                "execution": self.identifier,
                "token": self.token,
                "id": identifier,
                "version": version,
            },
        )
        if result is not True:
            raise ContentError("fixture_media_version_unavailable", 503)
        await self.connection.commit()


async def enqueue_pyramid(db, item_id, delegation):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.jobs_dispatch import prepare_job_dispatch, publish_prepared_dispatch
    from app.services.jobs_queue import dispatch_job

    execution = current()
    if execution is None or execution.kind != "backfill":
        raise ContentError("fixture_media_execution_unavailable", 503)
    child, queue = await execution.prepare_pyramid(item_id, delegation)
    task_name = "app.workers.image_pyramid.generate_image_pyramid"
    args = ["dataset_item", str(item_id), False, delegation]
    options = {"queue": queue}
    async with AsyncSession(bind=storage_access.engine(db), expire_on_commit=False) as owner:
        lease = await prepare_job_dispatch(
            owner,
            job_id=execution.identifier,
            task_name=task_name,
            execution_id=str(child),
            args=args,
            options=options,
        )
        await owner.commit()
        await publish_prepared_dispatch(
            owner,
            lease,
            publish=lambda: dispatch_job(
                task_name=task_name,
                execution_id=str(child),
                args=args,
                options=options,
            ),
        )


class ScreenshotProducer:
    def __init__(self, connection, admission, delivery_id):
        self.connection = connection
        self.admission = admission
        self.delivery_id = delivery_id
        self.settled = False
        self.rejected = False

    async def target(self):
        result = await self.connection.scalar(
            text("SELECT aap_assets_v1.fixture_screenshot_producer_target(:admission)"),
            {"admission": self.admission},
        )
        if not isinstance(result, dict) or set(result) != {
            "run_id", "endpoint", "bucket", "storage_identity", "receipt_sha256",
            "location_id", "binding_generation",
        }:
            raise ContentError("fixture_media_target_unavailable", 503)
        await self.connection.commit()
        return result

    async def settle(self, output_id, delivery_id):
        if self.settled or uuid.UUID(str(delivery_id)) != self.delivery_id:
            raise ContentError("fixture_media_scope_mismatch", 409)
        try:
            result = await self.connection.scalar(
                text("SELECT aap_assets_v1.finish_fixture_screenshot_producer(:admission,:delivery,:output)"),
                {"admission": self.admission, "delivery": self.delivery_id, "output": output_id},
            )
            if result is not True:
                raise ContentError("fixture_media_birth_unsettled", 409)
            await self.connection.commit()
            self.settled = True
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)

    async def reuse(self, output_id):
        if self.settled:
            raise ContentError("fixture_media_scope_mismatch", 409)
        try:
            result = await self.connection.scalar(
                text("SELECT aap_assets_v1.reuse_fixture_screenshot_producer(:admission,:output)"),
                {"admission": self.admission, "output": output_id},
            )
            if result not in {"reused", "unqualified"}:
                raise ContentError("fixture_media_birth_unsettled", 409)
            await self.connection.commit()
            self.settled = True
            self.rejected = result == "unqualified"
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        if self.rejected:
            raise ContentError("fixture_media_reuse_unqualified", 409)


@asynccontextmanager
async def screenshot_producer_scope(business, run_id, root_admission_id, dataset_id, item_id, control_url=None):
    """Bind the existing screenshot admission to one actual producer UOW."""
    if _active.get() is not None:
        raise ContentError("fixture_media_scope_mismatch", 409)
    if hasattr(control_url, "render_as_string"):
        control_url = control_url.render_as_string(hide_password=False)
    engine = create_async_engine(_control_url(control_url), poolclass=NullPool, hide_parameters=True)
    admission, delivery = uuid.uuid4(), uuid.uuid4()
    label = f"aap-fixture-native-source:{admission}"
    locked = admitted = False
    active_token = None
    failure = producer = None
    try:
        pid = await business.scalar(text("SELECT pg_catalog.pg_backend_pid()"))
        locked = True
        await business.execute(
            text("SELECT pg_catalog.pg_advisory_lock_shared(pg_catalog.hashtextextended(:label,0))"),
            {"label": label},
        )
        async with engine.connect() as control:
            admitted = True
            accepted = await control.scalar(
                text("SELECT aap_assets_v1.begin_fixture_screenshot_producer("
                     ":run,:root_admission,:dataset,:item,:delivery,:pid,:admission)"),
                {"run": run_id, "root_admission": root_admission_id, "dataset": dataset_id,
                 "item": item_id, "delivery": delivery, "pid": pid, "admission": admission},
            )
            if accepted is not True:
                admitted = False
                raise ContentError("fixture_media_birth_unavailable", 503)
            await control.commit()
            producer = ScreenshotProducer(control, admission, delivery)
            active_token = _active.set(producer)
            try:
                yield producer
                if not producer.settled:
                    raise ContentError("fixture_media_birth_unsettled", 409)
                admitted = False
            finally:
                _active.reset(active_token)
                active_token = None
    except BaseException as exc:
        failure = exc
        if (admitted or locked) and not (
            producer is not None and producer.rejected
            and isinstance(exc, ContentError) and exc.code == "fixture_media_reuse_unqualified"
        ):
            storage_access.mark_unsettled(exc)
        raise
    finally:
        errors = []
        if active_token is not None:
            _active.reset(active_token)
        if locked:
            try:
                released = await business.scalar(
                    text("SELECT pg_catalog.pg_advisory_unlock_shared(pg_catalog.hashtextextended(:label,0))"),
                    {"label": label},
                )
                if released is not True:
                    raise ContentError("fixture_media_birth_unsettled", 409)
            except BaseException as exc:
                errors.append(storage_access.mark_unsettled(exc))
        try:
            await engine.dispose()
        except BaseException as exc:
            errors.append(storage_access.mark_unsettled(exc))
        if errors:
            if failure is not None:
                errors.insert(0, failure)
            combined = builtins.BaseExceptionGroup("Fixture screenshot producer close failed", errors)
            storage_access.mark_unsettled(combined)
            raise combined from None


@asynccontextmanager
async def admitted_independent(delivery_id, task_name, queue, args, kwargs, *, soft_time_limit=None):
    """Admit the exact durable Jobs delivery on its function-only session."""
    try:
        identifier = uuid.UUID(delivery_id)
    except (ValueError, TypeError, AttributeError):
        raise ContentError("fixture_media_scope_untracked", 409) from None
    envelope = {
        "schema": "aap-jobs-queue/v2", "operation": "dispatch",
        "task_name": task_name, "execution_id": str(identifier),
        "args": args, "kwargs": kwargs, "options": {"queue": queue},
    }
    engine = create_async_engine(_control_url(), poolclass=NullPool, hide_parameters=True)
    failure = None
    try:
        async with engine.connect() as connection:
            saved = await connection.scalar(
                text("SELECT aap_assets_v1.fixture_independent_delivery_envelope(:id)"),
                {"id": identifier},
            )
            if saved is None:
                yield False
                return
            options = saved.get("options") if isinstance(saved, dict) else None
            if (
                not isinstance(options, dict)
                or set(options) not in ({"queue"}, {"queue", "soft_time_limit"})
                or options.get("queue") != queue
                or ("soft_time_limit" in options and options["soft_time_limit"] != soft_time_limit)
            ):
                yield False
                return
            envelope["options"] = options
            if saved != envelope:
                yield False
                return
            token = uuid.uuid4()
            try:
                result = await connection.scalar(
                    text("SELECT aap_assets_v1.begin_fixture_independent_execution(:id,:token,CAST(:envelope AS jsonb))"),
                    {"id": identifier, "token": token, "envelope": json.dumps(envelope, allow_nan=False)},
                )
                await connection.commit()
            except BaseException as exc:
                raise storage_access.mark_unsettled(exc)
            if result is None:
                yield False
                return
            if (
                not isinstance(result, dict)
                or result.get("schema") != "aap-fixture-independent-execution/v1"
                or result.get("delivery_id") != str(identifier)
                or result.get("envelope") != envelope
                or not isinstance(result.get("source"), dict)
            ):
                raise storage_access.mark_unsettled(
                    ContentError("fixture_media_execution_unavailable", 503)
                )
            execution = Execution(connection, "independent", identifier, token)
            execution.independent_source = result["source"]
            marker = _active.set(execution)
            try:
                yield True
            except BaseException as exc:
                failure = exc
                raise
            finally:
                _active.reset(marker)
                if not storage_access.failure_unsettled(failure):
                    try:
                        completed = await connection.scalar(
                            text("SELECT aap_assets_v1.finish_fixture_independent_execution(:id,:token)"),
                            {"id": identifier, "token": token},
                        )
                        if completed is not True:
                            raise ContentError("fixture_media_execution_unsettled", 503)
                        await connection.commit()
                    except BaseException as finish_error:
                        storage_access.mark_unsettled(finish_error)
                        if isinstance(failure, asyncio.CancelledError):
                            storage_access.mark_unsettled(failure)
                            raise failure from finish_error
                        if failure is not None:
                            raise builtins.BaseExceptionGroup(
                                "Fixture independent execution and settlement failed", [failure, finish_error]
                            ) from None
                        raise
    finally:
        primary = sys.exception()
        try:
            await engine.dispose()
        except BaseException as close_error:
            storage_access.mark_unsettled(close_error)
            if isinstance(primary, asyncio.CancelledError):
                storage_access.mark_unsettled(primary)
                raise primary from close_error
            if primary is not None:
                raise builtins.BaseExceptionGroup(
                    "Fixture independent execution and shutdown failed", [primary, close_error]
                ) from None
            raise


@asynccontextmanager
async def admitted(
    kind, identifier, queue, *, delivery_id=None, item_id=None, delegation=None
):
    """Hold the exact native session lock until real work and close complete."""
    engine = create_async_engine(_control_url(), poolclass=NullPool, hide_parameters=True)
    failure = None
    try:
        async with engine.connect() as connection:
            label = f"aap-fixture-media:{kind}:{identifier}"
            locked = await connection.scalar(
                text(
                    "SELECT pg_catalog.pg_try_advisory_lock("
                    "pg_catalog.hashtextextended(:label,0))"
                ),
                {"label": label},
            )
            await connection.commit()
            if locked is not True:
                raise ContentError("fixture_media_execution_busy", 409)
            token = uuid.uuid4()
            if kind == "backfill":
                statement = text(
                    "SELECT aap_jobs_v1.begin_fixture_media_backfill("
                    ":id,:task,:queue,:token)"
                )
                parameters = {
                    "id": identifier,
                    "task": delivery_id,
                    "queue": queue,
                    "token": token,
                }
            elif kind == "pyramid":
                statement = text(
                    "SELECT aap_jobs_v1.begin_fixture_media_child("
                    ":id,:queue,:item,CAST(:delegation AS jsonb),:token)"
                )
                parameters = {
                    "id": identifier,
                    "queue": queue,
                    "item": item_id,
                    "delegation": json.dumps(delegation, allow_nan=False),
                    "token": token,
                }
            else:
                raise ValueError("invalid fixture media execution kind")
            result = await connection.scalar(statement, parameters)
            await connection.commit()
            if result in {"blocked", "settled"}:
                yield False
                return
            if result != "started":
                raise ContentError("fixture_media_execution_unavailable", 503)
            execution = Execution(connection, kind, identifier, token)
            marker = _active.set(execution)
            try:
                yield True
            except BaseException as exc:
                failure = exc
                raise
            finally:
                _active.reset(marker)
                if not storage_access.failure_unsettled(failure):
                    try:
                        name = (
                            "finish_fixture_media_backfill"
                            if kind == "backfill"
                            else "finish_fixture_media_child"
                        )
                        completed = await connection.scalar(
                            text(f"SELECT aap_jobs_v1.{name}(:id,:token)"),
                            {"id": identifier, "token": token},
                        )
                        if completed is not True:
                            raise ContentError("fixture_media_execution_unsettled", 503)
                        await connection.commit()
                    except BaseException as finish_error:
                        storage_access.mark_unsettled(finish_error)
                        if isinstance(failure, asyncio.CancelledError):
                            storage_access.mark_unsettled(failure)
                            raise failure from finish_error
                        if failure is not None:
                            raise builtins.BaseExceptionGroup(
                                "Fixture media execution and settlement failed",
                                [failure, finish_error],
                            ) from None
                        raise
    finally:
        primary = sys.exception()
        try:
            await engine.dispose()
        except BaseException as close_error:
            storage_access.mark_unsettled(close_error)
            if isinstance(primary, asyncio.CancelledError):
                storage_access.mark_unsettled(primary)
                raise primary from close_error
            if primary is not None:
                raise builtins.BaseExceptionGroup(
                    "Fixture media execution and shutdown failed", [primary, close_error]
                ) from None
            raise
