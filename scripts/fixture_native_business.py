"""Provision the private native business identities for one verified fixture suite."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import uuid
from urllib.parse import quote

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from fixture_suite_coordinator import (
    FixtureSetupRefused,
    _resource_root,
    _settled,
    _settled_thread,
    _verified_database,
)
from worktree_env import state_path


def _require(condition, code):
    if not condition:
        raise FixtureSetupRefused(code)


def _private_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        current = Path(path).lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "fixture_native_business_secret_invalid",
        )
        return stream.read()


def _private_json(path):
    return json.loads(_private_bytes(path))


def _mc_output(scope, arguments, *, absent_code=None, payload=None):
    if scope.get("provider") == "raster":
        raster, _, _, _ = _raster_identity(
            Path(scope["raster_owner"]), scope["resources"], scope
        )
        private = raster.private
    else:
        from worktree_resources import docker

        observed = json.loads(docker("inspect", scope["container"]))[0]
        _require(
            observed.get("Id") == scope["container"]
            and observed.get("State", {}).get("Running") is True
            and (observed.get("Config", {}).get("Labels") or {}).get(
                "com.docker.compose.service"
            )
            == "minio",
            "fixture_native_business_minio_changed",
        )
        private = Path(scope["private_dir"])
    config = Path(scope["config"])
    _require(
        config.parent == private and config.name.startswith("native-business-mc-"),
        "fixture_native_business_mc_directory_invalid",
    )
    config.mkdir(mode=0o700, exist_ok=True)
    info = config.lstat()
    _require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700,
        "fixture_native_business_mc_directory_invalid",
    )
    result = subprocess.run(
        ["mc", "--json", *arguments],
        env={
            "PATH": os.environ["PATH"],
            "MC_HOST_scoped": scope["host"],
            "MC_CONFIG_DIR": scope["config"],
        },
        input=payload,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if absent_code is not None and result.returncode != 0:
        _require(result.returncode == 1, "fixture_native_business_iam_unavailable")
        try:
            reply = json.loads(result.stdout)
            code = reply["error"]["cause"]["error"]["Code"]
        except (ValueError, TypeError, KeyError):
            raise FixtureSetupRefused(
                "fixture_native_business_iam_absence_unproven"
            ) from None
        _require(
            isinstance(reply, dict)
            and reply.get("status") == "error"
            and code == absent_code,
            "fixture_native_business_iam_absence_unproven",
        )
        return None
    _require(result.returncode == 0, "fixture_native_business_iam_unavailable")
    return result.stdout


def _mc_info(scope, arguments):
    return json.loads(_mc_output(scope, arguments))


def _iam_components(scope):
    user = _mc_output(
        scope,
        ["admin", "user", "info", "scoped", scope["access_key"]],
        absent_code="XMinioAdminNoSuchUser",
    )
    policy = _mc_output(
        scope,
        ["admin", "policy", "info", "scoped", scope["policy"]],
        absent_code="XMinioAdminNoSuchPolicy",
    )
    return json.loads(user) if user is not None else None, json.loads(
        policy
    ) if policy is not None else None


def _assert_iam_absent(scope):
    _require(
        _iam_components(scope) == (None, None), "fixture_native_business_iam_exists"
    )


def _verify_policy(scope, policy):
    from fixture_store import _canonical_native_output_policy
    from verify_module_data_boundary import _media_storage_policy

    expected = _canonical_native_output_policy(
        _media_storage_policy(scope, worker=False)
    )
    _require(
        isinstance(policy, dict)
        and policy.get("status") == "success"
        and _canonical_native_output_policy(policy["policyInfo"]["Policy"]) == expected,
        "fixture_native_business_iam_changed",
    )
    return hashlib.sha256(
        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _verify_account(scope):
    from contextlib import closing
    from fixture_store import _client

    user = _mc_info(scope, ["admin", "user", "info", "scoped", scope["access_key"]])
    policy = _mc_info(scope, ["admin", "policy", "info", "scoped", scope["policy"]])
    policy_sha256 = _verify_policy(scope, policy)
    _require(
        user.get("status") == "success"
        and user.get("accessKey") == scope["access_key"]
        and user.get("userStatus") == "enabled"
        and user.get("policyName") == scope["policy"]
        and not user.get("memberOf"),
        "fixture_native_business_iam_changed",
    )
    with closing(
        _client(scope["endpoint"], scope["access_key"], scope["secret_key"])
    ) as client:
        _require(
            client.get_bucket_versioning(Bucket=scope["inputs_bucket"]).get("Status")
            == "Enabled",
            "fixture_native_business_credentials_unproven",
        )
    return {
        "identity_sha256": hashlib.sha256(scope["access_key"].encode()).hexdigest(),
        "policy": scope["policy"],
        "policy_sha256": policy_sha256,
    }


def _signatures():
    from yoao_assets_images_runtime.health import _expected_signatures
    from yoao_assets_media.contract import OPERATIONS as media
    from yoao_assets_video_inputs.execution import OPERATIONS as video
    from yoao_assets_video_inputs.tracker import TRACKER_OPERATIONS as tracker

    media_worker = {
        "yoao_assets_v1." + name + "(" + ", ".join(op.sql_types) + ")"
        for name, op in media.items()
        if op.role == "worker"
    }
    media_worker.update(
        "yoao_assets_v1." + name + "(" + ", ".join(op.sql_types) + ")"
        for name, op in video.items()
        if op.role in {"worker", "system"}
    )
    media_worker.update(
        "yoao_assets_v1." + name + "(" + ", ".join(op.sql_types) + ")"
        for name, op in tracker.items()
        if op.namespace == "yoao_assets_v1" and op.role == "worker"
    )
    return {
        "control": tuple(_expected_signatures("control")),
        "media-worker": tuple(sorted(media_worker)),
        "image-worker": tuple(_expected_signatures("worker")),
    }


def _admin(environment, owner_url):
    admin = make_url(environment["MIGRATION_DATABASE_URL"])
    _require(
        admin.drivername == "postgresql+asyncpg"
        and not admin.query
        and (admin.host, admin.port or 5432, admin.database)
        == (owner_url.host, owner_url.port or 5432, owner_url.database),
        "fixture_native_business_database_changed",
    )
    return admin


def _backend(environment, resources):
    from app.config import Settings
    from worktree_resources import LocalResources

    fields = (
        "DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "MINIO_ENDPOINT",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
    )
    values = {key.lower(): environment[key] for key in fields if environment.get(key)}
    missing = [key for key in fields if key.lower() not in values]
    if missing:
        # The ordinary worktree launcher hands every exec child the isolated
        # database, endpoint and buckets but never exports the checkout's
        # MinIO endpoint credentials; the product API and workers load them
        # from .env themselves. This private supervisor is the only process
        # that needs them (to create the per-run scoped MinIO accounts), so it
        # reads the same checkout configuration instead of widening any child
        # environment or the Playwright/server topology.
        from app.config import settings as checkout

        _require(
            missing == ["MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"]
            and bool(checkout.minio_access_key)
            and bool(checkout.minio_secret_key),
            "fixture_native_business_configuration_missing",
        )
        values["minio_access_key"] = checkout.minio_access_key
        values["minio_secret_key"] = checkout.minio_secret_key
    manifest = _private_json(
        state_path(Path(resources["root"]), resources["mode"], "resources.json")
    )
    _require(
        isinstance(manifest.get("minio", {}).get("ssl"), bool),
        "fixture_native_business_configuration_missing",
    )
    values["minio_use_ssl"] = manifest["minio"]["ssl"]
    settings = Settings(_env_file=None, **values)
    return LocalResources(
        resources, settings, code_root=Path(__file__).resolve().parents[1]
    )


def _scopes(backend, private_dir):
    from verify_module_data_boundary import _cleanup_storage_scope

    resources = backend.resources
    for field in (
        "MINIO_BUCKET",
        "MINIO_DATASETS_BUCKET",
        "MINIO_MEDIA_CACHE_BUCKET",
        "MINIO_INPUTS_BUCKET",
    ):
        bucket = resources["buckets"][field]
        tags = backend.s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
        _require(
            any(
                tag["Key"] == "yoao-worktree-owner"
                and tag["Value"] == resources["owner"]
                for tag in tags
            ),
            "fixture_native_business_bucket_changed",
        )
    scopes = {}
    for kind in ("control", "media-worker", "image-worker"):
        scope = _cleanup_storage_scope(backend)
        scope["datasets_bucket"] = resources["buckets"]["MINIO_DATASETS_BUCKET"]
        scope["legacy_bucket"] = resources["buckets"]["MINIO_BUCKET"]
        token = secrets.token_hex(12)
        scope["policy"] = "aap-fixture-native-business-" + token
        scheme = "https" if backend.settings.minio_use_ssl else "http"
        scope["endpoint"] = scheme + "://" + backend.settings.minio_endpoint
        scope["host"] = (
            scheme
            + "://"
            + quote(backend.settings.minio_access_key, safe="")
            + ":"
            + quote(backend.settings.minio_secret_key, safe="")
            + "@"
            + backend.settings.minio_endpoint
        )
        scope["private_dir"] = str(private_dir)
        scope["config"] = str(private_dir / ("native-business-mc-" + token))
        scopes[kind] = scope
    _require(
        len({scope["container"] for scope in scopes.values()}) == 1,
        "fixture_native_business_minio_changed",
    )
    return scopes


def _raster_identity(path, resources, expected=None):
    from contextlib import closing
    from fixture_store import _client, _root_credentials
    from raster_exclusive_provider import _items
    from run_raster_fault_e2e import verify_store_physical
    from verify_storage_candidates import Owner

    owner = Owner(path)
    items = _items(owner, resources)
    verify_store_physical(owner)
    store = owner.data["raster"]
    observed = owner.verified(items["container"])
    state, config = observed.get("State") or {}, observed.get("Config") or {}
    birth = store["provider_birth"]
    access, secret = _root_credentials(owner)
    _require(
        birth.get("container_id") == items["container"]["id"]
        and type(birth.get("pid")) is int
        and birth["pid"] > 0
        and isinstance(birth.get("started_at"), str)
        and bool(birth["started_at"])
        and birth.get("pid") == state.get("Pid")
        and birth.get("started_at") == state.get("StartedAt")
        and config.get("Cmd") == ["server", "/data", "--console-address", ":9001"]
        and state.get("Running") is True
        and observed.get("RestartCount") == 0
        and [
            item
            for item in config.get("Env", [])
            if item.startswith("MINIO_ROOT_USER=")
        ]
        == ["MINIO_ROOT_USER=" + access]
        and [
            item
            for item in config.get("Env", [])
            if item.startswith("MINIO_ROOT_PASSWORD=")
        ]
        == ["MINIO_ROOT_PASSWORD=" + secret],
        "fixture_native_business_raster_birth_changed",
    )
    identity = {
        "raster_owner": str(owner.path),
        "nonce": owner.data["nonce"],
        "container": items["container"]["id"],
        "birth": birth,
        "endpoint": "http://" + store["endpoint"],
        "resources": resources,
    }
    if expected is not None:
        _require(
            all(expected.get(key) == value for key, value in identity.items()),
            "fixture_native_business_raster_identity_changed",
        )
    with closing(_client(identity["endpoint"], access, secret)) as client:
        for field in (
            "MINIO_BUCKET",
            "MINIO_DATASETS_BUCKET",
            "MINIO_MEDIA_CACHE_BUCKET",
            "MINIO_INPUTS_BUCKET",
        ):
            bucket = resources["buckets"][field]
            tags = client.get_bucket_tagging(Bucket=bucket)["TagSet"]
            _require(
                any(
                    tag["Key"] == "yoao-worktree-owner"
                    and tag["Value"] == resources["owner"]
                    for tag in tags
                ),
                "fixture_native_business_bucket_changed",
            )
        _require(
            client.get_bucket_versioning(
                Bucket=resources["buckets"]["MINIO_INPUTS_BUCKET"]
            ).get("Status")
            == "Enabled",
            "fixture_native_business_bucket_changed",
        )
    return owner, identity, access, secret


def _raster_scopes(path, resources):
    owner, identity, access, secret = _raster_identity(path, resources)
    scopes = {}
    for kind in ("control", "media-worker", "image-worker"):
        token = secrets.token_hex(12)
        scopes[kind] = {
            **identity,
            "provider": "raster",
            "bucket": resources["buckets"]["MINIO_MEDIA_CACHE_BUCKET"],
            "inputs_bucket": resources["buckets"]["MINIO_INPUTS_BUCKET"],
            "datasets_bucket": resources["buckets"]["MINIO_DATASETS_BUCKET"],
            "legacy_bucket": resources["buckets"]["MINIO_BUCKET"],
            "access_key": "aapcln" + token[:14],
            "secret_key": secrets.token_hex(24),
            "policy": "aap-fixture-native-business-" + token,
            "config": str(owner.private / ("native-business-mc-" + token)),
            "host": "http://"
            + quote(access, safe="")
            + ":"
            + quote(secret, safe="")
            + "@"
            + owner.data["raster"]["endpoint"],
        }
    return scopes


def _create_account(scope):
    from verify_module_data_boundary import _media_storage_policy

    _mc_output(
        scope,
        ["admin", "user", "add", "scoped"],
        payload=scope["access_key"] + "\n" + scope["secret_key"] + "\n",
    )
    _mc_output(
        scope,
        ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
        payload=json.dumps(_media_storage_policy(scope, worker=False)),
    )
    _mc_output(
        scope,
        [
            "admin",
            "policy",
            "attach",
            "scoped",
            scope["policy"],
            "--user",
            scope["access_key"],
        ],
    )


def _provider_identity(scope):
    keys = (
        "provider",
        "raster_owner",
        "nonce",
        "container",
        "birth",
        "endpoint",
        "resources",
    )
    return {key: scope[key] for key in keys if key in scope}


def _same_scope(scope, current):
    keys = (
        "provider",
        "raster_owner",
        "nonce",
        "container",
        "birth",
        "endpoint",
        "resources",
        "host",
        "private_dir",
        "bucket",
        "inputs_bucket",
        "datasets_bucket",
        "legacy_bucket",
    )
    return all(scope.get(key) == current.get(key) for key in keys)


async def _role_health(url, kind):
    import asyncpg
    from yoao_assets_images_runtime.health import check_capabilities as image_health
    from yoao_assets_media_runtime.health import check_capabilities as media_health

    pool = await asyncpg.create_pool(
        dsn=url.set(drivername="postgresql").render_as_string(hide_password=False),
        min_size=1,
        max_size=1,
    )
    try:
        check = media_health if kind == "media-worker" else image_health
        _require(
            await check(pool, "control" if kind == "control" else "worker"),
            "fixture_native_business_role_capabilities_changed",
        )
    finally:
        await pool.close()


async def _create_roles(admin, resources, identity, roles, owner, entry):
    signatures = _signatures()
    recorded = {}
    engine = create_async_engine(admin, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            observed = (
                await connection.execute(
                    text(
                        "SELECT oid,pg_catalog.shobj_description(oid,'pg_database') "
                        "FROM pg_catalog.pg_database WHERE datname=current_database()"
                    )
                )
            ).one()
            _require(
                tuple(observed) == (identity["database_oid"], resources["owner"]),
                "fixture_native_business_database_changed",
            )
            operator = (
                await connection.execute(
                    text(
                        "SELECT rolsuper,rolcreaterole FROM pg_catalog.pg_roles WHERE rolname=current_user"
                    )
                )
            ).one()
            _require(
                operator[0] or operator[1], "fixture_native_business_role_create_denied"
            )
            for role in roles.values():
                existing = await connection.scalar(
                    text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname=:name"),
                    {"name": role["name"]},
                )
                _require(existing is None, "fixture_native_business_role_exists")
            entry["roles_birth"] = "submitted"
            owner.save()
            quote = engine.dialect.identifier_preparer.quote_identifier
            for kind, role in roles.items():
                await connection.execute(
                    text(
                        "SELECT pg_catalog.set_config('aap.native_business_role',:name,true), "
                        "pg_catalog.set_config('aap.native_business_password',:password,true), "
                        "pg_catalog.set_config('aap.native_business_marker',:marker,true)"
                    ),
                    role,
                )
                await connection.exec_driver_sql("""DO $role$ BEGIN
                    EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', current_setting('aap.native_business_role'),current_setting('aap.native_business_password'));
                    EXECUTE format('COMMENT ON ROLE %I IS %L', current_setting('aap.native_business_role'),current_setting('aap.native_business_marker'));
                END; $role$;""")
                role_sql = quote(role["name"])
                await connection.exec_driver_sql(
                    f"GRANT CONNECT ON DATABASE {quote(resources['database'])} TO {role_sql}"
                )
                await connection.exec_driver_sql(
                    f"GRANT USAGE ON SCHEMA yoao_assets_v1 TO {role_sql}"
                )
                await connection.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION {', '.join(signatures[kind])} TO {role_sql}"
                )
                oid = await connection.scalar(
                    text("SELECT oid FROM pg_catalog.pg_roles WHERE rolname=:name"),
                    {"name": role["name"]},
                )
                _require(type(oid) is int, "fixture_native_business_role_unavailable")
                recorded[kind] = {
                    "name": role["name"],
                    "oid": oid,
                    "marker": role["marker"],
                }
    finally:
        await engine.dispose()
    entry["roles"] = recorded
    owner.save()


async def _ensure_roles(admin, owner_url, resources, identity, roles, owner, entry):
    engine = create_async_engine(admin, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            rows = {}
            for kind, role in roles.items():
                rows[kind] = (
                    await connection.execute(
                        text(
                            "SELECT oid,pg_catalog.shobj_description(oid,'pg_authid') "
                            "FROM pg_catalog.pg_roles WHERE rolname=:name"
                        ),
                        {"name": role["name"]},
                    )
                ).one_or_none()
    finally:
        await engine.dispose()
    present = {kind for kind, row in rows.items() if row is not None}
    if not present:
        _require(not entry["roles"], "fixture_native_business_role_outcome_unknown")
        await _create_roles(admin, resources, identity, roles, owner, entry)
        return
    _require(
        present == set(roles) and entry.get("roles_birth") == "submitted",
        "fixture_native_business_role_outcome_unknown",
    )
    observed = {}
    for kind, role in roles.items():
        row = rows[kind]
        _require(
            row[1] == role["marker"] and type(row[0]) is int,
            "fixture_native_business_role_changed",
        )
        observed[kind] = {"name": role["name"], "oid": row[0], "marker": role["marker"]}
        await _role_health(
            owner_url.set(username=role["name"], password=role["password"]), kind
        )
    _require(
        not entry["roles"] or entry["roles"] == observed,
        "fixture_native_business_role_changed",
    )
    if not entry["roles"]:
        entry["roles"] = observed
        owner.save()


async def prepare(
    environment,
    *,
    suite_id: uuid.UUID,
    raster_owner: Path | None = None,
) -> tuple[Path, dict]:
    """Create or verify the exact journaled suite identities."""
    result = await _prepare(
        environment,
        suite_id=suite_id,
        raster_owner=raster_owner,
    )
    if result is None:
        return await verified(environment, suite_id=suite_id, raster_owner=raster_owner)
    return result


async def _prepare(
    environment,
    *,
    suite_id: uuid.UUID,
    raster_owner: Path | None = None,
) -> tuple[Path, dict] | None:
    """Resume only exact, unpublished business-identity birth."""
    import fcntl
    from fixture_store import reader_credentials, verified_store
    from verify_storage_candidates import Owner, private_write
    from yoao_assets_media_runtime.additional_original import (
        load_additional_original_config,
    )

    _require(
        isinstance(suite_id, uuid.UUID),
        "fixture_native_business_suite_invalid",
    )
    owner_url, resources, identity = await _verified_database(environment)
    admin = _admin(environment, owner_url)
    root = _resource_root(resources)
    manifest = state_path(root, resources["mode"], "fixture-store-owner.json")
    lock_path = state_path(root, resources["mode"], "fixture-store.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    backend = None
    try:
        info, current = os.fstat(descriptor), lock_path.lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "fixture_lock_invalid",
        )
        await _settled_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        current = lock_path.lstat()
        _require(
            (info.st_dev, info.st_ino, info.st_nlink)
            == (current.st_dev, current.st_ino, current.st_nlink),
            "fixture_lock_changed",
        )
        engine = create_async_engine(
            owner_url, poolclass=NullPool, hide_parameters=True
        )
        try:
            async with engine.connect() as connection:
                opened = await connection.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM yoao_fixture_v1.fixture_runs "
                        "WHERE id=:suite AND parent_run_id IS NULL AND scope_key IS NULL "
                        "AND family='owned' AND state='open')"
                    ),
                    {"suite": suite_id},
                )
            _require(opened is True, "fixture_native_business_suite_not_open")
        finally:
            await engine.dispose()
        owner = Owner(manifest)
        store, _ = await _settled_thread(verified_store, owner, identity)
        reader = reader_credentials(owner, store)
        configured = load_additional_original_config(environment)
        _require(
            configured is not None
            and configured.endpoint == store["endpoint"]
            and configured.bucket == store["bucket"]
            and configured.access_key == reader["access_key"]
            and configured.secret_key == reader["secret_key"],
            "fixture_native_business_original_reader_changed",
        )
        profile = next(
            (
                path
                for path in (
                    Path("/usr/share/color/icc/sRGB.icc"),
                    Path("/usr/share/color/icc/sRGB.icm"),
                    Path("/usr/share/color/icc/colord/sRGB.icc"),
                )
                if path.is_file()
            ),
            None,
        )
        _require(profile is not None, "fixture_native_business_srgb_profile_missing")
        entries = owner.data.setdefault("fixture_native_business", {})
        _require(isinstance(entries, dict), "fixture_native_business_receipt_missing")
        entry = entries.get(suite_id.hex)
        if entry is not None:
            _require(
                isinstance(entry, dict)
                and entry.get("suite_id") == str(suite_id)
                and entry.get("database") == identity
                and entry.get("state") in {"creating", "ready"},
                "fixture_native_business_receipt_changed",
            )
            if entry["state"] == "ready":
                return None
        if raster_owner is None:
            backend = _backend(environment, resources)
            current_scopes = await _settled_thread(_scopes, backend, owner.private)
        else:
            current_scopes = await _settled_thread(
                _raster_scopes, raster_owner, resources
            )
        origin = current_scopes["control"]["endpoint"]
        seed_path = owner.private / ("native-business-" + suite_id.hex + ".json")
        profile_path = owner.private / (
            "native-business-" + suite_id.hex + ".profiles.json"
        )
        if entry is None:
            roles = {
                kind: {
                    "name": prefix + suite_id.hex,
                    "password": secrets.token_urlsafe(48),
                    "marker": f"{resources['owner']}:fixture-native-business:{kind}:{suite_id}",
                }
                for kind, prefix in (
                    ("control", "aap_fixture_ctl_"),
                    ("media-worker", "aap_fixture_vid_"),
                    ("image-worker", "aap_fixture_img_"),
                )
            }
            scopes = current_scopes
            seed_bytes = json.dumps({"roles": roles, "scopes": scopes}).encode()
            private_write(seed_path, seed_bytes)
            entry = {
                "state": "creating",
                "suite_id": str(suite_id),
                "database": identity,
                "roles": {},
                "accounts": {},
                "iam_birth": {},
                "seed_file": str(seed_path),
                "seed_sha256": hashlib.sha256(seed_bytes).hexdigest(),
                "profile_file": str(profile_path),
                "storage_provider": _provider_identity(scopes["control"]),
            }
            entries[suite_id.hex] = entry
            owner.save()
        else:
            _require(
                entry.get("seed_file") == str(seed_path)
                and entry.get("profile_file") == str(profile_path),
                "fixture_native_business_receipt_changed",
            )
            seed_bytes = _private_bytes(seed_path)
            seed = json.loads(seed_bytes)
            _require(
                hashlib.sha256(seed_bytes).hexdigest() == entry.get("seed_sha256")
                and isinstance(seed, dict)
                and set(seed) == {"roles", "scopes"}
                and isinstance(entry.get("roles"), dict)
                and isinstance(entry.get("accounts"), dict)
                and isinstance(entry.get("iam_birth"), dict)
                and set(seed["roles"])
                == set(seed["scopes"])
                == {"control", "media-worker", "image-worker"},
                "fixture_native_business_receipt_changed",
            )
            roles, scopes = seed["roles"], seed["scopes"]
            _require(
                entry.get("storage_provider")
                == _provider_identity(current_scopes["control"])
                and all(
                    _same_scope(scopes[kind], current_scopes[kind]) for kind in scopes
                ),
                "fixture_native_business_minio_changed",
            )
        await _settled(
            asyncio.create_task(
                _ensure_roles(
                    admin, owner_url, resources, identity, roles, owner, entry
                )
            )
        )
        for kind, scope in scopes.items():
            recorded = entry["accounts"].get(kind)
            if recorded is None:
                user, policy = await _settled_thread(_iam_components, scope)
                if user is None and policy is None:
                    entry["iam_birth"][kind] = "submitted"
                    owner.save()
                    await _settled_thread(_create_account, scope)
                else:
                    _require(
                        user is not None
                        and policy is not None
                        and entry["iam_birth"].get(kind) == "submitted",
                        "fixture_native_business_iam_outcome_unknown",
                    )
                entry["accounts"][kind] = await _settled_thread(_verify_account, scope)
                owner.save()
            else:
                _require(
                    await _settled_thread(_verify_account, scope) == recorded,
                    "fixture_native_business_iam_changed",
                )
        for kind, role in roles.items():
            await _settled(
                asyncio.create_task(
                    _role_health(
                        owner_url.set(username=role["name"], password=role["password"]),
                        kind,
                    )
                )
            )
        original = {
            "AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT": configured.endpoint,
            "AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET": configured.bucket,
            "AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY": configured.access_key,
            "AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY": configured.secret_key,
        }
        profiles = {}
        for kind, role in roles.items():
            scope = scopes[kind]
            values = {
                "ASSETS_MEDIA_S3_ENDPOINT": origin,
                "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT": origin,
                "ASSETS_MEDIA_S3_REGION": "us-east-1",
                "ASSETS_MEDIA_S3_ACCESS_KEY": scope["access_key"],
                "ASSETS_MEDIA_S3_SECRET_KEY": scope["secret_key"],
                "ASSETS_MEDIA_DATASETS_BUCKET": resources["buckets"][
                    "MINIO_DATASETS_BUCKET"
                ],
                "ASSETS_MEDIA_CACHE_BUCKET": resources["buckets"][
                    "MINIO_MEDIA_CACHE_BUCKET"
                ],
                "ASSETS_MEDIA_INPUTS_BUCKET": resources["buckets"][
                    "MINIO_INPUTS_BUCKET"
                ],
                "ASSETS_IMAGES_LEGACY_BUCKET": resources["buckets"]["MINIO_BUCKET"],
                **original,
            }
            dsn = owner_url.set(
                drivername="postgresql",
                username=role["name"],
                password=role["password"],
            ).render_as_string(hide_password=False)
            values[
                {
                    "control": "ASSETS_MEDIA_CONTROL_DATABASE_URL",
                    "media-worker": "ASSETS_MEDIA_WORKER_DATABASE_URL",
                    "image-worker": "ASSETS_IMAGES_DATABASE_URL",
                }[kind]
            ] = dsn
            if kind == "image-worker":
                values["ASSETS_IMAGES_SRGB_PROFILE"] = str(profile)
            profiles[kind] = values
        document = {
            "schema": "aap-fixture-native-business/v1",
            "suite_id": str(suite_id),
            "database_oid": identity["database_oid"],
            "database_owner_marker": resources["owner"],
            "profiles": profiles,
        }
        profile_bytes = json.dumps(document).encode()
        if os.path.lexists(profile_path):
            _require(
                entry.get("profile_birth") == "submitted",
                "fixture_native_business_profile_changed",
            )
        else:
            entry["profile_birth"] = "submitted"
            owner.save()
            private_write(profile_path, profile_bytes)
        persisted = _private_bytes(profile_path)
        _require(persisted == profile_bytes, "fixture_native_business_profile_changed")
        document = json.loads(persisted)
        entry["profile_sha256"] = hashlib.sha256(profile_bytes).hexdigest()
        entry["state"] = "ready"
        owner.save()
        return profile_path, document
    finally:
        try:
            if backend is not None:
                backend.s3.close()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


async def verified(
    environment, *, suite_id: uuid.UUID, raster_owner: Path | None = None
) -> tuple[Path, dict]:
    """Recheck the exact published business profiles without creating identities."""
    import fcntl
    from fixture_store import reader_credentials, verified_store
    from verify_storage_candidates import Owner

    _require(isinstance(suite_id, uuid.UUID), "fixture_native_business_suite_invalid")
    owner_url, resources, identity = await _verified_database(environment)
    root = _resource_root(resources)
    manifest = state_path(root, resources["mode"], "fixture-store-owner.json")
    lock_path = state_path(root, resources["mode"], "fixture-store.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    backend = None
    try:
        info, current = os.fstat(descriptor), lock_path.lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "fixture_lock_invalid",
        )
        await _settled_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        current = lock_path.lstat()
        _require(
            (info.st_dev, info.st_ino, info.st_nlink)
            == (current.st_dev, current.st_ino, current.st_nlink),
            "fixture_lock_changed",
        )
        owner = Owner(manifest)
        store, _ = await _settled_thread(verified_store, owner, identity)
        original_reader = reader_credentials(owner, store)
        entries = owner.data.get("fixture_native_business")
        _require(isinstance(entries, dict), "fixture_native_business_receipt_missing")
        entry = entries.get(suite_id.hex)
        _require(
            isinstance(entry, dict)
            and entry.get("state") == "ready"
            and entry.get("suite_id") == str(suite_id)
            and entry.get("database") == identity,
            "fixture_native_business_receipt_changed",
        )
        seed_path = owner.private / ("native-business-" + suite_id.hex + ".json")
        profile_path = owner.private / (
            "native-business-" + suite_id.hex + ".profiles.json"
        )
        _require(
            entry.get("seed_file") == str(seed_path)
            and entry.get("profile_file") == str(profile_path),
            "fixture_native_business_receipt_changed",
        )
        seed_bytes, profile_bytes = (
            _private_bytes(seed_path),
            _private_bytes(profile_path),
        )
        seed = json.loads(seed_bytes)
        profile = json.loads(profile_bytes)
        _require(
            isinstance(seed, dict)
            and set(seed) == {"roles", "scopes"}
            and hashlib.sha256(seed_bytes).hexdigest() == entry.get("seed_sha256")
            and isinstance(profile, dict)
            and profile.get("schema") == "aap-fixture-native-business/v1"
            and profile.get("suite_id") == str(suite_id)
            and profile.get("database_oid") == identity["database_oid"]
            and profile.get("database_owner_marker") == resources["owner"]
            and isinstance(profile.get("profiles"), dict)
            and set(profile["profiles"]) == {"control", "media-worker", "image-worker"}
            and hashlib.sha256(profile_bytes).hexdigest() == entry.get("profile_sha256")
            and set(seed["roles"])
            == set(seed["scopes"])
            == set(entry["roles"])
            == set(entry["accounts"])
            == {"control", "media-worker", "image-worker"},
            "fixture_native_business_receipt_changed",
        )
        if raster_owner is None:
            backend = _backend(environment, resources)
            current_scopes = await _settled_thread(_scopes, backend, owner.private)
        else:
            current_scopes = await _settled_thread(
                _raster_scopes, raster_owner, resources
            )
        _require(
            entry.get("storage_provider")
            == _provider_identity(current_scopes["control"]),
            "fixture_native_business_minio_changed",
        )
        engine = create_async_engine(
            owner_url, poolclass=NullPool, hide_parameters=True
        )
        try:
            async with engine.connect() as connection:
                for kind, scope in seed["scopes"].items():
                    _require(
                        _same_scope(scope, current_scopes[kind]),
                        "fixture_native_business_minio_changed",
                    )
                    _require(
                        await _settled_thread(_verify_account, scope)
                        == entry["accounts"][kind],
                        "fixture_native_business_iam_changed",
                    )
                    role = seed["roles"][kind]
                    values = profile["profiles"][kind]
                    dsn_field = {
                        "control": "ASSETS_MEDIA_CONTROL_DATABASE_URL",
                        "media-worker": "ASSETS_MEDIA_WORKER_DATABASE_URL",
                        "image-worker": "ASSETS_IMAGES_DATABASE_URL",
                    }[kind]
                    expected_dsn = owner_url.set(
                        drivername="postgresql",
                        username=role["name"],
                        password=role["password"],
                    ).render_as_string(hide_password=False)
                    _require(
                        isinstance(values, dict)
                        and values.get("ASSETS_MEDIA_S3_ACCESS_KEY")
                        == scope["access_key"]
                        and values.get("ASSETS_MEDIA_S3_SECRET_KEY")
                        == scope["secret_key"]
                        and values.get("ASSETS_MEDIA_S3_ENDPOINT") == scope["endpoint"]
                        and values.get("AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT")
                        == store["endpoint"]
                        and values.get("AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET")
                        == store["bucket"]
                        and values.get("AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY")
                        == original_reader["access_key"]
                        and values.get("AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY")
                        == original_reader["secret_key"]
                        and values.get(dsn_field) == expected_dsn,
                        "fixture_native_business_profile_changed",
                    )
                    row = (
                        await connection.execute(
                            text(
                                "SELECT oid,pg_catalog.shobj_description(oid,'pg_authid') "
                                "FROM pg_catalog.pg_roles WHERE rolname=:name"
                            ),
                            {"name": role["name"]},
                        )
                    ).one_or_none()
                    _require(
                        row is not None
                        and row[0] == entry["roles"][kind]["oid"]
                        and row[1] == role["marker"] == entry["roles"][kind]["marker"]
                        and role["name"] == entry["roles"][kind]["name"],
                        "fixture_native_business_role_changed",
                    )
                    await _settled(
                        asyncio.create_task(
                            _role_health(
                                owner_url.set(
                                    username=role["name"], password=role["password"]
                                ),
                                kind,
                            )
                        )
                    )
        finally:
            await engine.dispose()
        return profile_path, profile
    finally:
        try:
            if backend is not None:
                backend.s3.close()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _business_run_settled(state, run_id, execution, reader, target):
    """One child's media evidence settles without inventing native participants."""
    from fixture_native_playwright import _native_run_evidence_settled

    if state != "retired" or not _native_run_evidence_settled(
        execution, reader, target is not None
    ):
        return False
    if execution is None and reader is None:
        return True
    return (
        isinstance(target, dict)
        and target.get("run_id") == str(run_id)
        and target.get("state") == "retired"
    )


