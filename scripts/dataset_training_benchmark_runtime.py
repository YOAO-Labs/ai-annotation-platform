"""Observational API/solo-Worker bootstrap for owned local training measurements.

No provisioning, task replacement or application configuration writes occur here.
The parent owns RSS/temp sampling, processes and eventual namespace destruction.
"""

from collections import Counter
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))
current = ContextVar("training_preparation_measurement", default=None)
TRAINING_TASK = "app.workers.training_dataset.run_training_dataset"
_HTTP_ID = re.compile(r"[0-9a-f]{32}")
_TASK_ID = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_SAFE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
_INSTALLED = False


class Measurement:
    def __init__(self, identifier, kind):
        self.identifier, self.kind = identifier, kind
        self.started = time.monotonic()
        self.cpu_started = time.process_time()
        self.values = Counter()
        self.operations = {}
        self.lock = threading.Lock()
        self.finished = False

    def add(self, **values):
        with self.lock:
            if self.finished:
                self.values["late_observer_calls"] += 1
            self.values.update(values)

    def s3_begin(self, name):
        if not isinstance(name, str) or _SAFE_NAME.fullmatch(name) is None:
            name = "other"
        with self.lock:
            if name not in self.operations and len(self.operations) >= 128:
                name = "other"
            operation = self.operations.setdefault(name, Counter())
            operation["calls"] += 1
            self.values.update(s3_calls=1, s3_inflight=1)
        return name

    def s3_end(self, name, started, failed):
        elapsed = time.monotonic() - started
        with self.lock:
            self.values.update(s3_inflight=-1, s3_call_seconds=elapsed)
            self.operations[name]["seconds"] += elapsed
            if failed:
                self.values["s3_failed_calls"] += 1
                self.operations[name]["failed"] += 1

    def finish(self, **facts):
        with self.lock:
            self.finished = True
            ended = time.monotonic()
            values = {
                key: self.values.get(key, 0)
                for key in (
                    "sql_count",
                    "sql_failed_count",
                    "sql_seconds",
                    "sql_inflight",
                    "s3_calls",
                    "s3_failed_calls",
                    "s3_call_seconds",
                    "s3_inflight",
                    "body_open_count",
                    "body_read_calls",
                    "body_read_failed_calls",
                    "body_read_bytes",
                    "body_read_seconds",
                    "body_close_calls",
                    "body_close_successes",
                    "body_close_failed_calls",
                    "body_close_seconds",
                    "body_unclosed_count",
                    "late_observer_calls",
                    "image_inspection_count",
                    "image_inspection_failed_count",
                    "image_inspection_seconds",
                    "body_unmeasurable_reads",
                )
            }
            return {
                "schema": 1,
                "identifier": self.identifier,
                "kind": self.kind,
                "started_monotonic": self.started,
                # The HTTP app/task scope has returned; incomplete_observation
                # counters below prevent confusing this with settled native I/O.
                "settled_monotonic": ended,
                "elapsed_seconds": ended - self.started,
                "cpu_seconds": time.process_time() - self.cpu_started,
                "s3_observation_scope": "current context and propagated native work; independent SDK transfer threads excluded",
                **values,
                "s3_operations": {
                    name: dict(value) for name, value in sorted(self.operations.items())
                },
                "complete_observation": not any(
                    values[key]
                    for key in (
                        "sql_inflight",
                        "s3_inflight",
                        "body_unclosed_count",
                        "body_close_failed_calls",
                        "late_observer_calls",
                        "body_unmeasurable_reads",
                    )
                ),
                **facts,
            }


class _BodyState:
    def __init__(self, measurement):
        self.measurement = measurement
        self.closed = False
        self.lock = threading.Lock()
        measurement.add(body_open_count=1, body_unclosed_count=1)

    def close(self, function, *args):
        started = time.monotonic()
        self.measurement.add(body_close_calls=1)
        try:
            result = function(*args)
        except BaseException:
            self.measurement.add(body_close_failed_calls=1)
            raise
        else:
            with self.lock:
                if not self.closed:
                    self.closed = True
                    self.measurement.add(body_close_successes=1, body_unclosed_count=-1)
            return result
        finally:
            self.measurement.add(body_close_seconds=time.monotonic() - started)


