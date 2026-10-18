"""Start the independent media control process; worker has its own entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict

import asyncpg
import uvicorn

from .api import create_app
from .control import MediaControl
from .control_config import load_control_config
from .database import AsyncpgMediaDatabase
from .health import check_capabilities
from .storage import S3MediaStorage


def configured_app(config):
    state = {}

    class Control:
        def __getattr__(self, name):
            return getattr(state["control"], name)

    async def ready():
        return "pool" in state and await check_capabilities(state["pool"], "control")

    app = create_app(Control(), config.token, readiness=ready)

    @asynccontextmanager
    async def lifespan(app):
        pool = await asyncpg.create_pool(
            config.database_url,
            min_size=config.pool_min_size,
            max_size=config.pool_max_size,
            timeout=config.pool_timeout_seconds,
            command_timeout=config.sql_timeout_seconds,
        )
        state["pool"] = pool
        try:
            if not await check_capabilities(pool, "control"):
                raise RuntimeError("Media control SQL capability qualification failed")
            state["control"] = MediaControl(
                AsyncpgMediaDatabase(pool, allowed_roles={"control", "system"}),
                S3MediaStorage(**asdict(config.storage)),
            )
            yield
        finally:
            state.clear()
            await pool.close()

    app.router.lifespan_context = lifespan
    return app


def main():
    config = load_control_config()
    uvicorn.run(
        configured_app(config),
        host=config.host,
        port=config.port,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
