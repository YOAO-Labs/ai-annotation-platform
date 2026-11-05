"""Owned, same-database Inference services for canonical worktree E2E runs."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import select
import socket
import stat
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from worktree_env import WorktreeError, atomic_json, read_state, state_path
from worktree_runtime import (
    process_handle,
    process_identity,
    process_matches,
    require_owner,
)

_ROUTING = (
    "aap_inference_routing_v1.model_dispatch_step(text)",
    "aap_inference_routing_v1.prediction_scope_reserve(text)",
    "aap_inference_routing_v1.prediction_test_scope_reserve(text)",
    "aap_inference_routing_v1.routing_policy()",
    "aap_inference_routing_v1.routing_step(text,text)",
    "aap_inference_routing_v1.scope_retirement_step(text,text)",
    "aap_inference_routing_v1.supersede_unbegun_scope(text)",
    "aap_inference_routing_v1.video_execution_scope_step(text,text)",
)
_HEALTH = (
    "aap_inference_health_observation_v1.health_observation_policy()",
    "aap_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid)",
    "aap_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text)",
)


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class FixtureInferenceServices:
    """Own the actual routing and health providers, never their consumers' DB role."""

    def __init__(self, backend, *, code_root: Path):
        self.backend = backend
        self.resources = backend.resources
        if (
            self.resources["mode"] != "e2e"
            or Path(code_root).resolve() != backend.code_root
        ):
            raise WorktreeError("推理夹具只接受已核验的 e2e 工作树")
        self.code_root = backend.code_root
        root = Path(self.resources["root"])
        self.credentials_path = state_path(root, "e2e", "inference-credentials.json")
        self.marker_path = state_path(root, "e2e", "inference-owner.json")
        self.control_path = None
        self.process = None
        self.record = None
        self.pidfd = None
        self.poisoned = False
        self._credentials = None

    def _identity(self):
        return {
            "schema": 1,
            "owner": self.resources["owner"],
            "database": self.resources["database"],
            "postgres": {
                "host": self.backend.url.host,
                "port": self.backend.url.port or 5432,
                "migration_role": self.backend.url.username,
            },
            "routing_role": self.resources["database"] + "_routing",
            "health_role": self.resources["database"] + "_health",
        }

    def _load_credentials(self, *, create=False):
        path = self.credentials_path
        if not path.exists():
            if not create:
                return None
            value = {
                **self._identity(),
                "routing_password": secrets.token_urlsafe(32),
                "health_password": secrets.token_urlsafe(32),
                "routing_token": secrets.token_urlsafe(32),
                "health_token": secrets.token_urlsafe(32),
                "port": _port(),
            }
            atomic_json(path, value, exclusive=True)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor) as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise WorktreeError("推理夹具凭据必须属于当前用户且权限为 0600")
            try:
                value = json.load(stream)
            except (ValueError, UnicodeError):
                raise WorktreeError("推理夹具凭据损坏；保留原文件") from None
        expected = self._identity()
        secret_keys = (
            "routing_password",
            "health_password",
            "routing_token",
            "health_token",
        )
        if (
            not isinstance(value, dict)
            or set(value) != {*expected, *secret_keys, "port"}
            or any(value.get(key) != item for key, item in expected.items())
            or any(
                not isinstance(value.get(key), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value[key])
                for key in secret_keys
            )
            or type(value.get("port")) is not int
            or not 1 <= value["port"] <= 65535
        ):
            raise WorktreeError("推理夹具凭据与资源身份不一致；拒绝接管")
        return value

    async def _roles(self, action: str):
        credentials = self._load_credentials(create=action == "create")
        observed_db = await self.backend.database()
        if action == "create" and not require_owner(
            self.resources["database"], observed_db, self.resources["owner"]
        ):
            raise WorktreeError("推理夹具数据库尚未准备")
        engine = create_async_engine(
            self.backend.url
            if action == "create"
            else self.backend.url.set(database="postgres"),
            poolclass=NullPool,
            hide_parameters=True,
        )
        try:
            async with engine.begin() as connection:
                if action == "create":
                    owner = await connection.scalar(
                        text(
                            "SELECT shobj_description(oid, 'pg_database') FROM "
                            "pg_database WHERE datname=current_database()"
                        )
                    )
                    if owner != self.resources["owner"]:
                        raise WorktreeError("推理夹具数据库归属变化")
                else:
                    await self.backend.verify_database_server(connection)
                operator = (
                    await connection.execute(
                        text(
                            "SELECT oid,rolsuper,rolcreaterole FROM pg_roles "
                            "WHERE rolname=current_user"
                        )
                    )
                ).mappings().one()
                for kind, schema, functions in (
                    ("routing", "aap_inference_routing_v1", _ROUTING),
                    ("health", "aap_inference_health_observation_v1", _HEALTH),
                ):
                    role = self._identity()[kind + "_role"]
                    await connection.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:role))"),
                        {"role": role},
                    )
                    observed = await self.backend._runtime_role_record(
                        connection, operator["oid"], role=role
                    )
                    if observed:
                        self.backend._validate_runtime_role(observed, credentials)
                        if observed["sessions"]:
                            raise WorktreeError("推理夹具账号仍有连接；保留资源")
                    if action == "create" and observed is None:
                        if not (operator["rolsuper"] or operator["rolcreaterole"]):
                            raise WorktreeError("迁移账号需要 CREATEROLE 创建推理夹具账号")
                        await connection.execute(
                            text(
                                "SELECT set_config('aap.fixture_role',:role,true), "
                                "set_config('aap.fixture_password',:password,true), "
                                "set_config('aap.fixture_owner',:owner,true)"
                            ),
                            {
                                "role": role,
                                "password": credentials[kind + "_password"],
                                "owner": self.resources["owner"],
                            },
                        )
                        await connection.exec_driver_sql("""DO $fixture_role$
                        BEGIN
                          EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', current_setting('aap.fixture_role'), current_setting('aap.fixture_password'));
                          EXECUTE format('COMMENT ON ROLE %I IS %L', current_setting('aap.fixture_role'), current_setting('aap.fixture_owner'));
                        END; $fixture_role$;""")
                        quote = engine.dialect.identifier_preparer.quote_identifier
                        await connection.exec_driver_sql(
                            f"GRANT CONNECT ON DATABASE {quote(self.resources['database'])} "
                            f"TO {quote(role)}"
                        )
                        await connection.exec_driver_sql(
                            f"GRANT USAGE ON SCHEMA {quote(schema)} TO {quote(role)}"
                        )
                        await connection.exec_driver_sql(
                            f"GRANT EXECUTE ON FUNCTION {', '.join(functions)} TO {quote(role)}"
                        )
                        observed = await self.backend._runtime_role_record(
                            connection, operator["oid"], role=role
                        )
                        if not observed:
                            raise WorktreeError("无法核对推理夹具账号")
                        self.backend._validate_runtime_role(observed, credentials)
                    if action in {"check_drop", "drop"} and observed:
                        if not operator["rolsuper"] and not (
                            operator["rolcreaterole"] and observed["operator_admin"]
                        ):
                            raise WorktreeError("迁移账号不能删除推理夹具账号；保留资源")
                        if action == "drop":
                            quoted = engine.dialect.identifier_preparer.quote_identifier(
                                role
                            )
                            await connection.exec_driver_sql(f"DROP ROLE {quoted}")
        finally:
            await engine.dispose()

    def assert_stopped(self):
        if self.marker_path.exists():
            marker = read_state(self.marker_path)
            if marker.get("owner") != self.resources["owner"]:
                raise WorktreeError("推理夹具进程记录归属不一致；拒绝接管")
            raise WorktreeError("推理夹具进程尚未证明退出；保留记录与资源")

    def check_destroy(self):
        self.assert_stopped()
        asyncio.run(self._roles("check_drop"))

    def finish_destroy(self):
        self.assert_stopped()
        asyncio.run(self._roles("drop"))
        self.credentials_path.unlink(missing_ok=True)

    def start(self):
        if self.poisoned:
            raise WorktreeError("推理夹具停止结果不明；保留原进程")
        if self.process is not None:
            if self.process.poll() is not None:
                self.poisoned = True
                raise WorktreeError("推理夹具进程意外退出；保留记录")
            return self._selection()
        if self.record is not None:
            if not process_matches(
                self.record, str(self.code_root / "scripts/fixture_inference_services.py")
            ):
                self.poisoned = True
                raise WorktreeError("推理夹具进程身份变化；保留记录")
            return self._selection()
        if self.marker_path.exists():
            return self._reattach()
        self.assert_stopped()
        self._credentials = self._load_credentials(create=True)
        asyncio.run(self._roles("create"))
        try:
            return self._start_owned()
        except BaseException:
            self.poisoned = True
            raise

    def _start_owned(self):
        parent = process_handle(os.getpid(), str(self.code_root))
        if parent is None:
            raise WorktreeError("无法核对推理夹具监督进程")
        control_directory = Path("/tmp").resolve() / (
            f"aap-inference-{os.geteuid()}-{secrets.token_hex(16)}"
        )
        if control_directory.exists() or control_directory.is_symlink():
            raise WorktreeError("推理夹具控制目录已存在；拒绝接管")
        nonce = secrets.token_urlsafe(32)
        marker = {
            "owner": self.resources["owner"],
            "phase": "starting",
            "nonce": nonce,
            "parent": parent,
            "control_directory": str(control_directory),
            "control_directory_identity": None,
        }
        atomic_json(self.marker_path, marker, exclusive=True)
        if read_state(self.marker_path) != marker:
            raise WorktreeError("推理夹具启动记录归属变化；拒绝接管")
        control_directory.mkdir(mode=0o700)
        info = control_directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or read_state(self.marker_path) != marker
        ):
            raise WorktreeError("推理夹具控制目录或记录身份变化；保留资源")
        marker = {
            **marker,
            "control_directory_identity": {"device": info.st_dev, "inode": info.st_ino},
        }
        atomic_json(self.marker_path, marker)
        self.control_path = control_directory / "control.sock"
        environment = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH")
            if key in os.environ
        }
        script = self.code_root / "scripts/fixture_inference_services.py"
        self.process = subprocess.Popen(
            [sys.executable, str(script), "--serve"],
            cwd=self.code_root / "apps/api",
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        handle = process_handle(self.process.pid, str(script))
        if not handle:
            self.poisoned = True
            raise WorktreeError("无法核对推理夹具进程；保留记录")
        if not self.marker_path.exists() or read_state(self.marker_path) != marker:
            raise WorktreeError("推理夹具启动记录归属变化；保留进程")
        self.record = {**marker, **handle, "phase": "running"}
        atomic_json(self.marker_path, self.record)
        values = self._credentials
        payload = {
            "control_path": str(self.control_path),
            "control_directory_identity": marker["control_directory_identity"],
            "postgres": {
                "host": self.backend.url.host,
                "port": self.backend.url.port or 5432,
                "database": self.resources["database"],
            },
            "roles": {
                kind: {
                    "username": values[kind + "_role"],
                    "password": values[kind + "_password"],
                }
                for kind in ("routing", "health")
            },
            "routing_token": values["routing_token"],
            "health_token": values["health_token"],
            "port": values["port"],
            "nonce": nonce,
            "health_max_age_seconds": (
                self.backend.settings.ml_backend_router_health_max_age_seconds
            ),
            "lease_ttl_ms": (
                self.backend.settings.ml_backend_router_lease_ttl_seconds * 1000
            ),
        }
        try:
            self.process.stdin.write((json.dumps(payload) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self.poisoned = True
            raise WorktreeError("推理夹具未接收专用配置；保留进程记录") from None
        deadline = time.monotonic() + 30
        ready, _, _ = select.select(
            [self.process.stdout], [], [], max(0, deadline - time.monotonic())
        )
        if (
            not ready
            or self.process.stdout.readline().decode().strip() != f"ready:{nonce}"
        ):
            raise WorktreeError("推理夹具未确认绑定端口；保留进程与凭据")
        opener = build_opener(ProxyHandler({}))
        while time.monotonic() < deadline and self.process.poll() is None:
            try:
                with opener.open(
                    f"http://127.0.0.1:{values['port']}/health", timeout=1
                ) as response:
                    if (
                        response.status == 200
                        and json.load(response) == {"status": "ready"}
                        and self.process.poll() is None
                        and process_matches(self.record, str(script))
                    ):
                        return self._selection()
            except (OSError, ValueError):
                time.sleep(0.1)
        self.poisoned = True
        raise WorktreeError("推理夹具未就绪；保留进程与凭据")

    def _control(self, command: str, deadline: float) -> dict:
        directory = self.control_path.parent
        directory_info = directory.lstat()
        expected = self.record["control_directory_identity"]
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid()
            or stat.S_IMODE(directory_info.st_mode) != 0o700
            or (directory_info.st_dev, directory_info.st_ino)
            != (expected["device"], expected["inode"])
        ):
            raise WorktreeError("推理夹具控制目录身份不一致")
        info = self.control_path.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise WorktreeError("推理夹具控制 socket 归属或权限不一致")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.connect(str(self.control_path))
            connection.sendall(
                (json.dumps({"command": command, "nonce": self.record["nonce"]}) + "\n").encode()
            )
            data = b""
            while not data.endswith(b"\n"):
                if len(data) >= 4096:
                    raise WorktreeError("推理夹具控制回执过长")
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = connection.recv(4096 - len(data))
                if not chunk:
                    raise WorktreeError("推理夹具控制回执缺失")
                data += chunk
        result = json.loads(data)
        if not isinstance(result, dict) or result.get("nonce") != self.record["nonce"]:
            raise WorktreeError("推理夹具控制回执身份不一致")
        return result

    def _reattach(self):
        marker = read_state(self.marker_path)
        require_owner("inference-owner", marker, self.resources["owner"])
        parent = marker.get("parent")
        if marker.get("phase") != "running" or not isinstance(parent, dict):
            raise WorktreeError("推理夹具启动未完成；保留记录")
        pid = parent.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise WorktreeError("推理夹具监督进程身份无效")
        actual = process_identity(pid)
        if actual is None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise WorktreeError("无法确认原推理夹具监督进程退出")
        elif actual["started"] == parent.get("started"):
            raise WorktreeError("原推理夹具监督进程仍在运行")
        script = str(self.code_root / "scripts/fixture_inference_services.py")
        if not process_matches(marker, script) or not hasattr(os, "pidfd_open"):
            raise WorktreeError("无法核对原推理夹具进程；保留记录")
        descriptor = os.pidfd_open(marker["pid"])
        try:
            if not process_matches(marker, script):
                raise WorktreeError("推理夹具进程身份变化；保留记录")
            self._credentials = self._load_credentials()
            if self._credentials is None:
                raise WorktreeError("推理夹具凭据缺失；拒绝接管")
            directory = Path(marker.get("control_directory", ""))
            if (
                directory.parent != Path("/tmp").resolve()
                or not re.fullmatch(
                    rf"aap-inference-{os.geteuid()}-[a-f0-9]{{32}}", directory.name
                )
            ):
                raise WorktreeError("推理夹具控制目录路径无效")
            self.control_path = directory / "control.sock"
            self.record = marker
            if self._control("inspect", time.monotonic() + 5) != {
                "nonce": marker["nonce"],
                "status": "serving",
            }:
                raise WorktreeError("推理夹具正在排空；保留原记录")
            opener = build_opener(ProxyHandler({}))
            with opener.open(
                f"http://127.0.0.1:{self._credentials['port']}/health", timeout=5
            ) as response:
                if response.status != 200 or json.load(response) != {"status": "ready"}:
                    raise WorktreeError("原推理夹具未就绪；保留记录")
            parent = process_handle(os.getpid(), str(self.code_root))
            if (
                parent is None
                or read_state(self.marker_path) != marker
                or not process_matches(marker, script)
            ):
                raise WorktreeError("推理夹具进程或记录变化；拒绝接管")
            self.record = {**marker, "parent": parent}
            atomic_json(self.marker_path, self.record)
            self.pidfd = descriptor
            return self._selection()
        except BaseException:
            self.record = None
            os.close(descriptor)
            raise

    def _selection(self):
        value = self._credentials
        origin = f"http://127.0.0.1:{value['port']}"
        return {
            "INFERENCE_ROUTING_URL": origin,
            "INFERENCE_ROUTING_TOKEN": value["routing_token"],
            "INFERENCE_ROUTING_TOKEN_FILE": "",
            "INFERENCE_HEALTH_OBSERVATION_URL": origin,
            "INFERENCE_HEALTH_OBSERVATION_TOKEN": value["health_token"],
            "INFERENCE_HEALTH_OBSERVATION_TOKEN_FILE": "",
            "ML_BACKEND_ROUTER_MODE": "enforce",
        }

    def pause(self):
        if self.poisoned:
            raise WorktreeError("推理夹具停止结果不明；保留原进程")
        if self.record is None:
            return
        try:
            if (
                not self.marker_path.exists()
                or read_state(self.marker_path) != self.record
                or not process_matches(
                    self.record,
                    str(self.code_root / "scripts/fixture_inference_services.py"),
                )
            ):
                raise WorktreeError("推理夹具进程身份变化；保留记录")
            deadline = time.monotonic() + 12
            receipt = self._control("drain", deadline)
            if receipt != {"nonce": self.record["nonce"], "status": "drained"}:
                raise WorktreeError("推理夹具排空回执无效；保留资源")
            if self.process is not None:
                if self.process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                    raise ValueError
            else:
                if self.pidfd is None:
                    raise WorktreeError("无法核对推理夹具进程退出；保留资源")
                ready, _, _ = select.select(
                    [self.pidfd], [], [], max(0, deadline - time.monotonic())
                )
                if not ready:
                    raise TimeoutError
            if (
                not self.marker_path.exists()
                or read_state(self.marker_path) != self.record
            ):
                raise WorktreeError("推理夹具退出后记录归属变化；保留资源")
            if self.process is not None:
                self.process.stdin.close()
                self.process.stdout.close()
            if self.pidfd is not None:
                os.close(self.pidfd)
                self.pidfd = None
            self.marker_path.unlink()
            self.process = None
            self.record = None
        except BaseException as error:
            self.poisoned = True
            if isinstance(error, WorktreeError):
                raise
            if isinstance(
                error, (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired)
            ):
                raise WorktreeError("推理夹具排空或进程退出未确认；保留资源") from error
            raise

    async def _fixture_settled(self):
        engine = create_async_engine(
            self.backend.url, poolclass=NullPool, hide_parameters=True
        )
        try:
            async with engine.connect() as connection:
                identity = (
                    await connection.execute(
                        text(
                            "SELECT current_database() AS name, "
                            "shobj_description(oid, 'pg_database') AS owner "
                            "FROM pg_database WHERE datname=current_database()"
                        )
                    )
                ).mappings().one()
                if identity["name"] != self.resources["database"] or not require_owner(
                    identity["name"], identity, self.resources["owner"]
                ):
                    raise WorktreeError("推理夹具数据库身份变化；保留 owner")
                return not await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_runs "
                        "WHERE state <> 'retired') OR EXISTS (SELECT 1 FROM "
                        "yoao_fixture_v1.fixture_run_admissions WHERE state <> 'settled') "
                        "OR EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_maintenance_windows "
                        "WHERE state <> 'resumed')"
                    )
                )
        finally:
            await engine.dispose()

    def close(self):
        if self.poisoned:
            raise WorktreeError("推理夹具停止结果不明；保留原进程")
        with self.backend.fixture_authority_fence():
            if self._credentials is not None and not asyncio.run(self._fixture_settled()):
                raise WorktreeError("夹具责任尚未结算；保留推理 owner")
            self.pause()


