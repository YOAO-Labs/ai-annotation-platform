"""Private owner-side bootstrap for run-scoped fixture originals.

Only a verified disposable database and an owned fresh physical store may be
published together. This module is not an HTTP API or an application credential.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timezone
import asyncio
import fcntl
import hashlib
import json
import re
import os
from pathlib import Path
import stat
import sys
import uuid

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from worktree_env import (
    FIXTURE_ORIGINAL_ENV_KEYS,
    WorktreeError,
    load_identity,
    read_state,
    state_path,
    topology,
)


ROOT = Path(__file__).resolve().parents[1]
# ROOT is the code checkout; disposable resource state has a separate owner root.


class FixtureSetupRefused(WorktreeError):
    """A private fixture authority or physical identity could not be proven."""


def _refuse_unless(condition, message):
    if not condition:
        raise FixtureSetupRefused(message)


def _canonical_resource_root(value):
    _refuse_unless(
        isinstance(value, str) and bool(value), "fixture_resource_root_missing"
    )
    root = Path(value)
    try:
        canonical = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise FixtureSetupRefused("fixture_resource_root_invalid") from None
    _refuse_unless(
        root.is_absolute() and root.is_dir() and str(root) == str(canonical),
        "fixture_resource_root_invalid",
    )
    return canonical


def _resource_root(resources):
    """Validate a launcher-owned topology without conflating it with code checkout."""
    _refuse_unless(isinstance(resources, dict), "fixture_topology_invalid")
    root = _canonical_resource_root(resources.get("root"))
    expected = topology(root, load_identity(root), resources.get("mode"))
    _refuse_unless(
        set(resources) == set(expected)
        and all(
            resources[key] == value
            for key, value in expected.items()
            if key != "buckets"
        )
        and isinstance(resources["buckets"], dict)
        and set(resources["buckets"])
        in (
            set(expected["buckets"]),
            set(expected["buckets"]) - {"MINIO_INPUTS_BUCKET"},
            set(expected["buckets"]) - {"MINIO_INPUTS_BUCKET", "MINIO_TRAINING_BUCKET"},
        )
        and all(
            expected["buckets"].get(key) == value
            for key, value in resources["buckets"].items()
        ),
        "fixture_topology_invalid",
    )
    manifest_path = state_path(root, resources["mode"], "resources.json")
    if manifest_path.exists():
        _refuse_unless(
            read_state(manifest_path).get("resources") == resources,
            "fixture_manifest_mismatch",
        )
    elif state_path(root, resources["mode"], "fixture-store-owner.json").exists():
        raise FixtureSetupRefused("fixture_manifest_missing")
    return root


def _verified_resources(environment):
    mode = environment.get("AAP_WORKTREE_MODE")
    _refuse_unless(mode in {"test", "e2e"}, "fixture_owned_topology_required")
    root = _canonical_resource_root(environment.get("AAP_WORKTREE_RESOURCE_ROOT"))
    resources = topology(root, load_identity(root), mode)
    manifest = read_state(state_path(root, mode, "resources.json"))
    _refuse_unless(manifest.get("resources") == resources, "fixture_manifest_mismatch")
    return root, resources, manifest


def acquire_store_destruction_lock(resources):
    """Refuse destruction while any fixture owner still holds its store lock."""
    root = _resource_root(resources)
    manifest = state_path(root, resources["mode"], "fixture-store-owner.json")
    if not manifest.exists():
        return None
    path = state_path(root, resources["mode"], "fixture-store.lock")
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise FixtureSetupRefused("fixture_lock_missing") from None
    try:
        info = os.fstat(descriptor)
        _refuse_unless(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600,
            "fixture_lock_invalid",
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FixtureSetupRefused("fixture_store_busy") from None
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


async def _settled(task):
    """Do not release the owner lock while admitted physical work is still running."""
    from app.services.storage_access import finish

    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as interrupted:
        try:
            await finish(task)
        except BaseException as failure:
            raise BaseExceptionGroup(
                "Fixture operation failed while cancellation was pending",
                [interrupted, failure],
            ) from None
        raise


async def _settled_thread(function, *args, **kwargs):
    return await _settled(
        asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    )


async def _provisioning_child(environment, payload):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "--complete-provisioning",
        cwd=str(ROOT / "apps/api"),
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await child.communicate(json.dumps(payload).encode() + b"\n")
    try:
        response = json.loads(stdout)
    except (ValueError, UnicodeError):
        raise FixtureSetupRefused("fixture_provisioning_invalid_response") from None
    if child.returncode:
        failure = (
            response.get("error", "provisioning_failed")
            if isinstance(response, dict)
            else "provisioning_failed"
        )
        if (
            not isinstance(failure, str)
            or re.fullmatch(r"[A-Za-z0-9_:]{1,120}", failure) is None
        ):
            failure = "provisioning_failed"
        raise FixtureSetupRefused(f"fixture_provisioning_failed:{failure}")
    _refuse_unless(
        response == {"status": "published"}, "fixture_provisioning_invalid_response"
    )


async def _verified_database(environment=None):
    environment = os.environ if environment is None else environment
    resource_root, resources, manifest = _verified_resources(environment)
    owner = make_url(environment["PLAYWRIGHT_E2E_DATABASE_URL"])
    runtime = make_url(
        environment.get("FIXTURE_RUNTIME_DATABASE_URL") or environment["DATABASE_URL"]
    )
    for url in (owner, runtime):
        _refuse_unless(
            url.drivername == "postgresql+asyncpg"
            and url.host in {"localhost", "127.0.0.1", "::1"}
            and not url.query
            and url.database == resources["database"],
            "fixture_database_target_mismatch",
        )
    _refuse_unless(
        owner.host == runtime.host
        and (owner.port or 5432) == (runtime.port or 5432)
        and owner.username
        and runtime.username
        and owner.username != runtime.username
        and manifest.get("postgres", {}).get("host") == owner.host
        and manifest.get("postgres", {}).get("port") == (owner.port or 5432),
        "fixture_database_roles_mismatch",
    )
    engine = create_async_engine(owner, poolclass=NullPool, hide_parameters=True)
    app_engine = create_async_engine(runtime, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT d.oid, d.datname, pg_catalog.pg_get_userbyid(d.datdba), "
                        "pg_catalog.shobj_description(d.oid, 'pg_database'), current_user "
                        "FROM pg_catalog.pg_database d WHERE d.datname = current_database()"
                    )
                )
            ).one()
            _refuse_unless(
                row[1] == resources["database"]
                and row[3] == resources["owner"]
                and row[4] == owner.username
                and row[2] != runtime.username,
                "fixture_database_owner_unproven",
            )
            identity = {
                "resource_root": str(resource_root),
                "database": row[1],
                "database_oid": row[0],
                "database_owner": row[2],
                "ownership": row[3],
                "postgres_host": owner.host,
                "postgres_port": owner.port or 5432,
                "operator": row[4],
            }
        async with app_engine.connect() as connection:
            role = (
                await connection.execute(
                    text(
                        "SELECT current_user, current_database(), "
                        "(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database()), "
                        "r.rolcanlogin, r.rolsuper, r.rolinherit, r.rolcreatedb, "
                        "r.rolcreaterole, r.rolreplication, r.rolbypassrls, "
                        "pg_catalog.pg_has_role(current_user, :owner, 'MEMBER') "
                        "FROM pg_catalog.pg_roles r WHERE r.rolname=current_user"
                    ),
                    {"owner": owner.username},
                )
            ).one()
            _refuse_unless(
                role[0] == runtime.username
                and role[1] == identity["database"]
                and role[2] == identity["database_oid"]
                and role[3]
                and not any(role[4:]),
                "fixture_runtime_role_unproven",
            )
    finally:
        await app_engine.dispose()
        await engine.dispose()
    return owner, resources, identity


async def verified_fixture_owner_url():
    """Return an owner URL only to a private fixture process after live verification."""
    owner, _, _ = await _verified_database()
    return owner


def _fixture_target_id(nonce):
    return uuid.uuid5(uuid.NAMESPACE_URL, "fixture-original-target:" + nonce)


def _scale_only(root):
    return (
        root.family == "scale"
        and root.original_target_id is None
        and root.original_storage_identity is None
        and root.original_store_receipt_sha256 is None
    )


async def _complete_provisioning(expected):
    """Probe and atomically enroll the real pre-start provisioning run."""
    from app.core.crypto import decrypt_secret, encrypt_secret
    from app.db.models.dataset_asset import StorageLocation
    from app.db.models.fixture_run import FixtureOriginalTarget, FixtureRun
    from app.db.models.storage_connection import StorageConnection
    from app.services import connector_guard, dataset_storage_target
    from app.services.managed_original_retirement import managed_storage_identity
    from app.services.managed_s3_target import Target
    from app.services.source_readers.s3 import S3Access
    from app.services.storage_location import connector_location_id, register_connection
    from fixture_store import observe_empty, verified_store
    from verify_storage_candidates import Owner

    owner_url, resources, identity = await _verified_database()
    manifest = state_path(
        _resource_root(resources), resources["mode"], "fixture-store-owner.json"
    )
    _refuse_unless(
        manifest.exists() and not manifest.is_symlink(), "fixture_manifest_invalid"
    )
    owner = Owner(manifest)
    run_id = uuid.UUID(owner.data["fixture_intent"]["provisioning_run_id"])
    target_id = _fixture_target_id(owner.data["nonce"])
    _refuse_unless(
        expected == {"nonce": owner.data["nonce"], "provisioning_run_id": str(run_id)}
        and "fixture_teardown" not in owner.data
        and not owner.data["cleaned"],
        "fixture_provisioning_identity_changed",
    )
    engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            allowlist = tuple(await connector_guard.get_allowlist(db))
            connector_guard.assert_host_allowed(
                owner.data["fixture_store"]["endpoint"], list(allowlist)
            )
            roots = list(
                await db.scalars(
                    select(FixtureRun).where(FixtureRun.parent_run_id.is_(None))
                )
            )
        store = owner.data["fixture_store"]
        claimed_roots = [root for root in roots if not _scale_only(root)]
        if store["state"] == "provisioned_unpublished":
            _refuse_unless(not claimed_roots, "fixture_unaccounted_roots")

            def probe(endpoint, bucket, account):
                config = {
                    "endpoint": endpoint,
                    "bucket": bucket,
                    "base_prefix": "",
                    "use_ssl": False,
                }
                target = Target(
                    uuid.uuid4(),
                    1,
                    1,
                    endpoint,
                    bucket,
                    dataset_storage_target.managed_prefix(""),
                    purpose="datasets",
                    _access=S3Access(config, encrypt_secret(account), allowlist),
                )
                dataset_storage_target.probe_target(target)

            await _settled_thread(observe_empty, owner, identity, probe=probe)
        store, account = await _settled_thread(verified_store, owner, identity)
        storage_identity = managed_storage_identity(store["endpoint"], store["bucket"])
        receipt = store["receipt_sha256"]
        _refuse_unless(
            all(
                _scale_only(root)
                or (
                    root.original_target_id == target_id
                    and root.original_storage_identity == storage_identity
                    and root.original_store_receipt_sha256 == receipt
                )
                for root in roots
            ),
            "fixture_unaccounted_roots",
        )
        connection_id = uuid.uuid5(
            uuid.NAMESPACE_URL, "fixture-connection:" + owner.data["nonce"]
        )
        config = {
            "endpoint": store["endpoint"],
            "bucket": store["bucket"],
            "base_prefix": "",
            "use_ssl": False,
        }
        async with sessions() as db, db.begin():
            await db.execute(
                text("SELECT pg_catalog.pg_advisory_xact_lock(:key)"),
                {"key": int.from_bytes(b"fixture1", "big", signed=True)},
            )
            root = await db.scalar(
                select(FixtureRun).where(FixtureRun.id == run_id).with_for_update()
            )
            connection = await db.scalar(
                select(StorageConnection)
                .where(StorageConnection.id == connection_id)
                .with_for_update(read=True)
            )
            new_connection = connection is None
            if new_connection:
                _refuse_unless(
                    store["state"] == "observed_unpublished"
                    and not claimed_roots
                    and root is None,
                    "fixture_connection_missing",
                )
                connection = StorageConnection(
                    id=connection_id,
                    name="fixture-original-" + owner.data["nonce"],
                    revision=1,
                    kind="s3",
                    config=config,
                    secret_enc=encrypt_secret(account),
                    scope="global",
                )
                db.add(connection)
                await db.flush()
                location = await register_connection(db, connection)
            else:
                location = await db.scalar(
                    select(StorageLocation)
                    .where(StorageLocation.connection_id == connection_id)
                    .with_for_update(read=True)
                )
            _refuse_unless(
                location is not None
                and location.id == connector_location_id(connection_id)
                and location.connection_id == connection_id
                and location.enabled
                and location.kind == "s3"
                and location.config_revision == 1
                and connection.revision == 1
                and connection.kind == "s3"
                and connection.scope == "global"
                and connection.project_id is None
                and connection.config == config
                and decrypt_secret(connection.secret_enc) == account,
                "fixture_registered_target_changed",
            )
            target = await db.scalar(
                select(FixtureOriginalTarget)
                .where(FixtureOriginalTarget.id == target_id)
                .with_for_update(read=True)
            )
            if target is None:
                _refuse_unless(
                    new_connection and not claimed_roots and root is None,
                    "fixture_target_missing",
                )
                target = FixtureOriginalTarget(
                    id=target_id,
                    location_id=location.id,
                    bucket=store["bucket"],
                    generation=1,
                    storage_identity=storage_identity,
                    store_receipt_sha256=receipt,
                )
                db.add(target)
                await db.flush()
            _refuse_unless(
                target.location_id == location.id
                and target.bucket == store["bucket"]
                and target.generation == 1
                and target.storage_identity == storage_identity
                and target.store_receipt_sha256 == receipt,
                "fixture_registered_target_changed",
            )
            if root is None:
                _refuse_unless(
                    store["state"] == "observed_unpublished" and not claimed_roots,
                    "fixture_provisioning_root_missing",
                )
                stamp = datetime.now(timezone.utc)
                db.add(
                    FixtureRun(
                        id=run_id,
                        parent_run_id=None,
                        scope_key=None,
                        family="owned",
                        state="retired",
                        sealed_at=stamp,
                        retired_at=stamp,
                        original_target_id=target.id,
                        original_storage_identity=storage_identity,
                        original_store_receipt_sha256=receipt,
                    )
                )
                await db.flush()
            else:
                _refuse_unless(
                    root.parent_run_id is None
                    and root.scope_key is None
                    and root.family == "owned"
                    and root.state == "retired"
                    and root.sealed_at is not None
                    and root.retired_at is not None
                    and root.original_target_id == target.id
                    and root.original_storage_identity == storage_identity
                    and root.original_store_receipt_sha256 == receipt,
                    "fixture_provisioning_root_changed",
                )
        store["state"] = "published"
        owner.data["fixture_intent"]["state"] = "published"
        owner.save()
    finally:
        await engine.dispose()


async def prepare_worktree_store(
    environment, *, resources, configured_key, configured_allowlist
):
    """Prepare owned physical storage and child-only connector configuration."""
    from cryptography.fernet import Fernet
    from yoao_assets_source.connectors.crypto import decrypt_secret
    from yoao_assets_source.connectors.host_guard import (
        assert_host_allowed,
        normalize_allowlist,
    )
    from fixture_store import (
        _private_bytes,
        inspect_prepared_store,
        provision,
        verify_prerequisites,
    )
    from verify_storage_candidates import Owner, private_write
    from storage_s3_contract import ContractError

    owner_url, verified_resources, identity = await _verified_database(environment)
    _refuse_unless(resources == verified_resources, "fixture_topology_invalid")
    resource_root = _resource_root(resources)
    manifest_path = state_path(
        resource_root, resources["mode"], "fixture-store-owner.json"
    )
    key_path = state_path(
        resource_root, resources["mode"], "fixture-connector-key.json"
    )
    lock_path = state_path(resource_root, resources["mode"], "fixture-store.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock_fd)
        _refuse_unless(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600,
            "fixture_lock_invalid",
        )
        await _settled_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
        engine = create_async_engine(
            owner_url, poolclass=NullPool, hide_parameters=True
        )
        try:
            async with engine.connect() as db:
                _refuse_unless(
                    await db.scalar(
                        text("SELECT to_regclass('yoao_fixture_v1.fixture_runs')")
                    )
                    is not None,
                    "fixture_schema_missing",
                )
                _refuse_unless(
                    not await db.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                            "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                            "AND backend_type='client backend')"
                        )
                    ),
                    "fixture_database_sessions_active",
                )
                roots_exist = await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_runs "
                        "WHERE parent_run_id IS NULL AND NOT (family='scale' "
                        "AND original_target_id IS NULL AND original_storage_identity IS NULL "
                        "AND original_store_receipt_sha256 IS NULL))"
                    )
                )
                targets_exist = await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM "
                        "yoao_fixture_v1.fixture_original_targets)"
                    )
                )
                encrypted = list(
                    (
                        await db.execute(
                            text(
                                "SELECT secret_enc FROM public.storage_connections "
                                "WHERE secret_enc IS NOT NULL"
                            )
                        )
                    ).scalars()
                )
                override = await db.scalar(
                    text(
                        "SELECT value_json FROM public.system_settings "
                        "WHERE key='connector_host_allowlist'"
                    )
                )

            try:
                await _settled_thread(verify_prerequisites)
            except ContractError as exc:
                raise FixtureSetupRefused(str(exc)) from None
            key = configured_key or ""
            if key_path.exists():
                saved = json.loads(_private_bytes(key_path))
                _refuse_unless(
                    set(saved) == {"owner", "database_oid", "key"}
                    and saved["owner"] == resources["owner"]
                    and saved["database_oid"] == identity["database_oid"]
                    and isinstance(saved["key"], str)
                    and (not key or key == saved["key"]),
                    "fixture_connector_key_changed",
                )
                key = saved["key"]
            elif not key:
                _refuse_unless(not encrypted, "fixture_existing_connector_key_missing")
                key = Fernet.generate_key().decode("ascii")
                private_write(
                    key_path,
                    json.dumps(
                        {
                            "owner": resources["owner"],
                            "database_oid": identity["database_oid"],
                            "key": key,
                        }
                    ).encode(),
                )
            try:
                Fernet(key.encode("ascii"))
                for secret in encrypted:
                    decrypt_secret(secret, key)
            except Exception:
                raise FixtureSetupRefused("fixture_connector_key_unproven") from None

            if not manifest_path.exists():
                _refuse_unless(
                    not roots_exist and not targets_exist, "fixture_unaccounted_roots"
                )
                owner = Owner(manifest_path, create=True)
                owner.data["fixture_intent"] = {
                    "database": identity,
                    "provisioning_run_id": str(uuid.uuid4()),
                    "state": "provisioning",
                }
                owner.save()
                await _settled_thread(provision, owner, identity)
            else:
                _refuse_unless(
                    not manifest_path.is_symlink(), "fixture_manifest_invalid"
                )
                owner = Owner(manifest_path)
                _refuse_unless(not owner.data["cleaned"], "fixture_store_cleaned")
            _refuse_unless(
                "fixture_teardown" not in owner.data, "fixture_teardown_pending"
            )
            address = await _settled_thread(inspect_prepared_store, owner, identity)
            allowlist = normalize_allowlist([*configured_allowlist, address])
            if override is not None:
                _refuse_unless(isinstance(override, list), "fixture_allowlist_invalid")
                effective = normalize_allowlist(override)
            else:
                effective = allowlist
            assert_host_allowed(f"http://{address}:9000", effective)
            child_environment = {
                **{
                    name: value
                    for name, value in environment.items()
                    if name.upper() not in FIXTURE_ORIGINAL_ENV_KEYS
                },
                "CONNECTOR_ENCRYPTION_KEY": key,
                "CONNECTOR_HOST_ALLOWLIST": json.dumps(allowlist),
            }
            payload = {
                "nonce": owner.data["nonce"],
                "provisioning_run_id": owner.data["fixture_intent"][
                    "provisioning_run_id"
                ],
            }
            await _settled(
                asyncio.create_task(_provisioning_child(child_environment, payload))
            )
            from fixture_store import reader_credentials

            # Reload the committed journal after the private child exits.
            owner = Owner(manifest_path)
            _refuse_unless(
                owner.data["fixture_store"]["state"] == "published",
                "fixture_enrollment_missing",
            )
            reader = reader_credentials(owner, owner.data["fixture_store"])
            store = owner.data["fixture_store"]
            return {
                "CONNECTOR_ENCRYPTION_KEY": key,
                "CONNECTOR_HOST_ALLOWLIST": json.dumps(allowlist),
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT": store["endpoint"],
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET": store["bucket"],
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY": reader["access_key"],
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY": reader["secret_key"],
            }
        finally:
            await engine.dispose()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


async def _settled_database(owner_url, resources, owner):
    """Recheck owner identity and all location-wide/fixture physical admissions."""
    from app.services.managed_original_retirement import managed_storage_identity
    from app.services.storage_location import connector_location_id

    store = owner.data["fixture_store"]
    identity = store["database"]
    _refuse_unless(
        identity.get("resource_root") == str(_resource_root(resources)),
        "fixture_resource_root_changed",
    )
    engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as db:
            observed = (
                await db.execute(
                    text(
                        "SELECT d.oid, d.datname, pg_catalog.pg_get_userbyid(d.datdba), "
                        "pg_catalog.shobj_description(d.oid, 'pg_database'), current_user "
                        "FROM pg_catalog.pg_database d WHERE d.datname = current_database()"
                    )
                )
            ).one()
            _refuse_unless(
                observed[0] == identity["database_oid"]
                and observed[1] == resources["database"] == identity["database"]
                and observed[2] == identity["database_owner"]
                and observed[3] == resources["owner"] == identity["ownership"]
                and observed[4] == identity["operator"] == owner_url.username,
                "fixture_database_owner_unproven",
            )
            _refuse_unless(
                not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                        "AND backend_type='client backend')"
                    )
                ),
                "fixture_database_sessions_active",
            )
            for table in (
                "yoao_fixture_v1.fixture_original_targets",
                "yoao_fixture_v1.fixture_runs",
                "yoao_fixture_v1.fixture_run_admissions",
                "yoao_fixture_v1.fixture_maintenance_windows",
                "public.storage_operations",
            ):
                _refuse_unless(
                    await db.scalar(text("SELECT to_regclass(:name)"), {"name": table})
                    is not None,
                    "fixture_settlement_schema_missing",
                )
            storage_identity = managed_storage_identity(
                store["endpoint"], store["bucket"]
            )
            target_id = _fixture_target_id(owner.data["nonce"])
            roots = (
                await db.execute(
                    text(
                        "SELECT id, family, original_target_id, original_storage_identity, "
                        "original_store_receipt_sha256 FROM yoao_fixture_v1.fixture_runs "
                        "WHERE parent_run_id IS NULL"
                    )
                )
            ).all()
            claimed_roots = [root for root in roots if not _scale_only(root)]
            provisioning_id = uuid.UUID(
                owner.data["fixture_intent"]["provisioning_run_id"]
            )
            _refuse_unless(
                (
                    store["state"] != "published"
                    or any(root.id == provisioning_id for root in claimed_roots)
                )
                and all(
                    root.original_target_id == target_id
                    and root.original_storage_identity == storage_identity
                    and root.original_store_receipt_sha256
                    == store.get("receipt_sha256")
                    for root in claimed_roots
                ),
                "fixture_enrollment_changed",
            )
            for query in (
                "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_runs WHERE state <> 'retired')",
                "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_run_admissions WHERE state <> 'settled')",
                "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_maintenance_windows WHERE state <> 'resumed')",
            ):
                _refuse_unless(
                    not await db.scalar(text(query)), "fixture_work_unsettled"
                )
            connection_id = uuid.uuid5(
                uuid.NAMESPACE_URL, "fixture-connection:" + owner.data["nonce"]
            )
            location_id = connector_location_id(connection_id)
            targets = (
                await db.execute(
                    text(
                        "SELECT t.id, t.location_id, t.bucket, t.generation, "
                        "t.storage_identity, t.store_receipt_sha256, l.connection_id, "
                        "l.enabled, l.kind AS location_kind, l.config_revision, c.config, "
                        "c.kind AS connection_kind, "
                        "c.scope, c.project_id, c.revision "
                        "FROM yoao_fixture_v1.fixture_original_targets t "
                        "JOIN public.storage_locations l ON l.id=t.location_id "
                        "JOIN public.storage_connections c ON c.id=l.connection_id "
                        "WHERE t.id=:target_id OR t.storage_identity=:storage_identity"
                    ),
                    {"target_id": target_id, "storage_identity": storage_identity},
                )
            ).all()
            _refuse_unless(
                (not claimed_roots and not targets)
                or (
                    bool(claimed_roots)
                    and len(targets) == 1
                    and targets[0].id == target_id
                    and targets[0].location_id == location_id
                    and targets[0].bucket == store["bucket"]
                    and targets[0].generation == 1
                    and targets[0].storage_identity == storage_identity
                    and targets[0].store_receipt_sha256 == store.get("receipt_sha256")
                    and targets[0].connection_id == connection_id
                    and targets[0].enabled
                    and targets[0].location_kind == "s3"
                    and targets[0].config_revision == 1
                    and targets[0].config
                    == {
                        "endpoint": store["endpoint"],
                        "bucket": store["bucket"],
                        "base_prefix": "",
                        "use_ssl": False,
                    }
                    and targets[0].connection_kind == "s3"
                    and targets[0].scope == "global"
                    and targets[0].project_id is None
                    and targets[0].revision == 1
                ),
                "fixture_registered_target_changed",
            )
            _refuse_unless(
                not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM public.storage_operations "
                        "WHERE location_id=:location_id)"
                    ),
                    {"location_id": location_id},
                ),
                "fixture_storage_unsettled",
            )
            gate = await db.scalar(
                text(
                    "SELECT state FROM public.storage_maintenance_gates "
                    "WHERE location_id=:location_id"
                ),
                {"location_id": location_id},
            )
            _refuse_unless(gate in {None, "active"}, "fixture_maintenance_window_busy")
    finally:
        await engine.dispose()


async def _verify_partial_provisioning(owner_url, resources, owner):
    """Validate a durable pre-publication creation intent before exact recovery."""
    from fixture_store import MINIO_IMAGE
    from storage_s3_contract import CHECKOUT_LABEL, OWNER_LABEL
    from verify_storage_candidates import command

    intent = owner.data.get("fixture_intent")
    _refuse_unless(
        isinstance(intent, dict)
        and intent.get("state") == "provisioning"
        and intent.get("database", {}).get("database") == resources["database"]
        and intent["database"].get("ownership") == resources["owner"]
        and intent["database"].get("resource_root") == str(_resource_root(resources))
        and not owner.data["cleaned"],
        "fixture_partial_intent_unknown",
    )
    items = owner.data["resources"]
    _refuse_unless(
        len(items) <= 3
        and all(
            item.get("kind") in {"volume", "network", "container"} for item in items
        )
        and len({item["kind"] for item in items}) == len(items),
        "fixture_inventory_changed",
    )
    container_ids = set()
    observed_items = []
    for item in items:
        observed = owner.inspect(item)
        if observed is None:
            continue
        actual_id = (
            observed.get("Name") if item["kind"] == "volume" else observed.get("Id")
        )
        labels = (
            observed.get("Config", {}).get("Labels", {})
            if item["kind"] == "container"
            else observed.get("Labels", {})
        )
        _refuse_unless(
            (
                item.get("id") == actual_id
                or (item.get("id") is None and item.get("state") == "creation_intent")
            )
            and labels.get(OWNER_LABEL) == owner.data["nonce"]
            and labels.get(CHECKOUT_LABEL) == str(ROOT),
            "fixture_partial_resource_unknown",
        )
        if item["kind"] == "container":
            _refuse_unless(
                item.get("image") == MINIO_IMAGE
                and observed.get("Config", {}).get("Image") == MINIO_IMAGE,
                "fixture_partial_resource_unknown",
            )
            container_ids.add(actual_id)
        observed_items.append((item, observed, actual_id))
    for item, observed, actual_id in observed_items:
        if item["kind"] == "network":
            _refuse_unless(
                set(observed.get("Containers", {})).issubset(container_ids),
                "fixture_network_in_use",
            )
        elif item["kind"] == "volume":
            users = command(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--no-trunc",
                    "--filter",
                    "volume=" + actual_id,
                    "--format",
                    "{{.ID}}",
                ]
            ).splitlines()
            _refuse_unless(set(users).issubset(container_ids), "fixture_volume_in_use")
    engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT d.oid,d.datname,pg_catalog.shobj_description(d.oid,'pg_database'),"
                        "current_user FROM pg_catalog.pg_database d WHERE datname=current_database()"
                    )
                )
            ).one()
            _refuse_unless(
                row[0] == intent["database"]["database_oid"]
                and row[1] == resources["database"]
                and row[2] == resources["owner"]
                and row[3] == intent["database"]["operator"] == owner_url.username,
                "fixture_database_owner_unproven",
            )
            _refuse_unless(
                not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                        "AND backend_type='client backend')"
                    )
                ),
                "fixture_database_sessions_active",
            )
            _refuse_unless(
                await db.scalar(
                    text("SELECT to_regclass('yoao_fixture_v1.fixture_runs')")
                )
                is not None
                and await db.scalar(
                    text(
                        "SELECT to_regclass('yoao_fixture_v1.fixture_original_targets')"
                    )
                )
                is not None
                and not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_runs "
                        "WHERE state <> 'retired' OR NOT (family='scale' "
                        "AND original_target_id IS NULL AND original_storage_identity IS NULL "
                        "AND original_store_receipt_sha256 IS NULL))"
                    )
                )
                and not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_original_targets)"
                    )
                ),
                "fixture_unaccounted_roots",
            )
    finally:
        await engine.dispose()


async def prepare_store_destruction(owner_url, resources, database_exists):
    """Fence a fully settled owned store before the worktree database is dropped."""
    from fixture_store import (
        _private_bytes,
        fence_account_for_destroy,
        retain_receipt,
        verify_empty_for_destroy,
    )
    from verify_storage_candidates import Owner

    resource_root = _resource_root(resources)
    manifest = state_path(resource_root, resources["mode"], "fixture-store-owner.json")
    key_path = state_path(
        resource_root, resources["mode"], "fixture-connector-key.json"
    )
    if not manifest.exists():
        _refuse_unless(not key_path.exists(), "fixture_manifest_missing")
        if database_exists:
            engine = create_async_engine(
                owner_url, poolclass=NullPool, hide_parameters=True
            )
            try:
                async with engine.connect() as db:
                    if (
                        await db.scalar(
                            text("SELECT to_regclass('yoao_fixture_v1.fixture_runs')")
                        )
                        is not None
                    ):
                        _refuse_unless(
                            await db.scalar(
                                text(
                                    "SELECT to_regclass('yoao_fixture_v1.fixture_original_targets')"
                                )
                            )
                            is not None
                            and not await db.scalar(
                                text(
                                    "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_runs "
                                    "WHERE state <> 'retired' OR NOT (family='scale' "
                                    "AND original_target_id IS NULL AND original_storage_identity IS NULL "
                                    "AND original_store_receipt_sha256 IS NULL))"
                                )
                            )
                            and not await db.scalar(
                                text(
                                    "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_original_targets)"
                                )
                            ),
                            "fixture_manifest_missing",
                        )
            finally:
                await engine.dispose()
        return None
    _refuse_unless(not manifest.is_symlink(), "fixture_manifest_invalid")
    owner = Owner(manifest)
    store = owner.data.get("fixture_store")
    if store is None:
        intent = owner.data.get("fixture_intent")
        _refuse_unless(
            isinstance(intent, dict)
            and intent.get("state") == "provisioning"
            and intent.get("database", {}).get("resource_root") == str(resource_root),
            "fixture_partial_intent_unknown",
        )
        if key_path.exists():
            saved = json.loads(_private_bytes(key_path))
            _refuse_unless(
                set(saved) == {"owner", "database_oid", "key"}
                and saved["owner"] == resources["owner"]
                and saved["database_oid"] == intent["database"]["database_oid"],
                "fixture_connector_key_changed",
            )
        if database_exists:
            await _verify_partial_provisioning(owner_url, resources, owner)
            owner.data["fixture_teardown"] = {"state": "ready", "partial": True}
            owner.save()
        else:
            _refuse_unless(
                owner.data.get("fixture_teardown")
                == {"state": "ready", "partial": True},
                "fixture_database_missing",
            )
        return owner
    _refuse_unless(
        isinstance(store, dict)
        and store["database"]["database"] == resources["database"]
        and store["database"]["ownership"] == resources["owner"]
        and store["database"].get("resource_root") == str(resource_root),
        "fixture_store_identity_changed",
    )
    if key_path.exists():
        saved = json.loads(_private_bytes(key_path))
        _refuse_unless(
            set(saved) == {"owner", "database_oid", "key"}
            and saved["owner"] == resources["owner"]
            and saved["database_oid"] == store["database"]["database_oid"],
            "fixture_connector_key_changed",
        )
    if not database_exists:
        _refuse_unless(
            owner.data.get("fixture_teardown", {}).get("state") == "ready",
            "fixture_database_missing",
        )
        if store["state"] != "provisioned_unpublished":
            receipt_path = state_path(
                resource_root,
                resources["mode"],
                "fixture-original-receipt-" + owner.data["nonce"] + ".json",
            )
            import hashlib

            _refuse_unless(
                hashlib.sha256(_private_bytes(receipt_path)).hexdigest()
                == store["receipt_sha256"],
                "fixture_receipt_changed",
            )
        container = next(
            item
            for item in owner.data["resources"]
            if item["kind"] == "container" and item["id"] == store["container_id"]
        )
        if owner.inspect(container) is not None:
            await _settled_thread(verify_empty_for_destroy, owner, store["database"])
            await _settled_thread(fence_account_for_destroy, owner)
        return owner
    _refuse_unless(not owner.data["cleaned"], "fixture_store_cleaned")
    await _settled_database(owner_url, resources, owner)
    await _settled_thread(verify_empty_for_destroy, owner, store["database"])
    await _settled_thread(fence_account_for_destroy, owner)
    await _settled_database(owner_url, resources, owner)
    await _settled_thread(verify_empty_for_destroy, owner, store["database"])
    receipt_path = state_path(
        resource_root,
        resources["mode"],
        "fixture-original-receipt-" + owner.data["nonce"] + ".json",
    )
    retain_receipt(owner, receipt_path)
    owner.data["fixture_teardown"]["state"] = "ready"
    owner.save()
    return owner


def finalize_store_destruction(owner, resources):
    """Dispose only the journaled exact IDs after the database has been removed."""
    if owner is None:
        return
    resource_root = _resource_root(resources)
    _refuse_unless(
        owner.path
        == state_path(resource_root, resources["mode"], "fixture-store-owner.json"),
        "fixture_manifest_mismatch",
    )
    _refuse_unless(
        owner.data.get("fixture_intent", {}).get("database", {}).get("resource_root")
        == str(resource_root),
        "fixture_resource_root_changed",
    )
    _refuse_unless(
        owner.data.get("fixture_teardown", {}).get("state") == "ready",
        "fixture_teardown_state",
    )
    if not owner.data["cleaned"]:
        _refuse_unless(owner.cleanup(), "fixture_store_cleanup_failed")
    archive = state_path(
        resource_root,
        resources["mode"],
        "fixture-store-owner-" + owner.data["nonce"] + ".json",
    )
    if archive.exists():
        _refuse_unless(
            archive.read_bytes() == owner.path.read_bytes(), "fixture_archive_conflict"
        )
    else:
        os.link(owner.path, archive, follow_symlinks=False)
    state_path(resource_root, resources["mode"], "fixture-connector-key.json").unlink(
        missing_ok=True
    )
    owner.path.unlink()
    descriptor = os.open(archive.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def prepare_fixture_suite(suite_id: str, family: str, *, verify_only: bool = False):
    """Register or verify an open suite against the enrolled dedicated store."""
    from app.core.crypto import decrypt_secret
    from app.db.models.dataset_asset import StorageLocation
    from app.db.models.fixture_run import FixtureOriginalTarget, FixtureRun
    from app.db.models.storage_connection import StorageConnection
    from app.services import connector_guard
    from app.services.managed_original_retirement import managed_storage_identity
    from app.services.storage_location import connector_location_id
    from fixture_store import verified_store
    from verify_storage_candidates import Owner

    try:
        identifier = uuid.UUID(suite_id)
    except (ValueError, TypeError, AttributeError):
        raise FixtureSetupRefused("fixture_suite_invalid") from None
    _refuse_unless(
        str(identifier) == suite_id and family in {"owned", "screenshot", "scale"},
        "fixture_suite_invalid",
    )
    owner_url, resources, identity = await _verified_database()
    resource_root = _resource_root(resources)
    manifest = state_path(resource_root, resources["mode"], "fixture-store-owner.json")
    lock_path = state_path(resource_root, resources["mode"], "fixture-store.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock_fd)
        _refuse_unless(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600,
            "fixture_lock_invalid",
        )
        await _settled_thread(fcntl.flock, lock_fd, fcntl.LOCK_EX)
        _refuse_unless(
            manifest.exists() and not manifest.is_symlink(),
            "fixture_store_not_prepared",
        )
        owner = Owner(manifest)
        _refuse_unless(
            not owner.data["cleaned"] and "fixture_teardown" not in owner.data,
            "fixture_store_unavailable",
        )
        store, account = await _settled_thread(verified_store, owner, identity)
        _refuse_unless(store["state"] == "published", "fixture_enrollment_missing")
        provisioning_id = uuid.UUID(owner.data["fixture_intent"]["provisioning_run_id"])
        target_id = _fixture_target_id(owner.data["nonce"])
        _refuse_unless(identifier != provisioning_id, "fixture_suite_changed")
        storage_identity = managed_storage_identity(store["endpoint"], store["bucket"])
        receipt = store["receipt_sha256"]
        connection_id = uuid.uuid5(
            uuid.NAMESPACE_URL, "fixture-connection:" + owner.data["nonce"]
        )
        location_id = connector_location_id(connection_id)
        config = {
            "endpoint": store["endpoint"],
            "bucket": store["bucket"],
            "base_prefix": "",
            "use_ssl": False,
        }
        engine = create_async_engine(
            owner_url, poolclass=NullPool, hide_parameters=True
        )
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db, db.begin():
                await db.execute(
                    text("SELECT pg_catalog.pg_advisory_xact_lock(:key)"),
                    {"key": int.from_bytes(b"fixture1", "big", signed=True)},
                )
                allowlist = await connector_guard.get_allowlist(db)
                connector_guard.assert_host_allowed(store["endpoint"], allowlist)
                roots = list(
                    await db.scalars(
                        select(FixtureRun).where(FixtureRun.parent_run_id.is_(None))
                    )
                )
                provisioning = next(
                    (root for root in roots if root.id == provisioning_id), None
                )
                _refuse_unless(
                    provisioning is not None
                    and provisioning.family == "owned"
                    and provisioning.state == "retired"
                    and provisioning.scope_key is None
                    and provisioning.sealed_at is not None
                    and provisioning.retired_at is not None
                    and all(
                        _scale_only(root)
                        or (
                            root.original_target_id == target_id
                            and root.original_storage_identity == storage_identity
                            and root.original_store_receipt_sha256 == receipt
                        )
                        for root in roots
                    ),
                    "fixture_enrollment_changed",
                )
                connection = await db.scalar(
                    select(StorageConnection)
                    .where(StorageConnection.id == connection_id)
                    .with_for_update(read=True)
                )
                location = await db.scalar(
                    select(StorageLocation)
                    .where(StorageLocation.id == location_id)
                    .with_for_update(read=True)
                )
                target = await db.scalar(
                    select(FixtureOriginalTarget)
                    .where(FixtureOriginalTarget.id == target_id)
                    .with_for_update(read=True)
                )
                _refuse_unless(
                    connection is not None
                    and connection.kind == "s3"
                    and connection.scope == "global"
                    and connection.project_id is None
                    and connection.revision == 1
                    and connection.config == config
                    and decrypt_secret(connection.secret_enc) == account
                    and location is not None
                    and location.enabled
                    and location.kind == "s3"
                    and location.config_revision == 1
                    and location.connection_id == connection_id
                    and target is not None
                    and target.location_id == location_id
                    and target.bucket == store["bucket"]
                    and target.generation == 1
                    and target.storage_identity == storage_identity
                    and target.store_receipt_sha256 == receipt,
                    "fixture_registered_target_changed",
                )
                root = await db.scalar(
                    select(FixtureRun)
                    .where(FixtureRun.id == identifier)
                    .with_for_update()
                )
                if root is None:
                    _refuse_unless(not verify_only, "fixture_suite_missing")
                    db.add(
                        FixtureRun(
                            id=identifier,
                            parent_run_id=None,
                            scope_key=None,
                            family=family,
                            state="open",
                            original_target_id=target.id,
                            original_storage_identity=storage_identity,
                            original_store_receipt_sha256=receipt,
                        )
                    )
                    await db.flush()
                else:
                    _refuse_unless(
                        root.parent_run_id is None
                        and root.scope_key is None
                        and root.family == family
                        and root.state == "open"
                        and root.original_target_id == target.id
                        and root.original_storage_identity == storage_identity
                        and root.original_store_receipt_sha256 == receipt,
                        "fixture_suite_changed",
                    )
            return {"suite_id": suite_id, "family": family}
        finally:
            await engine.dispose()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


async def prepare_native_run_target(run_id: uuid.UUID, admission_id: uuid.UUID):
    """Provision and register an admitted run's exact native output target."""
    from app.services.storage_access import mark_unsettled
    from fixture_media_control import register_native_target
    from fixture_store import provision_native_output, verified_native_output
    from verify_storage_candidates import Owner

    _refuse_unless(
        isinstance(run_id, uuid.UUID) and isinstance(admission_id, uuid.UUID),
        "fixture_native_run_invalid",
    )
    owner_url, resources, identity = await _verified_database()
    root = _resource_root(resources)
    manifest = state_path(root, resources["mode"], "fixture-store-owner.json")
    lock_path = state_path(root, resources["mode"], "fixture-store.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    owner = None
    registration_started = False
    try:
        info = os.fstat(descriptor)
        current = lock_path.lstat()
        _refuse_unless(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "fixture_lock_invalid",
        )
        await _settled_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        current = lock_path.lstat()
        _refuse_unless(
            (info.st_dev, info.st_ino, info.st_nlink)
            == (current.st_dev, current.st_ino, current.st_nlink),
            "fixture_lock_changed",
        )
        engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
        try:
            async with engine.connect() as connection:
                admitted = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_runs r "
                        "JOIN yoao_fixture_v1.fixture_runs s ON s.id = r.parent_run_id "
                        "JOIN yoao_fixture_v1.fixture_run_admissions a ON a.run_id = r.id "
                        "WHERE r.id = :run AND a.id = :admission "
                        "AND a.state = 'active' AND r.state = 'open' AND s.state = 'open')"
                    ),
                    {"run": run_id, "admission": admission_id},
                )
            _refuse_unless(admitted is True, "fixture_native_admission_missing")
        finally:
            await engine.dispose()
        _refuse_unless(
            manifest.exists() and not manifest.is_symlink(),
            "fixture_store_not_prepared",
        )
        owner = Owner(manifest)
        _refuse_unless(
            not owner.data["cleaned"] and "fixture_teardown" not in owner.data,
            "fixture_store_unavailable",
        )
        targets = owner.data.get("fixture_native_outputs", {})
        _refuse_unless(isinstance(targets, dict), "fixture_native_targets_invalid")
        if run_id.hex in targets:
            target = await _settled_thread(verified_native_output, owner, identity, run_id)
        else:
            target = await _settled_thread(provision_native_output, owner, identity, run_id)
        registration_started = True
        registered = await _settled(
            asyncio.create_task(register_native_target(run_id, owner, identity))
        )
        _refuse_unless(registered == target, "fixture_native_target_changed")
        return target
    except BaseException as failure:
        if registration_started or (
            owner is not None
            and isinstance(owner.data.get("fixture_native_outputs"), dict)
            and run_id.hex in owner.data["fixture_native_outputs"]
        ):
            mark_unsettled(failure)
        raise
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


