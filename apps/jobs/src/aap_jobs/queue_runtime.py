"""Private HTTP owner for existing Celery broker dispatch and revoke calls."""

from __future__ import annotations

import asyncio
import hmac
import logging

from .queue_config import JobsQueueSettings
from .queue_contract import (
    JOBS_QUEUE_PATH,
    JobsQueueContractError,
    QueueDispatch,
    QueueRevoke,
    decode_queue_request,
    encode_queue_reply,
    load_queue_configuration,
)

log = logging.getLogger("aap_jobs.queue.runtime")


class CeleryQueueTransport:
    """Use Celery's existing JSON routes and publisher retry policy."""

    def __init__(self, settings: JobsQueueSettings, *, celery_app=None):
        from celery import Celery

        self.app = celery_app or Celery("aap_jobs_queue", broker=settings.broker_url)
        config = load_queue_configuration()
        self.app.conf.update(
            task_serializer=config["task_serializer"],
            result_serializer=config["result_serializer"],
            accept_content=config["accept_content"],
            task_default_queue=config["task_default_queue"],
            task_routes=config["task_routes"],
        )

    def dispatch(self, request: QueueDispatch) -> None:
        self.app.send_task(
            request.task_name,
            args=request.args,
            kwargs=request.kwargs,
            task_id=request.execution_id,
        )

    def revoke(self, request: QueueRevoke) -> None:
        self.app.control.revoke(request.execution_id, terminate=False)

    def ping(self) -> None:
        connection = self.app.connection_for_read()
        try:
            connection.connect_timeout = 3
            connection.connect()
        finally:
            connection.release()

    def close(self) -> None:
        self.app.close()


def create_app(settings: JobsQueueSettings, transport=None):
    """Build the authenticated private queue API without connecting at import."""
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    owns_transport = transport is None
    queue_transport = transport or CeleryQueueTransport(settings)

    def reply(request_id: str, accepted: bool, *, status_code: int = 200):
        try:
            body = encode_queue_reply(
                accepted=accepted,
                execution_id=request_id,
            )
        except JobsQueueContractError:
            return Response(status_code=500)
        return Response(
            body,
            status_code=status_code,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    @asynccontextmanager
    async def lifespan(app):
        app.state.queue_transport = queue_transport
        try:
            yield
        finally:
            if owns_transport:
                try:
                    queue_transport.close()
                except Exception:
                    log.warning("Jobs queue transport close failed")

    async def live(_request: Request):
        return Response(b'{"status":"ok"}', media_type="application/json")

    async def ready(request: Request):
        try:
            await asyncio.to_thread(request.app.state.queue_transport.ping)
        except asyncio.CancelledError:
            raise
        except Exception:
            return Response(
                b'{"status":"unavailable"}',
                status_code=503,
                media_type="application/json",
            )
        return Response(b'{"status":"ok"}', media_type="application/json")

    async def operate(request: Request):
        expected = b"Bearer " + settings.token.encode("ascii")
        supplied = request.headers.get("authorization", "")
        try:
            authorized = hmac.compare_digest(supplied.encode("ascii"), expected)
        except (UnicodeEncodeError, TypeError):
            authorized = False
        if not authorized:
            return Response(status_code=401)

        try:
            operation = decode_queue_request(await request.body())
        except JobsQueueContractError:
            return Response(status_code=400)

        try:
            if isinstance(operation, QueueDispatch):
                await asyncio.to_thread(
                    request.app.state.queue_transport.dispatch,
                    operation,
                )
            else:
                await asyncio.to_thread(
                    request.app.state.queue_transport.revoke,
                    operation,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Jobs queue operation was not accepted")
            return reply(operation.execution_id, False, status_code=503)
        return reply(operation.execution_id, True)

    return Starlette(
        routes=[
            Route("/health/live", live, methods=["GET"]),
            Route("/health/ready", ready, methods=["GET"]),
            Route(JOBS_QUEUE_PATH, operate, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    import uvicorn

    settings = JobsQueueSettings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
