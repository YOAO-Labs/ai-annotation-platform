"""Private per-run media control identity for the verified fixture coordinator."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import uuid

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from fixture_suite_coordinator import FixtureSetupRefused, _resource_root, _verified_database
from worktree_env import state_path


_JOBS = (
    "aap_jobs_v1.begin_fixture_media_backfill(uuid,text,text,uuid)",
    "aap_jobs_v1.finish_fixture_media_backfill(uuid,uuid)",
    "aap_jobs_v1.prepare_fixture_media_child(uuid,uuid,uuid,uuid,jsonb)",
    "aap_jobs_v1.begin_fixture_media_child(uuid,text,uuid,jsonb,uuid)",
    "aap_jobs_v1.bind_fixture_media_child_generation(uuid,uuid,uuid,integer)",
    "aap_jobs_v1.record_fixture_pyramid_attempt(uuid,uuid,uuid,integer,uuid,uuid)",
    "aap_jobs_v1.record_fixture_video_attempt(uuid,uuid,uuid,uuid)",
    "aap_jobs_v1.finish_fixture_media_child(uuid,uuid)",
    "aap_jobs_v1.begin_fixture_media_retirement(uuid,uuid,uuid)",
    "aap_jobs_v1.finish_fixture_media_retirement(uuid,uuid,uuid)",
)
_ASSETS = (
    "aap_assets_v1.begin_fixture_native_source_admission(text,uuid,bigint,uuid,integer,uuid)",
    "aap_assets_v1.finish_fixture_native_source_admission(uuid)",
    "aap_assets_v1.begin_fixture_native_execution(text,uuid,uuid)",
    "aap_assets_v1.finish_fixture_native_execution(uuid)",
    "aap_assets_v1.begin_fixture_native_claim(uuid,integer,uuid)",
    "aap_assets_v1.finish_fixture_native_claim(uuid,uuid)",
    "aap_assets_v1.fixture_native_job_candidates(text)",
    "aap_assets_v1.begin_fixture_native_scan(uuid,uuid,bigint,uuid,integer,uuid)",
    "aap_assets_v1.finish_fixture_native_scan(uuid,uuid)",
    "aap_assets_v1.fixture_native_producer_ready(uuid)",
    "aap_assets_v1.fixture_external_image_attempt_sources_settled(uuid,uuid,uuid,uuid,bigint,uuid)",
    "aap_assets_v1.begin_fixture_native_cache_write(text,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,bigint,uuid)",
    "aap_assets_v1.advance_fixture_native_cache_write(uuid,uuid,text,text,text)",
    "aap_assets_v1.fixture_native_retirement_inventory(uuid,uuid,uuid)",
    "aap_assets_v1.prepare_fixture_native_scope(uuid,uuid,uuid,text)",
    "aap_assets_v1.finish_fixture_native_scope(uuid,uuid,uuid,text)",
    "aap_assets_v1.prepare_fixture_native_retirement(uuid,uuid,uuid,uuid)",
    "aap_assets_v1.advance_fixture_native_retirement(uuid,uuid,uuid,uuid,text)",
    "aap_assets_v1.fixture_native_job_inventory(uuid,uuid,uuid)",
    "aap_assets_v1.retire_fixture_native_job(uuid,uuid,uuid,text,uuid)",
    "aap_assets_v1.retire_fixture_native_target(uuid,text,text)",
    "aap_assets_v1.fixture_independent_delivery_envelope(uuid)",
    "aap_assets_v1.begin_fixture_independent_execution(uuid,uuid,jsonb)",
    "aap_assets_v1.begin_fixture_legacy_birth_admission(text,uuid,uuid,uuid,uuid,uuid,uuid,integer,uuid)",
    "aap_assets_v1.finish_fixture_legacy_birth_admission(uuid)",
    "aap_assets_v1.begin_fixture_screenshot_producer(uuid,uuid,uuid,uuid,uuid,integer,uuid)",
    "aap_assets_v1.fixture_screenshot_producer_target(uuid)",
    "aap_assets_v1.finish_fixture_screenshot_producer(uuid,uuid,uuid)",
    "aap_assets_v1.reuse_fixture_screenshot_producer(uuid,uuid)",
    "aap_assets_v1.finish_fixture_independent_execution(uuid,uuid)",
    "aap_assets_v1.fixture_legacy_execution_target(text,uuid,uuid)",
    "aap_assets_v1.retire_fixture_independent_publications(uuid,uuid,uuid)",
    "aap_assets_v1.bind_fixture_independent_child(uuid,uuid,uuid)",
    "aap_assets_v1.bind_fixture_independent_pyramid(uuid,uuid,uuid,integer)",
    "aap_assets_v1.record_fixture_independent_video_attempt(uuid,uuid,uuid,uuid)",
    "aap_assets_v1.record_fixture_independent_pyramid_attempt(uuid,uuid,uuid,integer,uuid,uuid)",
    "aap_assets_v1.begin_fixture_media_version(text,uuid,uuid,text,uuid,text,text,text,text,bigint,uuid)",
    "aap_assets_v1.record_fixture_media_version(text,uuid,uuid,uuid,text,text)",
    "aap_assets_v1.verify_fixture_media_version(text,uuid,uuid,uuid,text)",
    "aap_assets_v1.fixture_media_retirement_inventory(uuid,uuid,uuid)",
    "aap_assets_v1.fixture_media_output_inventory(uuid,uuid,uuid,text,uuid)",
    "aap_assets_v1.acquire_fixture_media_retirement_permit(uuid,uuid,uuid,text,uuid,uuid)",
    "aap_assets_v1.prepare_fixture_media_version_retirement(uuid,uuid,uuid,uuid,uuid)",
    "aap_assets_v1.advance_fixture_media_version_retirement(uuid,uuid,uuid,uuid,uuid,text)",
    "aap_assets_v1.fixture_media_output_retirement_done(uuid,uuid,uuid,text,uuid)",
    "aap_assets_v1.transition_fixture_media_output(uuid,uuid,uuid,text,uuid,text,text)",
    "aap_assets_v1.fixture_media_retirement_complete(uuid,uuid,uuid)",
)


def _private_file(path: Path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise FixtureSetupRefused("fixture_media_secret_path_invalid")
    parent = path.parent
    info = parent.stat()
    if (
        parent.resolve(strict=True) != parent
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise FixtureSetupRefused("fixture_media_secret_directory_invalid")
    return path


async def receipt_path(run_id: uuid.UUID) -> Path:
    """Locate a per-run secret only under the verified workspace state root."""
    if not isinstance(run_id, uuid.UUID):
        raise FixtureSetupRefused("fixture_media_run_invalid")
    _, resources, _ = await _verified_database()
    directory = state_path(
        _resource_root(resources), resources["mode"], "fixture-media-control"
    )
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    return _private_file(directory / f"{run_id.hex}.json")


def _read_receipt(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise FixtureSetupRefused("fixture_media_secret_invalid")
        content = stream.read(8193)
    if len(content) > 8192:
        raise FixtureSetupRefused("fixture_media_secret_invalid")
    try:
        return json.loads(content)
    except (ValueError, UnicodeError):
        raise FixtureSetupRefused("fixture_media_secret_invalid") from None


def _reserve(path, payload):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def _role(connection, name):
    return (
        await connection.execute(
            text(
                "SELECT r.oid,r.rolname,r.rolcanlogin,r.rolinherit,r.rolsuper,r.rolcreatedb,"
                "r.rolcreaterole,r.rolreplication,r.rolbypassrls,"
                "pg_catalog.shobj_description(r.oid,'pg_authid') AS comment,"
                "EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid) AS member "
                "FROM pg_catalog.pg_roles r WHERE r.rolname=:name"
            ),
            {"name": name},
        )
    ).mappings().one_or_none()


def _validate_role(role, marker):
    if (
        role is None or not role["rolcanlogin"] or role["rolinherit"] or role["rolsuper"]
        or role["rolcreatedb"] or role["rolcreaterole"] or role["rolreplication"]
        or role["rolbypassrls"] or role["member"] or role["comment"] != marker
    ):
        raise FixtureSetupRefused("fixture_media_role_changed")

async def _validate_acl(connection, role_oid, signatures=None, allowed_schemas=None):
    if signatures is None:
        signatures = _JOBS + _ASSETS
    if allowed_schemas is None:
        allowed_schemas = {"aap_jobs_v1", "aap_assets_v1"}
    expected = set(
        await connection.scalars(
            text(
                "SELECT pg_catalog.to_regprocedure(entries.signature)::oid "
                "FROM pg_catalog.unnest(CAST(:signatures AS text[])) AS entries(signature)"
            ),
            {"signatures": list(signatures)},
        )
    )
    actual = set(
        await connection.scalars(
            text(
                "SELECT procedure.oid FROM pg_catalog.pg_proc AS procedure "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS acl "
                "WHERE acl.grantee=:role AND acl.privilege_type='EXECUTE'"
            ),
            {"role": role_oid},
        )
    )
    schemas = set(
        (await connection.execute(
            text(
                "SELECT namespace.nspname,acl.privilege_type FROM pg_catalog.pg_namespace AS namespace "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(namespace.nspacl) AS acl "
                "WHERE acl.grantee=:role"
            ),
            {"role": role_oid},
        )).all()
    )
    if None in expected or expected != actual or schemas != {
        (schema, "USAGE") for schema in allowed_schemas
    }:
        raise FixtureSetupRefused("fixture_media_role_privileges_changed")



async def provision(admin_url: URL, run_id: uuid.UUID, secret_path: Path) -> URL:
    """Create or verify one exact scoped LOGIN; never replace a missing owner."""
    if not isinstance(run_id, uuid.UUID):
        raise FixtureSetupRefused("fixture_media_run_invalid")
    owner_url, resources, identity = await _verified_database()
    admin_url = make_url(admin_url)
    if (
        admin_url.drivername != "postgresql+asyncpg" or admin_url.host != owner_url.host
        or (admin_url.port or 5432) != (owner_url.port or 5432)
        or admin_url.database != owner_url.database or admin_url.query
    ):
        raise FixtureSetupRefused("fixture_media_database_changed")
    path = _private_file(secret_path)
    role_name = f"aap_fixture_media_{run_id.hex}"
    marker = f"{resources['owner']}:fixture-media:{run_id}"
    fresh = False
    try:
        receipt = _read_receipt(path)
    except FileNotFoundError:
        receipt = {
            "schema": 1, "run_id": str(run_id), "database_oid": identity["database_oid"],
            "role": role_name, "marker": marker, "password": secrets.token_urlsafe(48),
        }
        try:
            _reserve(path, receipt)
        except FileExistsError:
            raise FixtureSetupRefused("fixture_media_secret_race") from None
        fresh = True
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema", "run_id", "database_oid", "role", "marker", "password"}
        or receipt["schema"] != 1 or receipt["run_id"] != str(run_id)
        or receipt["database_oid"] != identity["database_oid"] or receipt["role"] != role_name
        or receipt["marker"] != marker or not isinstance(receipt["password"], str)
        or len(receipt["password"]) < 40
    ):
        raise FixtureSetupRefused("fixture_media_secret_changed")
    engine = create_async_engine(admin_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            owner = await connection.scalar(text("SELECT pg_catalog.shobj_description(oid,'pg_database') FROM pg_catalog.pg_database WHERE datname=current_database()"))
            if owner != resources["owner"]:
                raise FixtureSetupRefused("fixture_media_database_changed")
            await connection.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:name,0))"), {"name": role_name})
            role = await _role(connection, role_name)
            if role is None:
                if not fresh:
                    raise FixtureSetupRefused("fixture_media_role_missing")
                await connection.execute(text("SELECT pg_catalog.set_config('aap.media_role',:role,true), pg_catalog.set_config('aap.media_password',:password,true), pg_catalog.set_config('aap.media_marker',:marker,true)"), {"role": role_name, "password": receipt["password"], "marker": marker})
                await connection.exec_driver_sql("""DO $role$ BEGIN
                    EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', current_setting('aap.media_role'), current_setting('aap.media_password'));
                    EXECUTE format('COMMENT ON ROLE %I IS %L', current_setting('aap.media_role'), current_setting('aap.media_marker'));
                END; $role$;""")
                quote = engine.dialect.identifier_preparer.quote_identifier
                role_sql, db_sql = quote(role_name), quote(resources["database"])
                await connection.exec_driver_sql(f"GRANT CONNECT ON DATABASE {db_sql} TO {role_sql}")
                await connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA aap_jobs_v1,aap_assets_v1 TO {role_sql}")
                await connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {', '.join(_JOBS + _ASSETS)} TO {role_sql}")
                role = await _role(connection, role_name)
            _validate_role(role, marker)
            await _validate_acl(connection, role["oid"])
    finally:
        await engine.dispose()
    owner_engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(text("SELECT aap_jobs_v1.register_fixture_media_executor(:run,CAST(:role AS name))"), {"run": run_id, "role": role_name})
            principal = await connection.scalar(
                text("SELECT aap_jobs_v1.fixture_media_executor_identity(:run)"), {"run": run_id}
            )
            if principal != {"oid": role["oid"], "name": role_name, "state": "active"}:
                raise FixtureSetupRefused("fixture_media_role_changed")
    finally:
        await owner_engine.dispose()
    control_url = owner_url.set(username=role_name, password=receipt["password"])
    control_engine = create_async_engine(control_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with control_engine.connect() as connection:
            observed = (await connection.execute(text("SELECT current_user,current_database()"))).one()
            if observed != (role_name, resources["database"]):
                raise FixtureSetupRefused("fixture_media_role_changed")
    finally:
        await control_engine.dispose()
    return control_url


async def register_native_target(run_id: uuid.UUID, owner, database_identity: dict) -> dict:
    """Register a physically verified output target while holding its store lock."""
    from fixture_store import verified_native_output, verified_native_output_birth
    from fixture_suite_coordinator import _settled_thread

    if not isinstance(run_id, uuid.UUID):
        raise FixtureSetupRefused("fixture_media_run_invalid")
    owner_url, _, observed_identity = await _verified_database()
    if database_identity != observed_identity:
        raise FixtureSetupRefused("fixture_media_database_changed")
    output = await _settled_thread(
        verified_native_output, owner, database_identity, run_id
    )
    if (
        not isinstance(output, dict)
        or set(output) != {
            "run_id", "endpoint", "bucket", "storage_identity", "receipt_sha256",
            "writer_file", "reader_file", "cleanup_file",
        }
        or output["run_id"] != str(run_id)
        or not isinstance(output["endpoint"], str)
        or output["endpoint"].endswith("/")
        or output["bucket"] != f"fixture-media-{run_id.hex}"
        or output["storage_identity"] != hashlib.sha256(
            json.dumps([output["endpoint"], output["bucket"]]).encode()
        ).hexdigest()
        or not isinstance(output["receipt_sha256"], str)
        or len(output["receipt_sha256"]) != 64
        or any(
            not isinstance(output[name], (str, Path)) or not Path(output[name]).is_absolute()
            for name in ("writer_file", "reader_file", "cleanup_file")
        )
    ):
        raise FixtureSetupRefused("fixture_native_cache_target_changed")
    birth = await _settled_thread(verified_native_output_birth, owner, database_identity, run_id)
    if (
        not isinstance(birth, str)
        or hashlib.sha256(birth.encode("utf-8")).hexdigest() != output["receipt_sha256"]
    ):
        raise FixtureSetupRefused("fixture_native_birth_receipt_changed")
    from app.core.crypto import decrypt_secret, encrypt_secret
    from app.db.models.dataset_asset import StorageLocation
    from app.db.models.storage_connection import StorageConnection
    from app.services import storage_access
    from app.services.storage_location import connector_location_id, register_connection
    from sqlalchemy.ext.asyncio import async_sessionmaker

    reader = _read_receipt(_private_file(Path(output["reader_file"])))
    if (
        not isinstance(reader, dict)
        or set(reader) != {
            "schema", "run_id", "endpoint", "bucket", "storage_identity",
            "receipt_sha256", "access_key", "secret_key",
        }
        or reader["schema"] != "aap-fixture-native-output-target/v1"
        or any(reader[name] != output[name] for name in (
            "run_id", "endpoint", "bucket", "storage_identity", "receipt_sha256",
        ))
        or any(not isinstance(reader[name], str) or not reader[name] for name in ("access_key", "secret_key"))
    ):
        raise FixtureSetupRefused("fixture_native_cache_target_changed")
    account = {name: reader[name] for name in ("access_key", "secret_key")}
    connection_id = uuid.uuid5(uuid.NAMESPACE_URL, "fixture-native-media:" + str(run_id))
    location_id = connector_location_id(connection_id)
    config = {
        "endpoint": output["endpoint"], "bucket": output["bucket"],
        "base_prefix": "", "use_ssl": False,
    }
    engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db, db.begin():
            await db.execute(
                text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key,0))"),
                {"key": "aap-fixture-native-location:" + str(run_id)},
            )
            binding = await db.scalar(
                text("SELECT aap_assets_v1.fixture_native_cache_target_binding(:run)"),
                {"run": run_id},
            )
            connection = await db.get(StorageConnection, connection_id, with_for_update=True)
            if binding is None:
                if connection is not None or await db.get(StorageLocation, location_id) is not None:
                    raise FixtureSetupRefused("fixture_native_location_unowned")
                connection = StorageConnection(
                    id=connection_id, name="fixture-native-media-" + run_id.hex,
                    revision=1, kind="s3", config=config, secret_enc=encrypt_secret(account),
                    scope="global", project_id=None,
                )
                db.add(connection)
                await db.flush()
                location = await register_connection(db, connection)
            else:
                location = await db.get(StorageLocation, location_id, with_for_update=True)
            if (
                connection is None or location is None or connection.id != connection_id
                or connection.name != "fixture-native-media-" + run_id.hex
                or connection.kind != "s3" or connection.scope != "global"
                or connection.project_id is not None or connection.revision != 1
                or connection.config != config or decrypt_secret(connection.secret_enc) != account
                or location.id != location_id or location.connection_id != connection_id
                or location.kind != "s3" or location.config != {}
                or location.config_revision != 1 or location.enabled is not True
            ):
                raise FixtureSetupRefused("fixture_native_location_changed")
            await db.execute(
                text(
                    "SELECT aap_assets_v1.register_fixture_native_cache_target("
                    ":run,:endpoint,:bucket,:identity,:receipt,:birth,:connection,:location,:generation)"
                ),
                {
                    "run": run_id, "endpoint": output["endpoint"], "bucket": output["bucket"],
                    "identity": output["storage_identity"], "receipt": output["receipt_sha256"],
                    "birth": birth,
                    "connection": connection_id, "location": location_id, "generation": 1,
                },
            )
        async with sessions() as db:
            await storage_access.ensure(db, location_id)
            await db.commit()
    finally:
        await engine.dispose()
    return output


async def native_target(run_id: uuid.UUID) -> dict:
    """Verify and return a run's non-secret output target record under its store lock."""
    import fcntl

    from fixture_store import verified_native_output
    from fixture_suite_coordinator import _settled_thread
    from verify_storage_candidates import Owner

    if not isinstance(run_id, uuid.UUID):
        raise FixtureSetupRefused("fixture_media_run_invalid")
    _, resources, identity = await _verified_database()
    root = _resource_root(resources)
    manifest = state_path(root, resources["mode"], "fixture-store-owner.json")
    lock_path = state_path(root, resources["mode"], "fixture-store.lock")

    def observe():
        descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise FixtureSetupRefused("fixture_lock_invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                if not manifest.exists() or manifest.is_symlink():
                    raise FixtureSetupRefused("fixture_store_not_prepared")
                return verified_native_output(Owner(manifest), identity, run_id)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    return await _settled_thread(observe)


async def load_control(run_id: uuid.UUID, secret_path: Path) -> URL:
    """Open an existing exact LOGIN, including after its run was sealed."""
    if not isinstance(run_id, uuid.UUID):
        raise FixtureSetupRefused("fixture_media_run_invalid")
    owner_url, resources, identity = await _verified_database()
    receipt = _read_receipt(_private_file(secret_path))
    role_name = f"aap_fixture_media_{run_id.hex}"
    marker = f"{resources['owner']}:fixture-media:{run_id}"
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema", "run_id", "database_oid", "role", "marker", "password"}
        or receipt["schema"] != 1
        or receipt["run_id"] != str(run_id)
        or receipt["database_oid"] != identity["database_oid"]
        or receipt["role"] != role_name
        or receipt["marker"] != marker
        or not isinstance(receipt["password"], str)
        or len(receipt["password"]) < 40
    ):
        raise FixtureSetupRefused("fixture_media_secret_changed")
    engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            role = await _role(connection, role_name)
            _validate_role(role, marker)
            await _validate_acl(connection, role["oid"])
            principal = await connection.scalar(
                text("SELECT aap_jobs_v1.fixture_media_executor_identity(:run)"),
                {"run": run_id},
            )
            if principal != {"oid": role["oid"], "name": role_name, "state": "active"}:
                raise FixtureSetupRefused("fixture_media_role_changed")
    finally:
        await engine.dispose()
    control_url = owner_url.set(username=role_name, password=receipt["password"])
    control_engine = create_async_engine(control_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with control_engine.connect() as connection:
            observed = (await connection.execute(text("SELECT current_user,current_database()"))).one()
            if observed != (role_name, resources["database"]):
                raise FixtureSetupRefused("fixture_media_role_changed")
    finally:
        await control_engine.dispose()
    return control_url


def _verify_worker_dsn(path: Path, expected: str) -> Path:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or stream.read(8193).strip() != expected
        ):
            raise FixtureSetupRefused("fixture_media_worker_secret_changed")
    return path