class ObservedBody:
    """Delegate native reads/closure, counting delivered bytes rather than lengths.

    StreamingBody's context manager returns its raw stream. That stream is wrapped
    with the same ownership state, while __exit__ still delegates to the original.
    """

    def __init__(self, body, measurement, *, state=None):
        self.body = body
        self.measurement = measurement
        self.state = state or _BodyState(measurement)

    def __getattr__(self, name):
        return getattr(self.body, name)

    def _read(self, method, args, kwargs, *, readinto=False):
        started = time.monotonic()
        self.measurement.add(body_read_calls=1)
        try:
            result = getattr(self.body, method)(*args, **kwargs)
        except BaseException:
            self.measurement.add(body_read_failed_calls=1)
            raise
        else:
            if readinto:
                size = result if type(result) is int and result >= 0 else 0
            elif isinstance(result, (bytes, bytearray, memoryview)):
                size = len(result)
            elif isinstance(result, list) and all(
                isinstance(value, (bytes, bytearray, memoryview)) for value in result
            ):
                size = sum(len(value) for value in result)
            else:
                size = 0
                self.measurement.add(body_unmeasurable_reads=1)
            self.measurement.add(body_read_bytes=size)
            return result
        finally:
            self.measurement.add(body_read_seconds=time.monotonic() - started)

    def read(self, *args, **kwargs):
        return self._read("read", args, kwargs)

    def read1(self, *args, **kwargs):
        return self._read("read1", args, kwargs)

    def readinto(self, *args, **kwargs):
        return self._read("readinto", args, kwargs, readinto=True)

    def readinto1(self, *args, **kwargs):
        return self._read("readinto1", args, kwargs, readinto=True)

    def readline(self, *args, **kwargs):
        return self._read("readline", args, kwargs)

    def readlines(self, *args, **kwargs):
        return self._read("readlines", args, kwargs)

    def iter_chunks(self, chunk_size=1024):
        while chunk := self.read(chunk_size):
            yield chunk

    def iter_lines(self, chunk_size=1024, keepends=False):
        # Same StreamingBody algorithm; counting chunks preserves newline bytes.
        pending = b""
        for chunk in self.iter_chunks(chunk_size):
            lines = (pending + chunk).splitlines(True)
            for line in lines[:-1]:
                yield line.splitlines(keepends)[0]
            pending = lines[-1]
        if pending:
            yield pending.splitlines(keepends)[0]

    def __iter__(self):
        iterator = iter(self.body)
        while True:
            try:
                yield self._read_iterator(iterator)
            except StopIteration:
                return

    def _read_iterator(self, iterator):
        started = time.monotonic()
        self.measurement.add(body_read_calls=1)
        try:
            value = next(iterator)
        except StopIteration:
            raise
        except BaseException:
            self.measurement.add(body_read_failed_calls=1)
            raise
        else:
            self.measurement.add(body_read_bytes=len(value))
            return value
        finally:
            self.measurement.add(body_read_seconds=time.monotonic() - started)

    def __next__(self):
        return self._read_iterator(self.body)

    def close(self):
        return self.state.close(self.body.close)

    def __enter__(self):
        entered = self.body.__enter__()
        return (
            self
            if entered is self.body
            else ObservedBody(entered, self.measurement, state=self.state)
        )

    def __exit__(self, *args):
        return self.state.close(self.body.__exit__, *args)


def observe_s3_call(original, client, operation, parameters):
    measurement = current.get()
    if measurement is None or client.meta.service_model.service_name != "s3":
        return original(client, operation, parameters)
    started = time.monotonic()
    name = measurement.s3_begin(operation)
    failed = True
    try:
        result = original(client, operation, parameters)
        failed = False
        if isinstance(result, dict) and callable(
            getattr(result.get("Body"), "read", None)
        ):
            result = {**result, "Body": ObservedBody(result["Body"], measurement)}
        return result
    finally:
        measurement.s3_end(name, started, failed)


def sql_before(connection, cursor, statement, parameters, context, many):
    if (measurement := current.get()) is not None:
        context.training_benchmark_statement = (measurement, time.monotonic())
        measurement.add(sql_count=1, sql_inflight=1)


def _sql_finish(context, failed):
    if context is not None and (
        state := getattr(context, "training_benchmark_statement", None)
    ):
        del context.training_benchmark_statement
        measurement, started = state
        measurement.add(sql_inflight=-1, sql_seconds=time.monotonic() - started)
        if failed:
            measurement.add(sql_failed_count=1)