async def _require_native_retired(owner_url, suite_id, targets):
    engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT id,parent_run_id,state,family FROM yoao_fixture_v1.fixture_runs WHERE id=:suite OR parent_run_id=:suite"
                    ),
                    {"suite": suite_id},
                )
            ).all()
            root = next((row for row in rows if row.id == suite_id), None)
            _require(
                root is not None
                and root.parent_run_id is None
                and root.state == "retired"
                and root.family in {"owned", "screenshot"},
                "fixture_native_business_suite_unsettled",
            )
            if root.family == "screenshot":
                _require(len(rows) == 1, "fixture_native_business_run_unsettled")
            native_rows = (
                rows
                if root.family == "screenshot"
                else [row for row in rows if row.id != suite_id]
            )
            for row in native_rows:
                executor = await connection.scalar(
                    text("SELECT yoao_jobs_v1.fixture_media_executor_identity(:run)"),
                    {"run": row.id},
                )
                reader = await connection.scalar(
                    text("SELECT yoao_assets_v1.fixture_native_reader_identity(:run)"),
                    {"run": row.id},
                )
                target = targets.get(row.id.hex)
                # A child that registered no native participant never used the
                # suite's media identities; the same durable-identity rule as
                # suite settlement decides whether its own evidence is settled.
                _require(
                    _business_run_settled(row.state, row.id, executor, reader, target),
                    "fixture_native_business_run_unsettled",
                )
    finally:
        await engine.dispose()