async def verified_worker_dsn_path(run_id: uuid.UUID, secret_path: Path) -> Path:
    """Verify the already-provisioned restricted selector without creating it."""
    control = await load_control(run_id, secret_path)
    path = _private_file(Path(secret_path).with_suffix(".dsn"))
    return _verify_worker_dsn(path, control.render_as_string(hide_password=False))


async def worker_dsn_path(run_id: uuid.UUID, secret_path: Path) -> Path:
    """Publish one immutable worker DSN beside its coordinator receipt."""
    control = await load_control(run_id, secret_path)
    path = _private_file(Path(secret_path).with_suffix(".dsn"))
    expected = control.render_as_string(hide_password=False)
    try:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
    except FileExistsError:
        return _verify_worker_dsn(path, expected)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(expected)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return path


async def retire(admin_url: URL, run_id: uuid.UUID, secret_path: Path) -> None:
    """Drop only the exact LOGIN after the native run and outputs have settled."""
    if not isinstance(run_id, uuid.UUID):
        raise FixtureSetupRefused("fixture_media_run_invalid")
    owner_url, resources, identity = await _verified_database()
    admin_url = make_url(admin_url)
    if (
        admin_url.drivername != "postgresql+asyncpg" or admin_url.host != owner_url.host
        or (admin_url.port or 5432) != (owner_url.port or 5432)
        or admin_url.database != owner_url.database or admin_url.query
    ):
        raise FixtureSetupRefused("fixture_media_database_changed")
    receipt = _read_receipt(_private_file(secret_path))
    role_name = f"aap_fixture_media_{run_id.hex}"
    marker = f"{resources['owner']}:fixture-media:{run_id}"
    if (
        not isinstance(receipt, dict) or receipt.get("schema") != 1
        or receipt.get("run_id") != str(run_id) or receipt.get("database_oid") != identity["database_oid"]
        or receipt.get("role") != role_name or receipt.get("marker") != marker
    ):
        raise FixtureSetupRefused("fixture_media_secret_changed")
    owner_engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with owner_engine.begin() as connection:
            settled = await connection.scalar(
                text("SELECT aap_jobs_v1.retire_fixture_media_executor(:run)"),
                {"run": run_id},
            )
            if settled is not True:
                raise FixtureSetupRefused("fixture_media_executor_unsettled")
            principal = await connection.scalar(
                text("SELECT aap_jobs_v1.fixture_media_executor_identity(:run)"), {"run": run_id}
            )
            if (
                not isinstance(principal, dict) or principal.get("name") != role_name
                or principal.get("state") != "retired" or type(principal.get("oid")) is not int
            ):
                raise FixtureSetupRefused("fixture_media_role_changed")
    finally:
        await owner_engine.dispose()
    engine = create_async_engine(admin_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            owner = await connection.scalar(
                text("SELECT pg_catalog.shobj_description(oid,'pg_database') "
                     "FROM pg_catalog.pg_database WHERE datname=current_database()")
            )
            if owner != resources["owner"]:
                raise FixtureSetupRefused("fixture_media_database_changed")
            await connection.execute(
                text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:name,0))"),
                {"name": role_name},
            )
            role = await _role(connection, role_name)
            if role is None:
                return
            _validate_role(role, marker)
            if role["oid"] != principal["oid"]:
                raise FixtureSetupRefused("fixture_media_role_changed")
            await _validate_acl(connection, role["oid"])
            if await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity WHERE usesysid=:role)"),
                {"role": role["oid"]},
            ):
                raise FixtureSetupRefused("fixture_media_executor_unsettled")
            quote = engine.dialect.identifier_preparer.quote_identifier
            role_sql, db_sql = quote(role_name), quote(resources["database"])
            await connection.exec_driver_sql(
                f"REVOKE EXECUTE ON FUNCTION {', '.join(_JOBS + _ASSETS)} FROM {role_sql}"
            )
            await connection.exec_driver_sql(
                f"REVOKE USAGE ON SCHEMA aap_jobs_v1,aap_assets_v1 FROM {role_sql}"
            )
            await connection.exec_driver_sql(f"REVOKE CONNECT ON DATABASE {db_sql} FROM {role_sql}")
            await connection.exec_driver_sql(f"DROP ROLE {role_sql}")
    finally:
        await engine.dispose()
