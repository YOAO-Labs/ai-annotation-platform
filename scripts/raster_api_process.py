"""Return success only after Raster's ordinary API has drained and shut down."""

from __future__ import annotations

import argparse
from pathlib import Path
import signal
import sys

import uvicorn
from uvicorn.importer import import_from_string


def serve_api(app, *, port: int) -> int:
    application = import_from_string(app)
    shutdown_completed = False

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

    server = uvicorn.Server(
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

    # Uvicorn replays the shutdown signal after draining. Keep that replay inside
    # this owned process so a zero status can attest successful ASGI shutdown.
    previous = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        server.run()
    finally:
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
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
    raise SystemExit(serve_api("app.main:app", port=args.port))
