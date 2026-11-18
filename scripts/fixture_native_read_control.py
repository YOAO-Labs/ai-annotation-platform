"""Provision exact private native output reader and suite resolver LOGINs."""

from __future__ import annotations

from pathlib import Path
import secrets
import uuid

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from fixture_media_control import (
    _private_file,
    _read_receipt,
    _reserve,
    _role,
    _validate_acl,
    _validate_role,
    receipt_path,
)
from fixture_suite_coordinator import FixtureSetupRefused, _verified_database

_READER_FUNCTIONS = (
    "yoao_assets_v1.begin_fixture_native_output_read(uuid,uuid,uuid)",
    "yoao_assets_v1.finish_fixture_native_output_read(uuid,uuid)",
    "yoao_assets_v1.fixture_native_reader_ready(uuid)",
)
_RESOLVER_FUNCTIONS = ("yoao_assets_v1.resolve_fixture_native_cache(text)",)


def _spec(kind, scope):
    if kind == "reader":
        return (
            f"aap_fixture_media_read_{scope.hex}",
            "run_id",
            "aap-fixture-native-reader/v1",
            _READER_FUNCTIONS,
            "register_fixture_native_reader",
            "fixture_native_reader_identity",
            "retire_fixture_native_reader",
        )
    if kind == "resolver":
        return (
            f"aap_fixture_media_lookup_{scope.hex}",
            "suite_id",
            "aap-fixture-native-resolver/v1",
            _RESOLVER_FUNCTIONS,
            "register_fixture_native_resolver",
            "fixture_native_resolver_identity",
            "retire_fixture_native_resolver",
        )
    raise FixtureSetupRefused("fixture_native_role_invalid")


async def credential_path(kind: str, scope: uuid.UUID) -> Path:
    if not isinstance(scope, uuid.UUID):
        raise FixtureSetupRefused("fixture_media_run_invalid")
    _spec(kind, scope)
    base = await receipt_path(scope)
    return _private_file(base.with_name(f"{scope.hex}.{kind}.json"))


def _validate_database_url(admin_url, owner_url):
    value = make_url(admin_url)
    if (
        value.drivername != "postgresql+asyncpg"
        or value.host != owner_url.host
        or (value.port or 5432) != (owner_url.port or 5432)
        or value.database != owner_url.database
        or value.query
    ):
        raise FixtureSetupRefused("fixture_media_database_changed")
    return value


def _validate_credential(
    value, *, name, field, schema, scope, admin_url, identity, resources
):
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema", field, "dsn", "database_oid", "database_owner_marker"}
        or value["schema"] != schema
        or value[field] != str(scope)
        or value["database_oid"] != identity["database_oid"]
        or value["database_owner_marker"] != resources["owner"]
        or not isinstance(value["dsn"], str)
    ):
        raise FixtureSetupRefused("fixture_native_role_receipt_changed")
    try:
        url = make_url(value["dsn"])
    except (ValueError, TypeError):
        raise FixtureSetupRefused("fixture_native_role_receipt_changed") from None
    if (
        url.drivername != "postgresql+asyncpg"
        or url.username != name
        or not url.password
        or url.host != admin_url.host
        or (url.port or 5432) != (admin_url.port or 5432)
        or url.database != admin_url.database
        or url.query
    ):
        raise FixtureSetupRefused("fixture_native_role_receipt_changed")
    return url


