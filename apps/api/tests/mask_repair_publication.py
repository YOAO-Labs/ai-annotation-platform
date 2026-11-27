"""A committed repair database with a separate, function-only Jobs publisher."""

from contextlib import asynccontextmanager
import uuid

import pytest
from yoao_jobs.publication_postgres import PUBLICATION_FUNCTIONS

from app.config import settings
from app.workers.jobs_dispatch import _verify_login
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
async def bind_publication_login(engine, monkeypatch):
    async with _publication_login(engine) as (role, password):
        database = engine.dialect.identifier_preparer.quote_identifier(
            engine.url.database
        )
        functions = ", ".join(
            f"yoao_jobs_v1.{name}({arguments})"
            for name, arguments in sorted(PUBLICATION_FUNCTIONS)
        )
        async with engine.begin() as owner:
            had_public_temp = bool(
                (
                    await owner.exec_driver_sql(
                        "SELECT has_database_privilege('public', current_database(), 'TEMP')"
                    )
                ).scalar()
            )
            if had_public_temp:
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
        try:
            yield dsn
        finally:
            async with engine.begin() as owner:
                await owner.exec_driver_sql(
                    f"REVOKE EXECUTE ON FUNCTION {functions} FROM {role}"
                )
                await owner.exec_driver_sql(
                    f"REVOKE USAGE ON SCHEMA yoao_jobs_v1 FROM {role}"
                )
                await owner.exec_driver_sql(
                    f"REVOKE CONNECT ON DATABASE {database} FROM {role}"
                )
                if had_public_temp:
                    await owner.exec_driver_sql(
                        f"GRANT TEMP ON DATABASE {database} TO PUBLIC"
                    )


@asynccontextmanager
async def published_repair_database(test_engine, monkeypatch, managed_database):
    async with managed_database() as engine:
        async with bind_publication_login(engine, monkeypatch):
            yield engine


@pytest.fixture
async def input_world(test_engine, monkeypatch, managed_database):
    async with (
        published_repair_database(test_engine, monkeypatch, managed_database) as engine,
        input_storage_world(engine, monkeypatch) as inputs,
    ):
        yield inputs