def sql_after(connection, cursor, statement, parameters, context, many):
    _sql_finish(context, False)


def sql_error(exception_context):
    _sql_finish(exception_context.execution_context, True)


def observe_inspection(original, *args, **kwargs):
    measurement = current.get()
    if measurement is None:
        return original(*args, **kwargs)
    started = time.monotonic()
    measurement.add(image_inspection_count=1)
    try:
        return original(*args, **kwargs)
    except BaseException:
        measurement.add(image_inspection_failed_count=1)
        raise
    finally:
        measurement.add(image_inspection_seconds=time.monotonic() - started)


def install_observers():
    global _INSTALLED
    if _INSTALLED:
        return
    from botocore.client import BaseClient
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from app.services import content_snapshot, training_dataset_originals

    original_call, original_inspection = (
        BaseClient._make_api_call,
        content_snapshot._inspect_image,
    )
    original_training_inspection = training_dataset_originals.inspect_image

    def called(client, operation_name, api_params):
        return observe_s3_call(original_call, client, operation_name, api_params)

    def inspected(*args, **kwargs):
        return observe_inspection(original_inspection, *args, **kwargs)

    def training_inspected(*args, **kwargs):
        return observe_inspection(original_training_inspection, *args, **kwargs)

    BaseClient._make_api_call = called
    content_snapshot._inspect_image = inspected
    training_dataset_originals.inspect_image = training_inspected
    event.listen(Engine, "before_cursor_execute", sql_before)
    event.listen(Engine, "after_cursor_execute", sql_after)
    event.listen(Engine, "handle_error", sql_error)
    _INSTALLED = True


def persist(directory, record):
    path = Path(directory) / f"{record['identifier']}.json"
    temporary = path.with_suffix(".pending")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Never replace a measurement with the same ID.
    finally:
        temporary.unlink(missing_ok=True)


class ObservedAPI:
    def __init__(self, app, directory):
        self.app, self.directory = app, Path(directory)

    async def __call__(self, scope, receive, send):
        headers = [
            value
            for name, value in scope.get("headers", [])
            if name == b"x-training-benchmark"
        ]
        try:
            identifier = headers[0].decode("ascii") if len(headers) == 1 else ""
        except UnicodeError:
            identifier = ""
        measured = (
            scope["type"] == "http" and _HTTP_ID.fullmatch(identifier) is not None
        )
        measurement = Measurement(identifier, "http") if measured else None
        token = current.set(measurement)
        facts = {"failed": False, "status": None}

        async def observed_send(message):
            if message["type"] == "http.response.start":
                facts["status"] = message["status"]
                facts["headers_seconds"] = time.monotonic() - measurement.started
            await send(message)

        failed = False
        try:
            return await self.app(scope, receive, observed_send if measured else send)
        except BaseException as exc:
            failed = facts["failed"] = True
            facts["error_type"] = type(exc).__name__
            raise
        finally:
            current.reset(token)
            if measured:
                record = measurement.finish(**facts)
                try:
                    persist(self.directory, record)
                except Exception:
                    if not failed:
                        raise


class TrainingTaskObserver:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.frames = ContextVar("training_task_observer_frames", default=())

    def begin(self, sender=None, task_id=None, **kwargs):
        observed = getattr(sender, "name", None) == TRAINING_TASK
        if observed and (
            not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None
        ):
            raise ValueError("invalid_measurement_task_id")
        measurement = Measurement(task_id, "task") if observed else None
        token = current.set(measurement)
        self.frames.set((*self.frames.get(), (task_id, measurement, token)))

    def finish(self, sender=None, task_id=None, state=None, **kwargs):
        frames = self.frames.get()
        if not frames or frames[-1][0] != task_id:
            return
        _, measurement, token = frames[-1]
        self.frames.set(frames[:-1])
        current.reset(token)
        if measurement is not None:
            state = (
                state
                if isinstance(state, str) and _SAFE_NAME.fullmatch(state)
                else "unknown"
            )
            persist(
                self.directory,
                measurement.finish(
                    task_name=TRAINING_TASK,
                    task_state=state,
                    failed=state not in {"SUCCESS", "RETRY"},
                ),
            )

    def install(self):
        from celery.signals import task_prerun, task_postrun

        task_prerun.connect(self.begin, weak=False)
        task_postrun.connect(self.finish, weak=False)


