"""Bind Celery execution to an exact function-only Assets LOGIN."""

from __future__ import annotations

from asyncio import Lock
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings


@asynccontextmanager
async def executor_sessions(lane: Literal["image", "video"]):
    name = f"ASSETS_CELERY_{lane.upper()}_DATABASE_URL"
    if lane == "image":
        from yoao_assets_images_runtime.health import check_connection
    else:
        from yoao_assets_media_runtime.health import (
            check_worker_connection as check_connection,
        )

    engine = factory = None
    initialized = Lock()

    @asynccontextmanager
    async def session():
        nonlocal engine, factory
        if factory is None:
            async with initialized:
                if factory is None:
                    direct = getattr(settings, name.lower())
                    path = getattr(settings, name.lower() + "_file")
                    if not (direct or path):
                        raise ValueError(
                            f"{name} or {name}_FILE is required for Celery execution"
                        )
                    if direct and path:
                        raise ValueError(f"Set only one of {name} and {name}_FILE")
                    try:
                        if path:
                            with Path(path).open(encoding="utf-8") as secret:
                                value = secret.read(4097)
                        else:
                            value = direct
                        if len(value) > 4096:
                            raise ValueError
                        url = make_url(value.strip())
                        if (
                            url.get_backend_name() not in {"postgres", "postgresql"}
                            or not (url.host and url.username and url.database)
                            or url.database != make_url(settings.database_url).database
                        ):
                            raise ValueError
                    except (ArgumentError, OSError, ValueError):
                        raise ValueError(f"Invalid {name} configuration") from None
                    engine = create_async_engine(
                        url.set(drivername="postgresql+asyncpg"),
                        hide_parameters=True,
                        connect_args={"timeout": 5},
                    )
                    ready = False
                    try:
                        async with engine.connect() as connection:
                            raw = await connection.get_raw_connection()
                            ready = await check_connection(raw.driver_connection)
                    except Exception:
                        raise RuntimeError(f"{name} is unavailable") from None
                    finally:
                        if not ready:
                            await engine.dispose()
                            engine = None
                    if not ready:
                        raise RuntimeError(
                            f"{name} must use the exact Assets worker LOGIN"
                        )
                    factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            yield db

    try:
        yield session
    finally:
        if engine is not None:
            await engine.dispose()
