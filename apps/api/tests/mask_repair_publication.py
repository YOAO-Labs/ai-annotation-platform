"""A committed repair database with a separate, function-only Jobs publisher."""

from contextlib import asynccontextmanager
import uuid

import pytest
from yoao_jobs.publication_postgres import PUBLICATION_FUNCTIONS

from app.config import settings
from app.workers.jobs_dispatch import _verify_login
from tests.managed_image_inference_fixtures import (
    ManagedDatabaseIdentity,
    managed_database,
)
from tests.test_prediction_inputs import input_storage_world


@asynccontextmanager
async def _publication_login(test_engine):
    role = f"mask_publication_{uuid.uuid4().hex}"
    password = uuid.uuid4().hex
    async with test_engine.begin() as owner:
        await owner.exec_driver_sql(
            f"CREATE ROLE {role} LOGIN NOINHERIT PASSWORD '{password}'"
        )
    try:
        yield role, password
    finally:
        async with test_engine.begin() as owner:
            await owner.exec_driver_sql(f"DROP ROLE {role}")


@asynccontextmanager
async def published_repair_database(
    test_engine, monkeypatch, *, prefix: ManagedDatabaseIdentity | None = None
):
    async with (
        _publication_login(test_engine) as (role, password),
        managed_database(test_engine.url, prefix=prefix) as engine,
    ):
        database = engine.dialect.identifier_preparer.quote_identifier(
            engine.url.database
        )
        functions = ", ".join(
            f"yoao_jobs_v1.{name}({arguments})"
            for name, arguments in sorted(PUBLICATION_FUNCTIONS)
        )
        async with engine.begin() as owner:
            await owner.exec_driver_sql(
                f"REVOKE TEMP ON DATABASE {database} FROM PUBLIC"
            )
            await owner.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {database} TO {role}"
            )
            await owner.exec_driver_sql(f"GRANT USAGE ON SCHEMA yoao_jobs_v1 TO {role}")
            await owner.exec_driver_sql(
                f"GRANT EXECUTE ON FUNCTION {functions} TO {role}"
            )
        url = engine.url.set(drivername="postgresql", username=role, password=password)
        dsn = url.render_as_string(hide_password=False)
        await _verify_login(dsn, PUBLICATION_FUNCTIONS)
        monkeypatch.setattr(settings, "jobs_publication_database_url", dsn)
        monkeypatch.setattr(settings, "jobs_publication_database_url_file", "")
        yield engine


@pytest.fixture
async def input_world(test_engine, monkeypatch):
    async with (
        published_repair_database(test_engine, monkeypatch) as engine,
        input_storage_world(engine, monkeypatch) as inputs,
    ):
        yield inputs