def environment_contract(root, environ):
    from worktree_env import BUCKETS, load_identity, read_state, state_path, topology

    if environ.get("AAP_WORKTREE_MODE") != "test":
        raise ValueError("owned_test_mode_required")
    resources = topology(root, load_identity(root), "test")
    manifest = read_state(state_path(root, "test", "resources.json"))
    if manifest["resources"] != resources:
        raise ValueError("resource_manifest_mismatch")
    if any(
        environ.get(key) != bucket for key, bucket in resources["buckets"].items()
    ) or set(resources["buckets"]) != set(BUCKETS):
        raise ValueError("nine_owned_buckets_required")
    if any(
        value
        for key, value in environ.items()
        if key.upper()
        in {
            "MIGRATION_DATABASE_URL",
            "TEST_DATABASE_URL",
            "PLAYWRIGHT_E2E_DATABASE_URL",
        }
    ):
        raise ValueError("ordinary_runtime_credentials_required")
    try:
        database = urlsplit(environ.get("DATABASE_URL", ""))
        valid = (
            database.scheme == "postgresql+asyncpg"
            and database.hostname in {"localhost", "127.0.0.1", "::1"}
            and database.hostname == manifest["postgres"]["host"]
            and (database.port or 5432) == manifest["postgres"]["port"]
            and database.path == "/" + resources["database"]
            and not database.query
            and not database.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("exact_local_database_required")
    return resources


def check_runtime_role(row, resources):
    if (
        row["database"] != resources["database"]
        or row["owner"] != resources["owner"]
        or row["privileged"]
    ):
        raise ValueError("ordinary_owned_database_role_required")


async def verify_runtime_role(database_url, resources):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"timeout": 5, "command_timeout": 10},
    )
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT current_database() AS database, "
                            "shobj_description(d.oid,'pg_database') AS owner, "
                            "r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR d.datdba=r.oid "
                            "OR EXISTS(SELECT 1 FROM pg_namespace n WHERE n.nspname='public' AND n.nspowner=r.oid) "
                            "OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relowner=r.oid) AS privileged "
                            "FROM pg_roles r JOIN pg_database d ON d.datname=current_database() WHERE r.rolname=current_user"
                        )
                    )
                )
                .mappings()
                .one()
            )
            check_runtime_role(row, resources)
    finally:
        await engine.dispose()


def verify_runtime_environment():
    import asyncio
    from app.config import settings
    from benchmark_dataset_scan import check_worker_environment
    from worktree_resources import LocalResources
    from worktree_runtime import require_owner, validate_inventory

    resources = environment_contract(ROOT, os.environ)
    backend = LocalResources(resources, settings, code_root=ROOT)
    try:
        observed = backend.inspect()
        validate_inventory(resources, observed)
        if set(observed["buckets"]) != set(resources["buckets"].values()):
            raise ValueError("nine_owned_buckets_required")
        for name, row in (
            (resources["database"], observed["database"]),
            (resources["redis_name"], observed["redis"]),
            *observed["buckets"].items(),
        ):
            if not require_owner(name, row, resources["owner"]):
                raise ValueError("owned_resource_missing")
        check_worker_environment(os.environ, backend.environment())
        asyncio.run(verify_runtime_role(settings.database_url, resources))
        return resources
    finally:
        backend.s3.close()


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"api", "worker"}:
        raise ValueError("expected_api_or_worker_mode")
    mode, *arguments = arguments
    if len(arguments) != (2 if mode == "api" else 3):
        raise ValueError("invalid_runtime_arguments")
    directory = Path(arguments[0])
    if not directory.is_dir():
        raise ValueError("measurement_directory_required")
    verify_runtime_environment()
    install_observers()
    if mode == "api":
        from app.main import app
        import uvicorn

        uvicorn.run(
            ObservedAPI(app, directory),
            host="127.0.0.1",
            port=int(arguments[1]),
            log_level="warning",
        )
    else:
        observer = TrainingTaskObserver(directory)
        observer.install()
        from app.workers.celery_app import celery_app

        celery_app.worker_main(
            [
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "--loglevel=WARNING",
                "--hostname=" + arguments[1],
                "--queues=" + arguments[2],
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ]
        )


if __name__ == "__main__":
    main()