async def _retire_roles(admin, resources, identity, roles, recorded):
    signatures = _signatures()
    engine = create_async_engine(admin, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            observed = (
                await connection.execute(
                    text(
                        "SELECT oid,pg_catalog.shobj_description(oid,'pg_database') "
                        "FROM pg_catalog.pg_database WHERE datname=current_database()"
                    )
                )
            ).one()
            _require(
                tuple(observed) == (identity["database_oid"], resources["owner"]),
                "fixture_native_business_database_changed",
            )
            quote = engine.dialect.identifier_preparer.quote_identifier
            for kind, role in roles.items():
                await connection.execute(
                    text(
                        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:role,0))"
                    ),
                    {"role": role["name"]},
                )
                row = (
                    (
                        await connection.execute(
                            text(
                                "SELECT oid,pg_catalog.shobj_description(oid,'pg_authid') AS marker, "
                                "rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,rolinherit "
                                "FROM pg_catalog.pg_roles WHERE rolname=:name"
                            ),
                            {"name": role["name"]},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                _require(
                    row is not None
                    and row["oid"] == recorded[kind]["oid"]
                    and row["marker"] == role["marker"] == recorded[kind]["marker"]
                    and row["rolcanlogin"]
                    and not any(
                        row[name]
                        for name in (
                            "rolsuper",
                            "rolcreaterole",
                            "rolcreatedb",
                            "rolreplication",
                            "rolbypassrls",
                            "rolinherit",
                        )
                    ),
                    "fixture_native_business_role_changed",
                )
                _require(
                    not await connection.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity WHERE usesysid=:role)"
                        ),
                        {"role": row["oid"]},
                    ),
                    "fixture_native_business_role_sessions_remain",
                )
                role_sql = quote(role["name"])
                await connection.exec_driver_sql(
                    f"REVOKE EXECUTE ON FUNCTION {', '.join(signatures[kind])} FROM {role_sql}"
                )
                await connection.exec_driver_sql(
                    f"REVOKE USAGE ON SCHEMA yoao_assets_v1 FROM {role_sql}"
                )
                await connection.exec_driver_sql(
                    f"REVOKE CONNECT ON DATABASE {quote(resources['database'])} FROM {role_sql}"
                )
                await connection.exec_driver_sql(f"DROP ROLE {role_sql}")
                _require(
                    not await connection.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE oid=:oid)"
                        ),
                        {"oid": row["oid"]},
                    ),
                    "fixture_native_business_role_drop_unconfirmed",
                )
    finally:
        await engine.dispose()


