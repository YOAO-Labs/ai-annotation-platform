"""Own the canonical Raster run's restricted native Images executor."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import select
import signal
import stat
import subprocess
import sys
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from linux_pidfd import open_pidfd, pidfd_alive, pidfd_send_signal, waitid_pidfd
from raster_consumer_process import _drain_consumer, _track_consumer_descendants
from storage_s3_contract import ContractError, require
from verify_storage_candidates import private_write


_UNSETTLED = []


def _credentials(owner):
    path = owner.private / "images.json"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600,
            "raster_images_credentials_unsafe",
        )
        credentials = json.load(stream)
    require(
        isinstance(credentials, dict)
        and set(credentials)
        == {"database_role", "database_password", "access_key", "secret_key"}
        and credentials["database_role"] == "aap_raster_images_" + owner.data["nonce"]
        and credentials["database_role"]
        == owner.data["raster"]["images"]["database_role"]
        and credentials["access_key"] == owner.data["raster"]["images"]["s3_user"]
        and all(isinstance(value, str) and value for value in credentials.values()),
        "raster_images_credentials_changed",
    )
    return credentials


async def image_role(backend, owner, action):
    """Manage only the recorded function-only LOGIN and its exact OID."""
    from yoao_assets_images_runtime.health import _expected_signatures

    credentials = _credentials(owner)
    role = credentials["database_role"]
    require(action in {"create", "check", "drop"}, "raster_images_role_action_invalid")
    if action in {"create", "check"}:
        database = await backend.database()
        require(
            database is not None
            and database.get("owner") == backend.resources["owner"],
            "raster_images_database_owner_changed",
        )
    target = backend.url.set(database="postgres") if action == "drop" else backend.url
    engine = create_async_engine(target, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            if action in {"create", "check"}:
                db_owner = await connection.scalar(
                    text(
                        "SELECT shobj_description(oid,'pg_database') FROM pg_database "
                        "WHERE datname=current_database()"
                    )
                )
                require(
                    db_owner == backend.resources["owner"],
                    "raster_images_database_owner_changed",
                )
            else:
                await backend.verify_database_server(connection)
                present = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=:name)"
                    ),
                    {"name": backend.resources["database"]},
                )
                require(not present, "raster_images_database_still_exists")
            operator = (
                (
                    await connection.execute(
                        text(
                            "SELECT oid,rolsuper,rolcreaterole FROM pg_roles "
                            "WHERE rolname=current_user"
                        )
                    )
                )
                .mappings()
                .one()
            )
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:role))"), {"role": role}
            )
            observed = await backend._runtime_role_record(
                connection, operator["oid"], role=role
            )
            quote = engine.dialect.identifier_preparer.quote_identifier
            if action == "create":
                require(observed is None, "raster_images_role_already_exists")
                require(
                    operator["rolsuper"] or operator["rolcreaterole"],
                    "raster_images_role_create_denied",
                )
                await connection.execute(
                    text(
                        "SELECT set_config('aap.fixture_role',:role,true), "
                        "set_config('aap.fixture_password',:password,true), "
                        "set_config('aap.fixture_owner',:owner,true)"
                    ),
                    {
                        "role": role,
                        "password": credentials["database_password"],
                        "owner": backend.resources["owner"],
                    },
                )
                await connection.exec_driver_sql("""DO $role$ BEGIN
                    EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', current_setting('aap.fixture_role'), current_setting('aap.fixture_password'));
                    EXECUTE format('COMMENT ON ROLE %I IS %L', current_setting('aap.fixture_role'), current_setting('aap.fixture_owner'));
                    END; $role$;""")
                await connection.exec_driver_sql(
                    f"GRANT CONNECT ON DATABASE {quote(backend.resources['database'])} "
                    f"TO {quote(role)}"
                )
                await connection.exec_driver_sql(
                    f"GRANT USAGE ON SCHEMA yoao_assets_v1 TO {quote(role)}"
                )
                signatures = ", ".join(_expected_signatures("worker"))
                await connection.exec_driver_sql(
                    f"GRANT EXECUTE ON FUNCTION {signatures} TO {quote(role)}"
                )
                observed = await backend._runtime_role_record(
                    connection, operator["oid"], role=role
                )
                require(observed is not None, "raster_images_role_missing")
                original_oid = observed["oid"]
                backend._validate_runtime_role(observed, credentials)
                owner.data["raster"]["images"]["database_role_oid"] = original_oid
                owner.save()
            else:
                require(observed is not None, "raster_images_role_missing")
                backend._validate_runtime_role(observed, credentials)
                require(
                    observed["oid"]
                    == owner.data["raster"]["images"].get("database_role_oid"),
                    "raster_images_role_oid_changed",
                )
                require(not observed["sessions"], "raster_images_role_sessions_remain")
                require(
                    operator["rolsuper"]
                    or (operator["rolcreaterole"] and observed["operator_admin"]),
                    "raster_images_role_drop_denied",
                )
                if action == "drop":
                    await connection.exec_driver_sql(f"DROP ROLE {quote(role)}")
                    require(
                        not await connection.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE oid=:oid)"
                            ),
                            {"oid": observed["oid"]},
                        ),
                        "raster_images_role_drop_unconfirmed",
                    )
    finally:
        await engine.dispose()
    if action == "create":
        return original_oid


def make_credentials(owner):
    credentials = {
        "database_role": "aap_raster_images_" + owner.data["nonce"],
        "database_password": secrets.token_urlsafe(32),
        "access_key": "images" + secrets.token_hex(12),
        "secret_key": secrets.token_urlsafe(32),
    }
    private_write(owner.private / "images.json", json.dumps(credentials).encode())
    return credentials


def image_environment(owner, environment):
    from sqlalchemy.engine import make_url

    credentials = _credentials(owner)
    buckets = owner.data["raster"]["resources"]["buckets"]
    db = make_url(environment["DATABASE_URL"]).set(
        drivername="postgresql",
        username=credentials["database_role"],
        password=credentials["database_password"],
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
    require(profile is not None, "raster_images_srgb_profile_missing")
    original_keys = tuple(
        "AAP_ASSETS_ADDITIONAL_ORIGINAL_" + suffix
        for suffix in ("ENDPOINT", "BUCKET", "ACCESS_KEY", "SECRET_KEY")
    )
    require(
        all(key in environment for key in original_keys),
        "raster_images_original_reader_missing",
    )
    result = {
        key: environment[key]
        for key in (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "VIRTUAL_ENV",
            "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH",
            "DYLD_FALLBACK_LIBRARY_PATH",
            *original_keys,
        )
        if key in environment
    }
    endpoint = "http://" + owner.data["raster"]["endpoint"]
    result.update(
        {
            "ASSETS_IMAGES_DATABASE_URL": db.render_as_string(hide_password=False),
            "ASSETS_IMAGES_LEGACY_BUCKET": buckets["MINIO_BUCKET"],
            "ASSETS_IMAGES_POLL_SECONDS": "0.1",
            "ASSETS_IMAGES_SRGB_PROFILE": str(profile),
            "ASSETS_MEDIA_S3_ENDPOINT": endpoint,
            "ASSETS_MEDIA_S3_PUBLIC_ENDPOINT": endpoint,
            "ASSETS_MEDIA_S3_REGION": "us-east-1",
            "ASSETS_MEDIA_S3_ACCESS_KEY": credentials["access_key"],
            "ASSETS_MEDIA_S3_SECRET_KEY": credentials["secret_key"],
            "ASSETS_MEDIA_DATASETS_BUCKET": buckets["MINIO_DATASETS_BUCKET"],
            "ASSETS_MEDIA_CACHE_BUCKET": buckets["MINIO_MEDIA_CACHE_BUCKET"],
            "ASSETS_MEDIA_INPUTS_BUCKET": buckets["MINIO_INPUTS_BUCKET"],
        }
    )
    return result


class RasterImagesProcess:
    """Observe the real worker and its detached processing descendants."""

    def __init__(self, environment, *, root, journal):
        self.environment, self.root, self.journal = environment, root, journal
        self.child = None
        self.descriptor = None
        self.tracked = {}
        self.ready_fd = None
        self.settled_fd = None

    def start(self):
        self.journal("starting", {})
        self.ready_fd, writer = os.pipe()
        self.settled_fd, settled_writer = os.pipe()
        try:
            self.child = subprocess.Popen(
                [
                    str(self.root / "apps/api/.venv/bin/python"),
                    str(Path(__file__).resolve()),
                    "--worker",
                ],
                cwd=self.root / "apps/api",
                env={
                    **self.environment,
                    "RASTER_IMAGES_READY_FD": str(writer),
                    "RASTER_IMAGES_STOPPED_FD": str(settled_writer),
                },
                pass_fds=(writer, settled_writer),
                start_new_session=True,
            )
        except BaseException:
            os.close(self.ready_fd)
            os.close(self.settled_fd)
            self.settled_fd = None
            self.ready_fd = None
            raise
        finally:
            os.close(writer)
            os.close(settled_writer)
        self.journal("spawned", {"pid": self.child.pid})
        self.descriptor = open_pidfd(self.child.pid)
        self.journal(
            "started",
            {"pid": self.child.pid, "start_ticks": _start_ticks(self.child.pid)},
        )
        deadline = time.monotonic() + 120
        data = b""
        while True:
            self.observe()
            if select.select([self.ready_fd], [], [], 0)[0]:
                chunk = os.read(self.ready_fd, 7)
                data += chunk
                require(len(data) <= 6, "raster_images_readiness_invalid")
                if not chunk:
                    require(data == b"ready\n", "raster_images_readiness_invalid")
                    os.close(self.ready_fd)
                    self.ready_fd = None
                    self.observe()
                    self.journal(
                        "ready",
                        {
                            "pid": self.child.pid,
                            "start_ticks": _start_ticks(self.child.pid),
                        },
                    )
                    return self
            require(time.monotonic() < deadline, "raster_images_start_timeout")
            time.sleep(0.1)

    def observe(self):
        _track_consumer_descendants(self.child.pid, self.tracked)
        outcome = waitid_pidfd(self.descriptor, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if outcome is not None:
            error = ContractError("raster_images_exited")
            error.images_outcome = {
                "code": outcome.si_code,
                "status": outcome.si_status,
            }
            raise error

    def close(self):
        try:
            _track_consumer_descendants(self.child.pid, self.tracked)
            requested = pidfd_alive(self.descriptor)
            if requested:
                pidfd_send_signal(self.descriptor, signal.SIGTERM)
                while pidfd_alive(self.descriptor):
                    _track_consumer_descendants(self.child.pid, self.tracked)
                    select.select([self.descriptor], [], [], 0.1)
            result = _drain_consumer(self.child, self.tracked, request_stop=False)
            if not requested or result != 0:
                error = ContractError("raster_images_exit_unsettled")
                error.images_outcome = {
                    "exit_code": result,
                    "shutdown_requested": requested,
                }
                raise error
            receipt = os.read(self.settled_fd, 9)
            require(
                receipt == b"settled\n" and os.read(self.settled_fd, 1) == b"",
                "raster_images_settlement_unconfirmed",
            )
            self.journal(
                "stopped",
                {
                    "pid": self.child.pid,
                    "exit_code": result,
                    "shutdown_requested": True,
                },
            )
        except BaseException:
            _UNSETTLED.append(self)
            raise
        else:
            if self.ready_fd is not None:
                os.close(self.ready_fd)
            os.close(self.settled_fd)
            os.close(self.descriptor)
            for descriptor in self.tracked.values():
                os.close(descriptor)


def _start_ticks(pid):
    value = Path(f"/proc/{pid}/stat").read_text()
    return int(value[value.rfind(")") + 2 :].split()[19])


def _worker_main():
    """Run the installed worker with its explicit native lifecycle descriptors."""
    import pyvips

    from yoao_assets_images_runtime import worker

    pyvips.version(0)
    ready_fd = os.environ.pop("RASTER_IMAGES_READY_FD")
    stopped_fd = os.environ.pop("RASTER_IMAGES_STOPPED_FD")
    sys.argv = [sys.argv[0], "--ready-fd", ready_fd, "--stopped-fd", stopped_fd]
    worker.main()


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    sys.argv = [sys.argv[0]]
    _worker_main()
