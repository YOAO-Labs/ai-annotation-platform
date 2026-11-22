"""Return success only after Raster's ordinary API has drained and shut down."""

from __future__ import annotations

import argparse
from contextlib import AsyncExitStack
import os
from pathlib import Path
import signal
import socket
import sys
from uuid import UUID

import uvicorn
from uvicorn.importer import import_from_string


def serve_api(app, *, port: int, ready_fd: int | None = None, playback=None) -> int:
    application = import_from_string(app)
    shutdown_completed = False
    controls = AsyncExitStack()

    async def observed_application(scope, receive, send):
        if scope["type"] != "lifespan":
            await application(scope, receive, send)
            return

        async def observed_send(message):
            nonlocal shutdown_completed
            await send(message)
            if message["type"] == "lifespan.shutdown.complete":
                shutdown_completed = True

        await application(scope, receive, observed_send)

    class ObservedServer(uvicorn.Server):
        async def startup(self, sockets=None):
            nonlocal ready_fd
            await super().startup(sockets=sockets)
            if self.started and playback is not None:
                from fixture_playback_control import serve_playback_control
                from app.services.original_playback import owned_playback_contexts

                await controls.enter_async_context(
                    serve_playback_control(
                        *playback,
                        application.state.original_playback_registry,
                        owned_playback_contexts,
                    )
                )
            if self.started and ready_fd is not None:
                try:
                    if os.write(ready_fd, b"ready\n") != 6:
                        raise RuntimeError("API readiness acknowledgement incomplete")
                finally:
                    os.close(ready_fd)
                    ready_fd = None

        async def shutdown(self, sockets=None):
            try:
                await controls.aclose()
            finally:
                await super().shutdown(sockets=sockets)

    server = ObservedServer(
        uvicorn.Config(
            observed_application,
            host="127.0.0.1",
            port=port,
            lifespan="on",
            timeout_graceful_shutdown=None,
        )
    )

    def request_stop(_signal, _frame):
        server.should_exit = True

    previous = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        server.run()
    finally:
        if ready_fd is not None:
            os.close(ready_fd)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return (
        0
        if (
            shutdown_completed
            and not server.force_exit
            and not server.lifespan.error_occurred
        )
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ready-fd", type=int, required=True)
    parser.add_argument("--playback-fd", type=int, required=True)
    parser.add_argument("--playback-suite", type=UUID, required=True)
    parser.add_argument("--playback-verifier", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
    with socket.socket(fileno=args.playback_fd) as endpoint:
        raise SystemExit(
            serve_api(
                "app.main:app",
                port=args.port,
                ready_fd=args.ready_fd,
                playback=(endpoint, args.playback_suite, args.playback_verifier),
            )
        )