async def _roles_present(admin, resources, identity, roles, recorded):
    engine = create_async_engine(admin, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            observed = (
                await connection.execute(
                    text(
                        "SELECT oid,pg_catalog.shobj_description(oid,'pg_database') "
                        "FROM pg_catalog.pg_database WHERE datname=current_database()"
                    )
                )
            ).one()
            _require(
                tuple(observed) == (identity["database_oid"], resources["owner"]),
                "fixture_native_business_database_changed",
            )
            present = []
            for kind, role in roles.items():
                evidence = recorded[kind]
                rows = (
                    await connection.execute(
                        text(
                            "SELECT oid,rolname,pg_catalog.shobj_description(oid,'pg_authid') "
                            "FROM pg_catalog.pg_roles WHERE rolname=:name OR oid=:oid"
                        ),
                        {"name": role["name"], "oid": evidence["oid"]},
                    )
                ).all()
                if rows:
                    _require(
                        len(rows) == 1
                        and rows[0][0] == evidence["oid"]
                        and rows[0][1] == role["name"] == evidence["name"]
                        and rows[0][2] == role["marker"] == evidence["marker"],
                        "fixture_native_business_role_changed",
                    )
                    present.append(kind)
            _require(
                not present or set(present) == set(roles),
                "fixture_native_business_role_outcome_unknown",
            )
            return bool(present)
    finally:
        await engine.dispose()


def _retire_account(scope, evidence, progress, owner):
    user, policy = _iam_components(scope)
    if user is not None:
        _require(
            not progress.get("user_absent") and policy is not None,
            "fixture_native_business_iam_changed",
        )
        _require(
            _verify_account(scope) == evidence, "fixture_native_business_iam_changed"
        )
        progress["user_submitted"] = True
        owner.save()
        _mc_output(scope, ["admin", "user", "remove", "scoped", scope["access_key"]])
        user, policy = _iam_components(scope)
    _require(
        user is None and progress.get("user_submitted") is True,
        "fixture_native_business_iam_outcome_unknown",
    )
    progress["user_absent"] = True
    owner.save()
    if policy is not None:
        _require(
            not progress.get("policy_absent")
            and _verify_policy(scope, policy) == evidence["policy_sha256"],
            "fixture_native_business_iam_changed",
        )
        progress["policy_submitted"] = True
        owner.save()
        _mc_output(scope, ["admin", "policy", "remove", "scoped", scope["policy"]])
        user, policy = _iam_components(scope)
    _require(
        user is None and policy is None and progress.get("policy_submitted") is True,
        "fixture_native_business_iam_outcome_unknown",
    )
    progress["policy_absent"] = True
    owner.save()


async def retire(
    environment, *, suite_id: uuid.UUID, raster_owner: Path | None = None
) -> None:
    """Retire exact business identities only after native run and process proof."""
    import fcntl
    from fixture_store import verified_store
    from verify_storage_candidates import Owner

    _require(isinstance(suite_id, uuid.UUID), "fixture_native_business_suite_invalid")
    owner_url, resources, identity = await _verified_database(environment)
    admin = _admin(environment, owner_url)
    root = _resource_root(resources)
    manifest = state_path(root, resources["mode"], "fixture-store-owner.json")
    lock_path = state_path(root, resources["mode"], "fixture-store.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    backend = None
    try:
        info, current = os.fstat(descriptor), lock_path.lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino),
            "fixture_lock_invalid",
        )
        await _settled_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        current = lock_path.lstat()
        _require(
            (info.st_dev, info.st_ino, info.st_nlink)
            == (current.st_dev, current.st_ino, current.st_nlink),
            "fixture_lock_changed",
        )
        owner = Owner(manifest)
        await _settled_thread(verified_store, owner, identity)
        entries = owner.data.get("fixture_native_business")
        _require(isinstance(entries, dict), "fixture_native_business_receipt_missing")
        entry = entries.get(suite_id.hex)
        _require(
            isinstance(entry, dict)
            and entry.get("state") in {"ready", "retiring", "retired"}
            and entry.get("suite_id") == str(suite_id)
            and entry.get("database") == identity,
            "fixture_native_business_receipt_changed",
        )
        targets = owner.data.get("fixture_native_outputs", {})
        _require(isinstance(targets, dict), "fixture_native_targets_invalid")
        await _settled(
            asyncio.create_task(_require_native_retired(owner_url, suite_id, targets))
        )
        seed_path = owner.private / ("native-business-" + suite_id.hex + ".json")
        profile_path = owner.private / (
            "native-business-" + suite_id.hex + ".profiles.json"
        )
        _require(
            entry.get("seed_file") == str(seed_path)
            and entry.get("profile_file") == str(profile_path),
            "fixture_native_business_receipt_changed",
        )
        seed_bytes, profile_bytes = (
            _private_bytes(seed_path),
            _private_bytes(profile_path),
        )
        seed = json.loads(seed_bytes)
        profile = json.loads(profile_bytes)
        _require(
            isinstance(seed, dict)
            and set(seed) == {"roles", "scopes"}
            and hashlib.sha256(seed_bytes).hexdigest() == entry.get("seed_sha256")
            and isinstance(profile, dict)
            and profile.get("schema") == "aap-fixture-native-business/v1"
            and profile.get("suite_id") == str(suite_id)
            and hashlib.sha256(profile_bytes).hexdigest() == entry.get("profile_sha256")
            and set(seed["roles"])
            == set(seed["scopes"])
            == set(entry["roles"])
            == set(entry["accounts"])
            == {"control", "media-worker", "image-worker"},
            "fixture_native_business_receipt_changed",
        )
        if raster_owner is None:
            backend = _backend(environment, resources)
            current_scopes = await _settled_thread(_scopes, backend, owner.private)
        else:
            current_scopes = await _settled_thread(
                _raster_scopes, raster_owner, resources
            )
        _require(
            entry.get("storage_provider")
            == _provider_identity(current_scopes["control"]),
            "fixture_native_business_minio_changed",
        )
        for kind, scope in seed["scopes"].items():
            _require(
                _same_scope(scope, current_scopes[kind]),
                "fixture_native_business_minio_changed",
            )
            evidence = entry["accounts"][kind]
            _require(
                evidence.get("identity_sha256")
                == hashlib.sha256(scope["access_key"].encode()).hexdigest()
                and evidence.get("policy") == scope["policy"],
                "fixture_native_business_iam_changed",
            )
            role = seed["roles"][kind]
            _require(
                role["name"] == entry["roles"][kind]["name"]
                and role["marker"] == entry["roles"][kind]["marker"],
                "fixture_native_business_role_changed",
            )
        roles_present = await _roles_present(
            admin, resources, identity, seed["roles"], entry["roles"]
        )
        if entry["state"] == "ready":
            _require(roles_present, "fixture_native_business_role_outcome_unknown")
            for kind, scope in seed["scopes"].items():
                _require(
                    await _settled_thread(_verify_account, scope)
                    == entry["accounts"][kind],
                    "fixture_native_business_iam_changed",
                )
                role = seed["roles"][kind]
                await _settled(
                    asyncio.create_task(
                        _role_health(
                            owner_url.set(
                                username=role["name"], password=role["password"]
                            ),
                            kind,
                        )
                    )
                )
            entry["state"] = "retiring"
            entry["roles_retire_submitted"] = True
            entry["iam_retirement"] = {}
            owner.save()
        else:
            _require(
                entry.get("roles_retire_submitted") is True,
                "fixture_native_business_role_outcome_unknown",
            )
        if roles_present:
            _require(
                not entry.get("roles_retired"), "fixture_native_business_role_changed"
            )
            await _settled(
                asyncio.create_task(
                    _retire_roles(
                        admin, resources, identity, seed["roles"], entry["roles"]
                    )
                )
            )
            _require(
                not await _roles_present(
                    admin, resources, identity, seed["roles"], entry["roles"]
                ),
                "fixture_native_business_role_drop_unconfirmed",
            )
        entry["roles_retired"] = True
        owner.save()
        progress_by_kind = entry.get("iam_retirement")
        _require(
            isinstance(progress_by_kind, dict),
            "fixture_native_business_receipt_changed",
        )
        for kind, scope in seed["scopes"].items():
            evidence = entry["accounts"][kind]
            if evidence.get("retired") is True:
                progress = progress_by_kind.get(kind)
                _require(
                    isinstance(progress, dict)
                    and progress.get("user_submitted") is True
                    and progress.get("user_absent") is True
                    and progress.get("policy_submitted") is True
                    and progress.get("policy_absent") is True,
                    "fixture_native_business_iam_outcome_unknown",
                )
                await _settled_thread(_assert_iam_absent, scope)
            else:
                progress = progress_by_kind.setdefault(kind, {})
                _require(
                    isinstance(progress, dict),
                    "fixture_native_business_iam_outcome_unknown",
                )
                await _settled_thread(_retire_account, scope, evidence, progress, owner)
            current_scopes = (
                await _settled_thread(_scopes, backend, owner.private)
                if raster_owner is None
                else await _settled_thread(_raster_scopes, raster_owner, resources)
            )
            _require(
                _same_scope(scope, current_scopes[kind]),
                "fixture_native_business_minio_changed",
            )
            entry["accounts"][kind]["retired"] = True
            owner.save()
        entry["state"] = "retired"
        owner.save()
    finally:
        try:
            if backend is not None:
                backend.s3.close()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