async def provision(
    admin_url: URL, kind: str, scope: uuid.UUID, *, proxy_origin: str | None = None
) -> Path:
    """Create only the exact per-run reader or per-suite lookup LOGIN and receipt."""
    if not isinstance(scope, uuid.UUID):
        raise FixtureSetupRefused("fixture_media_run_invalid")
    name, field, schema, functions, register, identity_function, _retire = _spec(
        kind, scope
    )
    if (kind == "reader") != (isinstance(proxy_origin, str) and bool(proxy_origin)):
        raise FixtureSetupRefused("fixture_native_proxy_origin_invalid")
    owner_url, resources, identity = await _verified_database()
    admin_url = _validate_database_url(admin_url, owner_url)
    path = await credential_path(kind, scope)
    marker = f"{resources['owner']}:fixture-native-{kind}:{scope}"
    fresh = False
    try:
        receipt = _read_receipt(path)
    except FileNotFoundError:
        credential = admin_url.set(username=name, password=secrets.token_urlsafe(48))
        receipt = {
            "schema": schema,
            field: str(scope),
            "dsn": credential.render_as_string(hide_password=False),
            "database_oid": identity["database_oid"],
            "database_owner_marker": resources["owner"],
        }
        try:
            _reserve(path, receipt)
        except FileExistsError:
            raise FixtureSetupRefused("fixture_native_role_receipt_race") from None
        fresh = True
    credential = _validate_credential(
        receipt,
        name=name,
        field=field,
        schema=schema,
        scope=scope,
        admin_url=admin_url,
        identity=identity,
        resources=resources,
    )
    engine = create_async_engine(admin_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            owner = await connection.scalar(
                text(
                    "SELECT pg_catalog.shobj_description(oid,'pg_database') "
                    "FROM pg_catalog.pg_database WHERE datname=current_database()"
                )
            )
            if owner != resources["owner"]:
                raise FixtureSetupRefused("fixture_media_database_changed")
            await connection.execute(
                text(
                    "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:name,0))"
                ),
                {"name": name},
            )
            role = await _role(connection, name)
            if role is None:
                if not fresh:
                    raise FixtureSetupRefused("fixture_native_role_missing")
                await connection.execute(
                    text(
                        "SELECT pg_catalog.set_config('aap.native_role',:name,true), "
                        "pg_catalog.set_config('aap.native_password',:password,true), "
                        "pg_catalog.set_config('aap.native_marker',:marker,true)"
                    ),
                    {"name": name, "password": credential.password, "marker": marker},
                )
                await connection.exec_driver_sql("""DO $role$ BEGIN
                    EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', current_setting('aap.native_role'), current_setting('aap.native_password'));
                    EXECUTE format('COMMENT ON ROLE %I IS %L', current_setting('aap.native_role'), current_setting('aap.native_marker'));
                END; $role$;""")
                quote = engine.dialect.identifier_preparer.quote_identifier
                await connection.exec_driver_sql(
                    f"GRANT CONNECT ON DATABASE {quote(resources['database'])} TO {quote(name)}"
                )
                await connection.exec_driver_sql(
                    f"GRANT USAGE ON SCHEMA yoao_assets_v1 TO {quote(name)}"
                )
                await connection.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION {', '.join(functions)} TO {quote(name)}"
                )
                role = await _role(connection, name)
            elif fresh:
                raise FixtureSetupRefused("fixture_native_role_unaccounted")
            _validate_role(role, marker)
            await _validate_acl(connection, role["oid"], functions, {"yoao_assets_v1"})
    finally:
        await engine.dispose()
    narrow_engine = create_async_engine(
        credential, poolclass=NullPool, hide_parameters=True
    )
    try:
        async with narrow_engine.connect() as connection:
            observed = (
                await connection.execute(
                    text(
                        "SELECT current_user,oid,pg_catalog.shobj_description(oid,'pg_database') "
                        "FROM pg_catalog.pg_database WHERE datname=current_database()"
                    )
                )
            ).one()
            if observed != (name, identity["database_oid"], resources["owner"]):
                raise FixtureSetupRefused("fixture_native_role_identity_changed")
    finally:
        await narrow_engine.dispose()
    owner_engine = create_async_engine(
        owner_url, poolclass=NullPool, hide_parameters=True
    )
    try:
        async with owner_engine.begin() as connection:
            arguments = (
                ":scope,CAST(:role AS name),:origin"
                if kind == "reader"
                else ":scope,CAST(:role AS name)"
            )
            accepted = await connection.scalar(
                text(f"SELECT yoao_assets_v1.{register}({arguments})"),
                {
                    "scope": scope,
                    "role": name,
                    **({"origin": proxy_origin} if kind == "reader" else {}),
                },
            )
            principal = await connection.scalar(
                text(f"SELECT yoao_assets_v1.{identity_function}(:scope)"),
                {"scope": scope},
            )
            if (
                accepted is not True
                or not isinstance(principal, dict)
                or principal.get("oid") != role["oid"]
                or principal.get("name") != name
                or principal.get("state") != "active"
                or kind == "reader"
                and principal.get("proxy_origin") != proxy_origin
            ):
                raise FixtureSetupRefused("fixture_native_role_identity_changed")
    finally:
        await owner_engine.dispose()
    return path


async def retire(admin_url: URL, kind: str, scope: uuid.UUID) -> None:
    """Drop only a settled reader/resolver LOGIN; keep its native tombstone and receipt."""
    if not isinstance(scope, uuid.UUID):
        raise FixtureSetupRefused("fixture_media_run_invalid")
    name, field, schema, functions, _register, identity_function, retire_function = (
        _spec(kind, scope)
    )
    owner_url, resources, identity = await _verified_database()
    admin_url = _validate_database_url(admin_url, owner_url)
    receipt = _read_receipt(await credential_path(kind, scope))
    _validate_credential(
        receipt,
        name=name,
        field=field,
        schema=schema,
        scope=scope,
        admin_url=admin_url,
        identity=identity,
        resources=resources,
    )
    marker = f"{resources['owner']}:fixture-native-{kind}:{scope}"
    owner_engine = create_async_engine(
        owner_url, poolclass=NullPool, hide_parameters=True
    )
    try:
        async with owner_engine.begin() as connection:
            settled = await connection.scalar(
                text(f"SELECT yoao_assets_v1.{retire_function}(:scope)"),
                {"scope": scope},
            )
            principal = await connection.scalar(
                text(f"SELECT yoao_assets_v1.{identity_function}(:scope)"),
                {"scope": scope},
            )
            if (
                settled is not True
                or not isinstance(principal, dict)
                or principal.get("state") != "retired"
                or principal.get("name") != name
                or type(principal.get("oid")) is not int
            ):
                raise FixtureSetupRefused("fixture_native_role_unsettled")
    finally:
        await owner_engine.dispose()
    engine = create_async_engine(admin_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:name,0))"
                ),
                {"name": name},
            )
            role = await _role(connection, name)
            if role is None:
                return
            _validate_role(role, marker)
            if role["oid"] != principal["oid"]:
                raise FixtureSetupRefused("fixture_native_role_identity_changed")
            await _validate_acl(connection, role["oid"], functions, {"yoao_assets_v1"})
            if await connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity WHERE usesysid=:role)"
                ),
                {"role": role["oid"]},
            ):
                raise FixtureSetupRefused("fixture_native_role_unsettled")
            quote = engine.dialect.identifier_preparer.quote_identifier
            await connection.exec_driver_sql(
                f"REVOKE EXECUTE ON FUNCTION {', '.join(functions)} FROM {quote(name)}"
            )
            await connection.exec_driver_sql(
                f"REVOKE USAGE ON SCHEMA yoao_assets_v1 FROM {quote(name)}"
            )
            await connection.exec_driver_sql(
                f"REVOKE CONNECT ON DATABASE {quote(resources['database'])} FROM {quote(name)}"
            )
            await connection.exec_driver_sql(f"DROP ROLE {quote(name)}")
    finally:
        await engine.dispose()