async def retire_native_run_target(run_id: uuid.UUID, control_url):
    """Retire one physically verified target under its still-active Native owner."""
    from fixture_store import retire_native_output
    from verify_storage_candidates import Owner

    _refuse_unless(isinstance(run_id, uuid.UUID), "fixture_native_run_invalid")
    owner_url, resources, identity = await _verified_database()
    root = _resource_root(resources)
    manifest = state_path(root, resources["mode"], "fixture-store-owner.json")
    lock_path = state_path(root, resources["mode"], "fixture-store.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info, current = os.fstat(descriptor), lock_path.lstat()
        _refuse_unless(
            stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "fixture_lock_invalid",
        )
        await _settled_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        current = lock_path.lstat()
        _refuse_unless(
            (info.st_dev, info.st_ino, info.st_nlink)
            == (current.st_dev, current.st_ino, current.st_nlink),
            "fixture_lock_changed",
        )
        _refuse_unless(
            manifest.exists() and not manifest.is_symlink(),
            "fixture_store_not_prepared",
        )
        owner = Owner(manifest)

        async def retire_and_acknowledge():
            proof = await retire_native_output(
                owner, identity, run_id, control_url=control_url
            )
            retirement = owner.data["fixture_native_outputs"][run_id.hex]["retirement"]
            receipt = retirement["physical_receipt"]
            _refuse_unless(
                isinstance(receipt, dict)
                and receipt.get("schema") == "aap-fixture-native-target-physical-retirement/v1"
                and receipt.get("native_receipt") == proof
                and receipt.get("bucket_absent") is True,
                "fixture_native_physical_receipt_changed",
            )
            serialized = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            _refuse_unless(
                digest == retirement.get("physical_receipt_sha256"),
                "fixture_native_physical_receipt_changed",
            )
            engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
            try:
                async with engine.begin() as connection:
                    acknowledged = await connection.scalar(
                        text(
                            "SELECT aap_assets_v1."
                            "acknowledge_fixture_native_target_physical_retirement(:receipt,:sha256)"
                        ),
                        {"receipt": serialized, "sha256": digest},
                    )
                    _refuse_unless(
                        acknowledged is True, "fixture_native_database_retirement_unconfirmed"
                    )
            finally:
                await engine.dispose()
            return proof

        return await _settled(asyncio.create_task(retire_and_acknowledge()))
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


if __name__ == "__main__":
    if sys.argv[1:] != ["--complete-provisioning"]:
        raise SystemExit(2)
    sys.path.insert(0, str(ROOT / "apps/api"))
    try:
        payload = json.loads(sys.stdin.readline())
        with redirect_stdout(sys.stderr):
            asyncio.run(_complete_provisioning(payload))
        print(json.dumps({"status": "published"}), flush=True)
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, FixtureSetupRefused)
            else type(error).__name__
        )
        print(json.dumps({"error": code}), flush=True)
        raise SystemExit(1) from None