async def _serve(payload):
    from yoao_inference.health_observation_manager import BackendHealthObservationManager
    from yoao_inference.health_observation_postgres import (
        BackendHealthObservationPostgresData,
    )
    from yoao_inference.pipeline_server import PipelineHttpServer
    from yoao_inference.routing_runtime import RoutingRuntime

    def dsn(kind):
        return URL.create(
            "postgresql", **payload["postgres"], **payload["roles"][kind]
        ).render_as_string(hide_password=False)

    routing = RoutingRuntime(
        dsn("routing"),
        health_max_age_seconds=payload["health_max_age_seconds"],
        lease_ttl_ms=payload["lease_ttl_ms"],
    )
    health_data = BackendHealthObservationPostgresData(dsn("health"))
    server = PipelineHttpServer(
        None,
        None,
        None,
        host="127.0.0.1",
        port=payload["port"],
        routing_manager=routing,
        routing_control_token=payload["routing_token"],
        health_observation_manager=BackendHealthObservationManager(health_data),
        health_observation_control_token=payload["health_token"],
        required_capabilities=("routing", "health_observation"),
    )
    control_path = Path(payload["control_path"])
    if control_path.exists() or control_path.is_symlink():
        raise RuntimeError("Inference control socket already exists")
    shutdown = asyncio.Event()
    draining = False
    controller = None
    socket_identity = None

    def verify_directory():
        current = control_path.parent.lstat()
        expected = payload["control_directory_identity"]
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) != 0o700
            or (current.st_dev, current.st_ino)
            != (expected["device"], expected["inode"])
        ):
            raise RuntimeError("Inference control directory identity changed")

    async def control(reader, writer):
        nonlocal draining
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5)
            request = json.loads(raw)
            if (
                not isinstance(request, dict)
                or set(request) != {"command", "nonce"}
                or not isinstance(request["nonce"], str)
                or not hmac.compare_digest(request["nonce"], payload["nonce"])
            ):
                return
            if request["command"] == "inspect":
                status = "draining" if draining else "serving"
            elif request["command"] == "drain" and not draining:
                draining = True
                await server.close()
                await routing.close()
                await health_data.close()
                controller.close()
                current = control_path.lstat()
                if (
                    not stat.S_ISSOCK(current.st_mode)
                    or (current.st_dev, current.st_ino) != socket_identity
                    or current.st_uid != os.geteuid()
                ):
                    raise RuntimeError("Inference control socket identity changed")
                verify_directory()
                control_path.unlink()
                control_path.parent.rmdir()
                status = "drained"
            else:
                return
            writer.write(
                (json.dumps({"nonce": payload["nonce"], "status": status}) + "\n").encode()
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            if status == "drained":
                shutdown.set()
        except (OSError, ValueError, TimeoutError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass

    verify_directory()
    controller = await asyncio.start_unix_server(
        control, path=str(control_path), limit=4096
    )
    os.chmod(control_path, 0o600)
    control_info = control_path.lstat()
    socket_identity = (control_info.st_dev, control_info.st_ino)
    await server.start()
    print(f"ready:{payload['nonce']}", flush=True)
    await shutdown.wait()
    await controller.wait_closed()


if __name__ == "__main__" and sys.argv[1:] == ["--serve"]:
    asyncio.run(_serve(json.loads(sys.stdin.buffer.readline())))
