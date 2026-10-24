"""Observability-only API wrapper for the owned directory-original benchmark."""

from contextlib import contextmanager
import contextvars
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))
current = contextvars.ContextVar("directory_read_measurement", default=None)


class ObservedBody:
    def __init__(self, body, metrics):
        self.body, self.metrics = body, metrics

    def __getattr__(self, name):
        return getattr(self.body, name)

    def read(self, *args):
        value = self.body.read(*args)
        self.metrics["source_read_calls"] += 1
        self.metrics["source_bytes"] += len(value)
        return value


def install():
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from app.services import reference_storage, content_snapshot

    original_file = reference_storage.content_file
    original_inspect = content_snapshot._inspect_image

    @contextmanager
    def opened(*args, **kwargs):
        metrics = current.get()
        with original_file(*args, **kwargs) as (body, observation):
            if metrics is not None:
                metrics["source_content_sessions"] += 1
                body = ObservedBody(body, metrics)
            yield body, observation

    def inspected(*args, **kwargs):
        started = time.monotonic()
        try:
            return original_inspect(*args, **kwargs)
        finally:
            if (metrics := current.get()) is not None:
                metrics["image_inspection_seconds"] += time.monotonic() - started

    @event.listens_for(Engine, "before_cursor_execute")
    def before(connection, cursor, statement, parameters, context, many):
        if current.get() is not None:
            context.original_read_started = time.monotonic()

    @event.listens_for(Engine, "after_cursor_execute")
    def after(connection, cursor, statement, parameters, context, many):
        if (metrics := current.get()) is not None:
            metrics["sql_count"] += 1
            metrics["sql_seconds"] += time.monotonic() - context.original_read_started

    reference_storage.content_file = opened
    content_snapshot._inspect_image = inspected


class ObservedAPI:
    def __init__(self, app, directory):
        self.app, self.directory = app, directory

    async def __call__(self, scope, receive, send):
        identifier = (
            dict(scope.get("headers", []))
            .get(b"x-read-benchmark", b"")
            .decode("ascii", errors="ignore")
        )
        if scope["type"] != "http" or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            return await self.app(scope, receive, send)
        metrics = {
            key: 0
            for key in [
                "source_content_sessions",
                "source_read_calls",
                "source_bytes",
                "sql_count",
                "sql_seconds",
                "image_inspection_seconds",
            ]
        }
        token = current.set(metrics)
        started = time.monotonic()

        async def observed_send(message):
            if message["type"] == "http.response.start":
                metrics["status"] = message["status"]
                metrics["headers_seconds"] = time.monotonic() - started
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            settled = time.monotonic()
            metrics["settled_seconds"] = settled - started
            metrics["settled_monotonic"] = settled
            current.reset(token)
            temporary = self.directory / f"{identifier}.pending"
            temporary.write_text(json.dumps(metrics))
            temporary.replace(self.directory / f"{identifier}.json")


if __name__ == "__main__":
    from benchmark_dataset_scan import verify_worker_environment

    verify_worker_environment()
    directory, port = sys.argv[1:]
    install()
    from app.main import app
    import uvicorn

    uvicorn.run(
        ObservedAPI(app, Path(directory)),
        host="127.0.0.1",
        port=int(port),
        log_level="warning",
    )
