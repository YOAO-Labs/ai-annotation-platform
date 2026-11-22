"""Idempotently create the isolated PostgreSQL database named by DATABASE_URL."""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine


def _validated_target_url(database_url: str) -> URL:
    url = make_url(database_url)
    database_name = url.database or ""
    if url.get_backend_name() != "postgresql":
        raise ValueError("DATABASE_URL must use PostgreSQL")
    if not database_name.lower().endswith(("_e2e", "_test")):
        raise ValueError("DATABASE_URL database name must end with _e2e or _test")
    return url


async def prepare_e2e_database(database_url: str) -> bool:
    """Create the target database if absent; return whether it was created."""
    target_url = _validated_target_url(database_url)
    target_database = target_url.database
    assert target_database is not None

    maintenance_url = target_url.set(database="postgres")
    engine = create_async_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_advisory_lock(hashtext(:database_name))"),
                {"database_name": target_database},
            )
            try:
                exists = await connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                    {"database_name": target_database},
                )
                if exists:
                    return False

                quoted_database = engine.dialect.identifier_preparer.quote_identifier(
                    target_database
                )
                await connection.exec_driver_sql(f"CREATE DATABASE {quoted_database}")
                return True
            finally:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:database_name))"),
                    {"database_name": target_database},
                )
    finally:
        await engine.dispose()


async def _main() -> None:
    database_url = os.environ["DATABASE_URL"]
    target_url = _validated_target_url(database_url)
    created = await prepare_e2e_database(database_url)
    action = "created" if created else "already exists"
    print(f"Database {target_url.database!r} {action}.")


if __name__ == "__main__":
    asyncio.run(_main())
