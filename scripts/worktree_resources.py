"""Local PostgreSQL/MinIO ownership and Redis lifecycle for worktree environments."""

from __future__ import annotations

import ast
import asyncio
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
import warnings
from urllib.parse import urlsplit

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from grant_sql import read_runtime_grant_statements
from worktree_env import (
    WorktreeError,
    _validate_environment_sources,
    atomic_json,
    isolated_environment,
    load_identity,
    read_state,
    state_path,
    topology,
)
from worktree_runtime import (
    require_owner,
    validate_destruction,
    validate_inventory,
    validate_revisions,
)

OWNER_LABEL = "org.yoao.worktree-owner"
OWNER_TAG = "yoao-worktree-owner"
_OWNED_ROLE_SUFFIXES = {
    "runtime": "app",
    "jobs-dispatch": "jobs_claim",
    "jobs-publication": "jobs_pub",
    "assets-video": "assets_video",
    "assets-image": "assets_image",
}
_JOBS_ROLE_PURPOSES = ("jobs-dispatch", "jobs-publication")
ASSETS_EXECUTION_LANES = ("video", "image")


def _without_buckets(manifest: dict, *names: str) -> dict:
    result = json.loads(json.dumps(manifest))
    buckets = result.get("resources", {}).get("buckets", {})
    for name in names:
        buckets.pop(name, None)
    return result


def docker(*arguments: str) -> str:
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=90
    )
    if result.returncode:
        raise WorktreeError(f"Docker {arguments[0]} 失败；请检查本机 Docker 是否可用")
    return result.stdout.strip()


def verified_shared_bootstrap_minio(endpoint_url: str, *, container_id=None):
    """Verify a live bootstrap owner for application use."""
    return _verified_shared_bootstrap_minio(
        endpoint_url, state="running", container_id=container_id
    )


def _verified_finished_shared_bootstrap_minio(endpoint_url: str):
    """Verify a finished bootstrap owner only for resource retirement."""
    return _verified_shared_bootstrap_minio(endpoint_url, state="finished")


def _verified_shared_bootstrap_minio(
    endpoint_url: str, *, state: str, container_id=None
):
    """Verify the explicitly selected private bootstrap owner without modifying it."""
    selector = os.environ.get("AAP_MODULE_DATA_SHARED_BOOTSTRAP_OWNER")
    if not selector:
        return None

    from fixture_store import MINIO_IMAGE
    from verify_storage_candidates import Owner

    path = Path(selector)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.getuid()
        or path.stat().st_mode & 0o077
    ):
        raise WorktreeError(
            "Shared bootstrap owner manifest is not a private owned file"
        )
    owner = Owner(path)
    verification = owner.data.get("verification")
    if (
        owner.data.get("cleaned") is not False
        or not isinstance(verification, dict)
        or verification.get("state") != state
        or verification.get("endpoint") != endpoint_url
    ):
        raise WorktreeError("Shared bootstrap owner is not live for this endpoint")
    endpoint = urlsplit(endpoint_url)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.path
        or endpoint.query
        or endpoint.fragment
        or endpoint.port is None
        or endpoint.port == 0
        or endpoint_url != f"{endpoint.scheme}://{endpoint.netloc}"
    ):
        raise WorktreeError("Shared bootstrap endpoint is invalid")
    containers = [
        item
        for item in owner.data["resources"]
        if item.get("kind") == "container" and item.get("image") == MINIO_IMAGE
    ]
    networks = [
        item for item in owner.data["resources"] if item.get("kind") == "network"
    ]
    if (
        len(containers) != 1
        or len(networks) != 1
        or containers[0].get("state") != "created"
        or networks[0].get("state") != "created"
        or (container_id is not None and containers[0].get("id") != container_id)
    ):
        raise WorktreeError("Shared bootstrap resource identity is ambiguous")
    network = owner.verified(networks[0])
    container = owner.verified(containers[0])
    gateways = [
        ipaddress.ip_address(item["Gateway"])
        for item in (network.get("IPAM", {}).get("Config") or [])
        if item.get("Gateway") and ipaddress.ip_address(item["Gateway"]).version == 4
    ]
    if (
        network.get("Driver") != "bridge"
        or len(gateways) != 1
        or not gateways[0].is_private
        or gateways[0].is_loopback
        or gateways[0].is_link_local
        or gateways[0].is_multicast
        or gateways[0].is_reserved
        or gateways[0].is_unspecified
        or endpoint.hostname != str(gateways[0])
    ):
        raise WorktreeError("Shared bootstrap gateway differs from the owned bridge")
    bindings = (container.get("NetworkSettings", {}).get("Ports") or {}).get(
        "9000/tcp"
    ) or []
    attachments = [
        item
        for item in (
            container.get("NetworkSettings", {}).get("Networks") or {}
        ).values()
        if item.get("NetworkID") == networks[0]["id"]
    ]
    member = (network.get("Containers") or {}).get(containers[0]["id"]) or {}
    if (
        container.get("State", {}).get("Running") is not True
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", container.get("Image", ""))
        or (container.get("Config", {}).get("Labels") or {}).get(
            "com.docker.compose.service"
        )
        != "minio"
        or len(bindings) != 1
        or bindings[0].get("HostIp") != str(gateways[0])
        or bindings[0].get("HostPort") != str(endpoint.port)
        or len(attachments) != 1
        or attachments[0].get("Gateway") != str(gateways[0])
        or not attachments[0].get("EndpointID")
        or member.get("EndpointID") != attachments[0]["EndpointID"]
    ):
        raise WorktreeError("Shared bootstrap container or bridge attachment changed")
    return container


def unused_loopback_port() -> int:
    """Choose an explicit binding; Docker still rejects any intervening collision."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def migration_graph(code_root: Path) -> tuple[list[str], list[str]]:
    """Catch duplicate IDs before Alembic's revision map can overwrite them."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    api = code_root / "apps/api"
    revisions = []
    for path in sorted((api / "alembic/versions").glob("*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.parse(path.read_text(), filename=str(path)).body:
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else ([node.target] if isinstance(node, ast.AnnAssign) else [])
            )
            if any(
                isinstance(target, ast.Name) and target.id == "revision"
                for target in targets
            ):
                revisions.append(ast.literal_eval(node.value))
    if not revisions or len(revisions) != len(set(revisions)):
        raise WorktreeError("迁移 revision 缺失或重复；请先整合迁移历史")
    config = Config(str(api / "alembic.ini"))
    config.set_main_option("script_location", str(api / "alembic"))
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        scripts = ScriptDirectory.from_config(config)
        list(scripts.walk_revisions())
        heads = list(scripts.get_heads())
    validate_revisions(revisions, heads, [], skip=False)
    return revisions, heads


class _WorktreeDestroyPreparation:
    """A lock-scoped, single-use result of the existing destruction guards."""

    def __init__(self, backend, fixture_owner, inference):
        self.backend = backend
        self.fixture_owner = fixture_owner
        self.inference = inference
        self.phase = "prepared"
        self.storage = None
        self.raster_owner = None
        self.raster_database_oid = None
        self.raster_redis_retired = False
        self.raster_source_stopped = False
        self.legacy_original_retirement = None


class LocalResources:
    def __init__(
        self,
        resources: dict,
        settings,
        *,
        code_root: Path,
        retire_finished_bootstrap=False,
    ):
        self.root = Path(resources["root"])
        expected = topology(self.root, load_identity(self.root), resources["mode"])
        if resources != expected:
            raise WorktreeError("资源拓扑与本工作树 identity 不一致")
        self.resources = resources
        self.settings = settings
        self._fixture_overrides = {}
        self.code_root = code_root.resolve()
        self.touched = False
        self._prepared = False
        self._retire_finished_bootstrap = retire_finished_bootstrap
        self._fixture_inference_prepared = False
        self._destroy_preparation = None
        if retire_finished_bootstrap:
            _validate_environment_sources(
                self.resources,
                database_url=settings.database_url,
                migration_database_url=settings.effective_migration_database_url,
                minio_endpoint=settings.minio_endpoint,
                minio_use_ssl=settings.minio_use_ssl,
                redis_port=1,
                inherited=os.environ,
                retirement=True,
            )
        else:
            self._environment(
                1, require_inputs_bucket=False, database_url=settings.database_url
            )
        self.url = make_url(settings.effective_migration_database_url).set(
            database=resources["database"]
        )
        self.manifest = {
            "schema": 1,
            "resources": resources,
            "postgres": {"host": self.url.host, "port": self.url.port or 5432},
            "minio": {
                "endpoint": settings.minio_endpoint,
                "ssl": settings.minio_use_ssl,
            },
        }
        self.target_manifest = self.manifest
        path = state_path(self.root, resources["mode"], "resources.json")
        if retire_finished_bootstrap and not path.exists():
            raise WorktreeError("没有已登记的资源清单；拒绝退休或接管")
        if not path.exists() and any(
            state_path(self.root, resources["mode"], name).exists()
            for name in (
                "run.json",
                "services.json",
                "runtime-credentials.json",
                "assets-image-credentials.json",
                "assets-video-credentials.json",
                "fixture-store-owner.json",
            )
        ):
            raise WorktreeError("资源清单缺失但仍有归属状态；拒绝重新分配或接管")
        if path.exists():
            saved = read_state(path)
            if (
                not isinstance(saved.get("resources"), dict)
                or saved["resources"].get("owner") != resources["owner"]
            ):
                raise WorktreeError(
                    "基础设施目标与已有资源清单不一致；使用匹配的旧工具，不得重贴标签"
                )
            saved_postgres = saved.get("postgres")
            if isinstance(saved_postgres, dict) and "database_oid" in saved_postgres:
                oid = saved_postgres["database_oid"]
                if type(oid) is not int or oid <= 0:
                    raise WorktreeError("数据库出生身份无效；保留资源")
                self.manifest["postgres"]["database_oid"] = oid
            if saved != self.manifest:
                legacy_8 = _without_buckets(
                    self.manifest, "MINIO_TRAINING_BUCKET", "MINIO_INPUTS_BUCKET"
                )
                legacy_9 = _without_buckets(self.manifest, "MINIO_INPUTS_BUCKET")
                if saved != legacy_8 and saved != legacy_9:
                    raise WorktreeError(
                        "基础设施目标与已有资源清单不一致；先用原配置销毁旧环境"
                    )
                self.manifest = saved
                self.resources = saved["resources"]
        scheme = "https" if settings.minio_use_ssl else "http"
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"{scheme}://{settings.minio_endpoint}",
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            config=BotoConfig(
                signature_version="s3v4",
                connect_timeout=3,
                read_timeout=10,
                retries={"total_max_attempts": 1},
            ),
        )

    def _environment(
        self,
        port: int,
        *,
        require_inputs_bucket: bool = True,
        database_url: str | None = None,
    ) -> dict:
        if getattr(self, "_retire_finished_bootstrap", False):
            raise WorktreeError("退休模式不能创建子进程环境")
        return isolated_environment(
            self.resources,
            code_root=self.code_root,
            database_url=database_url
            or self.runtime_url().render_as_string(hide_password=False),
            migration_database_url=self.settings.effective_migration_database_url,
            minio_endpoint=self.settings.minio_endpoint,
            minio_use_ssl=self.settings.minio_use_ssl,
            redis_port=port,
            inherited=os.environ,
            require_inputs_bucket=require_inputs_bucket,
        )

    def _require_preparation_allowed(self) -> None:
        if getattr(self, "_retire_finished_bootstrap", False):
            raise WorktreeError("退休模式不能准备或接管环境")

    def _owned_role_path(self, purpose: str):
        return state_path(
            self.root, self.resources["mode"], f"{purpose}-credentials.json"
        )

    def _runtime_credentials(
        self, *, required: bool = False, purpose: str = "runtime"
    ) -> dict | None:
        path = self._owned_role_path(purpose)
        if not path.exists():
            if required:
                raise WorktreeError("独立运行账号尚未准备；请先运行 init")
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor) as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise WorktreeError(
                    "运行账号凭据必须属于当前用户且权限为 0600；不会读取不安全文件"
                )
            try:
                document = json.load(stream)
            except (json.JSONDecodeError, UnicodeError):
                raise WorktreeError(
                    "运行账号凭据损坏；拒绝重新生成或回退 owner"
                ) from None
        if not isinstance(document, dict):
            raise WorktreeError("运行账号凭据格式无效；拒绝接管")
        expected = self._runtime_credential_identity(purpose)
        if (
            set(document) != {*expected, "password"}
            or any(document.get(key) != value for key, value in expected.items())
            or not isinstance(document.get("password"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", document["password"])
        ):
            raise WorktreeError("运行账号凭据与本环境或原迁移账号不一致；拒绝接管")
        return document

    def _runtime_credential_identity(self, purpose: str = "runtime") -> dict:
        suffix = _OWNED_ROLE_SUFFIXES[purpose]
        return {
            "schema": 1,
            "owner": self.resources["owner"],
            "role": f"{self.resources['database']}_{suffix}",
            "postgres": {
                "host": self.url.host,
                "port": self.url.port or 5432,
                "migration_role": self.url.username,
            },
        }

    def runtime_url(self):
        configured = make_url(self.settings.database_url)
        if configured.username != self.url.username:
            return configured.set(database=self.resources["database"])
        credentials = self._runtime_credentials(required=True)
        return configured.set(
            database=self.resources["database"],
            username=credentials["role"],
            password=credentials["password"],
        )

    def assets_worker_url(self, lane):
        credentials = self._runtime_credentials(required=True, purpose=f"assets-{lane}")
        return self.url.set(
            username=credentials["role"], password=credentials["password"]
        )

    def assets_worker_environment(self) -> dict:
        return {
            f"ASSETS_CELERY_{lane.upper()}_DATABASE_URL": self.assets_worker_url(
                lane
            ).render_as_string(hide_password=False)
            for lane in ASSETS_EXECUTION_LANES
        }

    async def _runtime_role_record(self, connection, operator_oid, *, role=None):
        result = (
            (
                await connection.execute(
                    text(
                        "SELECT r.oid,r.rolname AS name,shobj_description(r.oid,'pg_authid') AS owner,"
                        "r.rolcanlogin AND NOT (r.rolsuper OR r.rolinherit OR r.rolcreatedb OR "
                        "r.rolcreaterole OR r.rolreplication OR r.rolbypassrls) AND r.rolconfig IS NULL "
                        "AND r.rolvaliduntil IS NULL AND NOT EXISTS(SELECT 1 FROM pg_database "
                        "WHERE datname=:database AND datdba=r.oid) AS safe,"
                        "EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid OR "
                        "(m.roleid=r.oid AND (m.member<>:operator OR NOT m.admin_option "
                        "OR m.inherit_option OR m.set_option))) AS foreign_membership,"
                        "(SELECT count(*) FROM pg_stat_activity WHERE usesysid=r.oid) AS sessions,"
                        "EXISTS(SELECT 1 FROM pg_shdepend d WHERE d.refclassid='pg_authid'::regclass "
                        "AND d.refobjid=r.oid AND d.classid<>'pg_auth_members'::regclass "
                        "AND NOT (d.dbid=COALESCE((SELECT oid FROM pg_database WHERE datname=:database),0) "
                        "AND d.dbid<>0) AND NOT (d.dbid=0 AND d.classid='pg_database'::regclass "
                        "AND d.objid=COALESCE((SELECT oid FROM pg_database WHERE datname=:database),0))) AS external_dependencies,"
                        "EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.roleid=r.oid "
                        "AND m.member=:operator AND m.admin_option) AS operator_admin "
                        "FROM pg_roles r WHERE r.rolname=:role"
                    ),
                    {
                        "role": role or self._runtime_credential_identity()["role"],
                        "operator": operator_oid,
                        "database": self.resources["database"],
                    },
                )
            )
            .mappings()
            .first()
        )
        return dict(result) if result else None

    def _validate_runtime_role(self, observed, credentials) -> None:
        require_owner(observed["name"], observed, self.resources["owner"])
        if not credentials:
            raise WorktreeError("运行账号已存在但缺少本地凭据；不会重置或接管")
        if (
            not observed["safe"]
            or observed["foreign_membership"]
            or observed["external_dependencies"]
        ):
            raise WorktreeError("自有运行账号权限、成员资格或外部依赖已变化；拒绝继续")

    async def owned_runtime_role(
        self, action: str = "inspect", *, purpose: str = "runtime"
    ) -> dict | None:
        """Keep generated LOGINs bound to this exact mode and purpose."""
        if action not in {"inspect", "check_create", "create", "check_drop", "drop"}:
            raise WorktreeError("无效的运行账号操作")
        if action in {"check_create", "create"}:
            self._require_preparation_allowed()
        credentials = self._runtime_credentials(purpose=purpose)
        shared_owner = (
            make_url(self.settings.database_url).username == self.url.username
        )
        if purpose == "runtime" and not shared_owner and credentials is None:
            return None
        if (
            purpose == "runtime"
            and action in {"check_create", "create"}
            and not shared_owner
        ):
            raise WorktreeError(
                "此环境已有自有运行账号；先用原配置销毁，再更换账号配置"
            )
        identity = self._runtime_credential_identity(purpose)
        role = identity["role"]
        engine = create_async_engine(
            self.url.set(database="postgres"),
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"timeout": 5, "command_timeout": 10},
        )
        try:
            async with engine.begin() as connection:
                await self.verify_database_server(connection)
                if action != "inspect":
                    await connection.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:role))"),
                        {"role": role},
                    )
                operator = (
                    (
                        await connection.execute(
                            text(
                                "SELECT oid,rolsuper,rolcreaterole FROM pg_roles WHERE rolname=current_user"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )

                observed = await self._runtime_role_record(
                    connection, operator["oid"], role=role
                )
                if observed is not None:
                    self._validate_runtime_role(observed, credentials)
                if action in {"check_create", "create"} and observed is None:
                    if not (operator["rolsuper"] or operator["rolcreaterole"]):
                        if purpose != "runtime":
                            raise WorktreeError(
                                f"迁移账号需要 CREATEROLE 创建隔离的 {purpose} 账号"
                            )
                        raise WorktreeError(
                            "同账号配置需要迁移账号具备 CREATEROLE；请由管理员预配置普通运行账号，"
                            "并分开 DATABASE_URL 与 MIGRATION_DATABASE_URL；不会回退 owner"
                        )
                    if action == "check_create":
                        return None
                    if credentials is None:
                        credentials = {
                            **identity,
                            "password": secrets.token_urlsafe(32),
                        }
                        atomic_json(
                            self._owned_role_path(purpose),
                            credentials,
                            exclusive=True,
                        )
                        credentials = self._runtime_credentials(
                            required=True, purpose=purpose
                        )
                    await connection.execute(
                        text(
                            "SELECT set_config('yoao.worktree_runtime_role',:role,true),"
                            "set_config('yoao.worktree_runtime_password',:password,true),"
                            "set_config('yoao.worktree_runtime_owner',:owner,true)"
                        ),
                        {
                            "role": role,
                            "password": credentials["password"],
                            "owner": self.resources["owner"],
                        },
                    )
                    await connection.exec_driver_sql("""DO $create_runtime$
                    BEGIN
                        EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
                            current_setting('yoao.worktree_runtime_role'),current_setting('yoao.worktree_runtime_password'));
                        EXECUTE format('COMMENT ON ROLE %I IS %L',
                            current_setting('yoao.worktree_runtime_role'),current_setting('yoao.worktree_runtime_owner'));
                    END;
                    $create_runtime$;""")
                    observed = await self._runtime_role_record(
                        connection, operator["oid"], role=role
                    )
                    if (
                        not observed
                        or not observed["safe"]
                        or observed["foreign_membership"]
                        or observed["external_dependencies"]
                        or observed["owner"] != self.resources["owner"]
                    ):
                        raise WorktreeError(
                            "自有运行账号创建后未能验证；保留凭据以便恢复"
                        )
                if action in {"check_drop", "drop"} and observed is not None:
                    if observed["sessions"]:
                        raise WorktreeError("运行账号仍有连接；不会强制终止未知客户端")
                    if not operator["rolsuper"] and not (
                        operator["rolcreaterole"] and observed["operator_admin"]
                    ):
                        raise WorktreeError(
                            "迁移账号不能删除自有运行账号；保留全部资源"
                        )
                    if action == "drop":
                        quoted = engine.dialect.identifier_preparer.quote_identifier(
                            role
                        )
                        await connection.exec_driver_sql(f"DROP ROLE {quoted}")
                        observed = await self._runtime_role_record(
                            connection, operator["oid"], role=role
                        )
                        if observed is not None:
                            raise WorktreeError("运行账号删除未能验证")
                if observed:
                    observed.pop("oid")
                    observed.pop("operator_admin")
                return observed
        finally:
            await engine.dispose()

    async def verify_runtime_login(self) -> bool:
        runtime = self.runtime_url()
        engine = create_async_engine(
            runtime,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"timeout": 5, "command_timeout": 10},
        )
        try:
            async with engine.connect() as connection:
                if (
                    await connection.scalar(text("SELECT current_user"))
                    != runtime.username
                ):
                    raise WorktreeError("运行账号身份与本环境不一致")
                return True
        finally:
            await engine.dispose()

    def jobs_database_url(self, purpose: str) -> str:
        if purpose not in _JOBS_ROLE_PURPOSES:
            raise WorktreeError("无效的 Jobs 账号用途")
        credentials = self._runtime_credentials(required=True, purpose=purpose)
        return self.url.set(
            drivername="postgresql",
            username=credentials["role"],
            password=credentials["password"],
        ).render_as_string(hide_password=False)

    async def grant_jobs_recovery(self) -> None:
        from asyncpg import PostgresError, connect
        from yoao_jobs.dispatch_postgres import RECOVERY_FUNCTIONS
        from yoao_jobs.publication_postgres import PUBLICATION_FUNCTIONS
        from yoao_owner_data.postgres import (
            FunctionRoleUnavailable,
            verify_function_only_role,
        )

        self._require_preparation_allowed()
        profiles = (
            ("jobs-dispatch", RECOVERY_FUNCTIONS),
            ("jobs-publication", PUBLICATION_FUNCTIONS),
        )
        engine = create_async_engine(self.url, poolclass=NullPool, hide_parameters=True)
        try:
            quote = engine.dialect.identifier_preparer.quote_identifier
            database = quote(self.resources["database"])
            runtime = self.runtime_url().username
            async with engine.begin() as db:
                observed = await db.scalar(
                    text(
                        "SELECT shobj_description(oid,'pg_database') "
                        "FROM pg_database WHERE datname=current_database()"
                    )
                )
                if observed != self.resources["owner"]:
                    raise WorktreeError("Jobs 授权数据库归属变化")
                if await db.scalar(
                    text(
                        "SELECT has_database_privilege(CAST(:role AS name), "
                        "current_database(),'TEMP')"
                    ),
                    {"role": runtime},
                ):
                    await db.exec_driver_sql(
                        f"GRANT TEMP ON DATABASE {database} TO {quote(runtime)}"
                    )
                await db.exec_driver_sql(
                    f"REVOKE TEMP ON DATABASE {database} FROM PUBLIC"
                )
                operator_oid = await db.scalar(
                    text("SELECT current_user::regrole::oid")
                )
                for purpose, functions in profiles:
                    credentials = self._runtime_credentials(
                        required=True, purpose=purpose
                    )
                    role = credentials["role"]
                    record = await self._runtime_role_record(
                        db, operator_oid, role=role
                    )
                    if record is None:
                        raise WorktreeError("缺少隔离 Jobs 账号")
                    self._validate_runtime_role(record, credentials)
                    signatures = ", ".join(
                        f"yoao_jobs_v1.{name}({arguments})"
                        for name, arguments in sorted(functions)
                    )
                    await db.exec_driver_sql(
                        f"GRANT CONNECT ON DATABASE {database} TO {quote(role)}"
                    )
                    await db.exec_driver_sql(
                        f"GRANT USAGE ON SCHEMA yoao_jobs_v1 TO {quote(role)}"
                    )
                    await db.exec_driver_sql(
                        f"GRANT EXECUTE ON FUNCTION {signatures} TO {quote(role)}"
                    )
        finally:
            await engine.dispose()
        for purpose, functions in profiles:
            try:
                connection = await connect(
                    self.jobs_database_url(purpose), timeout=5, command_timeout=5
                )
                try:
                    await verify_function_only_role(
                        connection,
                        schema="yoao_jobs_v1",
                        functions=functions,
                        role=self._runtime_credential_identity(purpose)["role"],
                    )
                finally:
                    await connection.close(timeout=5)
            except (PostgresError, OSError, FunctionRoleUnavailable):
                raise WorktreeError(f"无法核对本环境的 {purpose} 函数账号") from None

    def manage_worker_roles(self, action: str) -> None:
        for purpose in (
            *_JOBS_ROLE_PURPOSES,
            *(f"assets-{lane}" for lane in ASSETS_EXECUTION_LANES),
        ):
            asyncio.run(self.owned_runtime_role(action, purpose=purpose))

    async def prepare_assets_worker_roles(self) -> None:
        from yoao_assets_images_runtime.health import check_connection as check_image
        from yoao_assets_media_runtime.health import (
            check_worker_connection as check_video,
        )

        self._require_preparation_allowed()
        profiles = (
            ("video", "media", "asset_media_worker_role", check_video),
            ("image", "image", "asset_image_worker_role", check_image),
        )
        engine = create_async_engine(self.url, poolclass=NullPool, hide_parameters=True)
        try:
            async with engine.begin() as connection:
                await self.verify_database_server(connection)
                quote = connection.dialect.identifier_preparer.quote_identifier
                database = quote(self.resources["database"])
                operator = await connection.scalar(
                    text("SELECT current_user::regrole::oid")
                )
                for lane, script, variable, _ in profiles:
                    credentials = self._runtime_credentials(
                        required=True, purpose=f"assets-{lane}"
                    )
                    name = credentials["role"]
                    role = await self._runtime_role_record(
                        connection, operator, role=name
                    )
                    if role is None:
                        raise WorktreeError(f"缺少 Assets {lane} worker 专用账号")
                    self._validate_runtime_role(role, credentials)
                    grants = (
                        self.code_root / f"infra/docker/assets-{script}-grants.sql"
                    ).read_text()
                    prefix = f'\n    TO :"{variable}";'
                    if grants.count(prefix) != 1:
                        raise WorktreeError(f"Assets {lane} worker 授权片段缺失或重复")
                    functions = grants.split(prefix, 1)[0].rsplit(
                        "\nGRANT EXECUTE ON FUNCTION\n", 1
                    )[1]
                    await connection.exec_driver_sql(
                        f"GRANT CONNECT ON DATABASE {database} TO {quote(name)}"
                    )
                    await connection.exec_driver_sql(
                        f"GRANT USAGE ON SCHEMA yoao_assets_v1 TO {quote(name)}"
                    )
                    await connection.exec_driver_sql(
                        f"GRANT EXECUTE ON FUNCTION {functions} TO {quote(name)}"
                    )
        finally:
            await engine.dispose()
        for lane, _, _, check in profiles:
            worker = create_async_engine(
                self.assets_worker_url(lane), poolclass=NullPool, hide_parameters=True
            )
            try:
                async with worker.connect() as connection:
                    raw = await connection.get_raw_connection()
                    if not await check(raw.driver_connection):
                        raise WorktreeError(
                            f"Assets {lane} worker 专用账号的有效权限不符"
                        )
            finally:
                await worker.dispose()

    def environment(self) -> dict:
        if getattr(self, "_retire_finished_bootstrap", False):
            raise WorktreeError("退休模式不能创建子进程环境")
        observed = self.redis_info()
        require_owner(self.resources["redis_name"], observed, self.resources["owner"])
        if not observed or not observed["running"] or not observed["port"]:
            raise WorktreeError("独立 Redis 未运行；请先 init")
        environment = {**self._environment(observed["port"]), **self._fixture_overrides}
        if self._owned_role_path("jobs-publication").exists():
            environment["JOBS_PUBLICATION_DATABASE_URL"] = self.jobs_database_url(
                "jobs-publication"
            )
        return environment

    async def database(self, action: str = "inspect") -> dict | None:
        return await self._database(action)

    async def _database(
        self, action: str, *, raster_prepared=None, legacy_prepared=None
    ) -> dict | None:
        if action == "create":
            self._require_preparation_allowed()
        if action == "drop" and legacy_prepared is None:
            journal = state_path(
                self.root, self.resources["mode"], "legacy-original-retirement.json"
            )
            if journal.exists():
                raise WorktreeError("受管原件退休日志仍存在；拒绝删除数据库")
        if raster_prepared is not None:
            self._require_destroy_preparation(raster_prepared, "database_intent")
            if (
                action != "drop"
                or raster_prepared.storage != "raster_provider_retired"
                or type(raster_prepared.raster_database_oid) is not int
                or raster_prepared.raster_database_oid <= 0
            ):
                raise WorktreeError("Raster 数据库销毁身份不明确；保留资源")
        if legacy_prepared is not None:
            self._require_destroy_preparation(legacy_prepared, "database_intent")
            proof = legacy_prepared.legacy_original_retirement
            if (
                action != "drop"
                or not isinstance(proof, dict)
                or type(proof.get("database_oid")) is not int
                or proof["database_oid"] <= 0
            ):
                raise WorktreeError("受管原件数据库身份不明确；保留资源")
        engine = create_async_engine(
            self.url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={
                "timeout": 5,
                "command_timeout": None if action == "drop" else 10,
                "server_settings": {"lock_timeout": "10s"},
            },
        )
        name = self.resources["database"]
        try:
            async with engine.connect() as connection:
                await self.verify_database_server(connection)
                if action != "inspect":
                    await connection.execute(
                        text("SELECT pg_advisory_lock(hashtext(:name))"), {"name": name}
                    )
                try:

                    async def read():
                        row = (
                            (
                                await connection.execute(
                                    text(
                                        "SELECT oid, shobj_description(oid, 'pg_database') AS owner, "
                                        "(SELECT count(*) FROM pg_stat_activity WHERE datname = :name "
                                        "AND backend_type IS DISTINCT FROM 'autovacuum worker') AS sessions "
                                        "FROM pg_database WHERE datname = :name"
                                    ),
                                    {"name": name},
                                )
                            )
                            .mappings()
                            .first()
                        )
                        return dict(row) if row else None

                    if raster_prepared is not None:
                        self._require_raster_redis_retired(raster_prepared)
                    observed = await read()
                    if action == "inspect":
                        return (
                            {key: observed[key] for key in ("owner", "sessions")}
                            if observed
                            else None
                        )
                    if raster_prepared is not None and (
                        observed is None
                        or observed["oid"] != raster_prepared.raster_database_oid
                    ):
                        raise WorktreeError(
                            "Raster 原始数据库不存在或身份已变化；保留资源"
                        )
                    if legacy_prepared is not None and (
                        observed is None
                        or observed["oid"]
                        != legacy_prepared.legacy_original_retirement["database_oid"]
                    ):
                        raise WorktreeError("受管原件数据库身份已变化；保留资源")
                    present = require_owner(name, observed, self.resources["owner"])
                    quoted = engine.dialect.identifier_preparer.quote_identifier(name)
                    if action == "create":
                        path = state_path(
                            self.root, self.resources["mode"], "resources.json"
                        )
                        if not path.exists():
                            atomic_json(path, self.manifest, exclusive=True)
                        if read_state(path) != self.manifest:
                            raise WorktreeError(
                                "资源清单在数据库创建期间变化；保留资源"
                            )
                        birth_oid = self.manifest["postgres"].get("database_oid")
                        if birth_oid is not None and (
                            not present or observed["oid"] != birth_oid
                        ):
                            raise WorktreeError("数据库出生身份已变化；保留资源")
                        if (
                            present
                            and birth_oid is None
                            and self.resources["mode"] in {"test", "e2e"}
                        ):
                            raise WorktreeError("数据库缺少出生身份；拒绝接管")
                    if action == "create" and not present:
                        await connection.exec_driver_sql(f"CREATE DATABASE {quoted}")
                        owner = self.resources["owner"].replace("'", "''")
                        await connection.exec_driver_sql(
                            f"COMMENT ON DATABASE {quoted} IS '{owner}'"
                        )
                    elif action == "drop" and present:
                        if observed["sessions"]:
                            raise WorktreeError(
                                "数据库仍有连接；不会强制断开未知客户端"
                            )
                        await connection.exec_driver_sql(f"DROP DATABASE {quoted}")
                    verified = await read()
                    if action == "create" and not require_owner(
                        name, verified, self.resources["owner"]
                    ):
                        raise WorktreeError("数据库创建未能验证")
                    if action == "create" and not present:
                        if type(verified["oid"]) is not int or verified["oid"] <= 0:
                            raise WorktreeError("数据库出生身份未能验证；保留资源")
                        if read_state(path) != self.manifest:
                            raise WorktreeError(
                                "资源清单在数据库创建期间变化；保留资源"
                            )
                        self.manifest["postgres"]["database_oid"] = verified["oid"]
                        self.target_manifest["postgres"]["database_oid"] = verified[
                            "oid"
                        ]
                        atomic_json(path, self.manifest)
                    if action == "drop" and verified is not None:
                        raise WorktreeError("数据库删除未能验证")
                    return (
                        {key: verified[key] for key in ("owner", "sessions")}
                        if verified
                        else None
                    )
                finally:
                    if action != "inspect" and not connection.invalidated:
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(hashtext(:name))"),
                            {"name": name},
                        )
        finally:
            await engine.dispose()

    async def verify_database_server(self, owner_connection) -> None:
        """Prove both logins share one server before inspecting or changing resources."""
        selector = os.environ.get("AAP_PRIVATE_FIXTURE_RASTER_OWNER")
        require_private_postgres = (
            os.environ.get("AAP_PRIVATE_FIXTURE_RASTER_POSTGRES") == "1"
        )
        if require_private_postgres and not selector:
            raise WorktreeError("Raster PostgreSQL owner 缺失；保留资源")
        if selector:
            from raster_exclusive_provider import _items, verified_postgres
            from verify_storage_candidates import Owner

            raster = Owner(selector)
            _items(raster, self.resources)
            if require_private_postgres and "postgres" not in raster.data["raster"]:
                raise WorktreeError("Raster PostgreSQL 出生身份缺失；保留资源")
            if "postgres" in raster.data["raster"]:
                if self.url.host != "127.0.0.1":
                    raise WorktreeError("Raster PostgreSQL 必须使用原始 loopback 端口")
                verified_postgres(raster, self.resources, port=self.url.port or 5432)
        runtime = make_url(self.settings.database_url).set(
            database=owner_connection.engine.url.database
        )
        engine = create_async_engine(
            runtime,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"timeout": 5, "command_timeout": 10},
        )
        key = {"key": secrets.randbits(63)}
        await owner_connection.execute(text("SELECT pg_advisory_lock(:key)"), key)
        try:
            async with engine.connect() as connection:
                if await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), key
                ):
                    raise WorktreeError("API 与迁移必须连接同一个本机 PostgreSQL 实例")
        finally:
            await engine.dispose()
            await owner_connection.execute(text("SELECT pg_advisory_unlock(:key)"), key)

    def bucket_info(self, name: str) -> dict | None:
        try:
            self.s3.head_bucket(Bucket=name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchBucket"}:
                return None
            raise
        try:
            tags = self.s3.get_bucket_tagging(Bucket=name)["TagSet"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchTagSet":
                raise
            tags = []
        return {
            "owner": next(
                (tag["Value"] for tag in tags if tag["Key"] == OWNER_TAG), None
            )
        }

    def inputs_bucket_versioning(self) -> dict:
        """Read versioning only for an input bucket recorded in this mode manifest."""
        bucket = self.resources["buckets"].get("MINIO_INPUTS_BUCKET")
        if bucket is None:
            raise WorktreeError("旧资源清单没有输入 bucket")
        if not require_owner(bucket, self.bucket_info(bucket), self.resources["owner"]):
            raise WorktreeError(f"输入 bucket {bucket} 尚未归属于本环境")
        return self.s3.get_bucket_versioning(Bucket=bucket)

    def redis_info(self) -> dict | None:
        name = self.resources["redis_name"]
        identifier = docker(
            "container",
            "ls",
            "-a",
            "--filter",
            f"name=^/{name}$",
            "--format",
            "{{.ID}}",
        )
        if not identifier:
            return None
        if len(identifier.splitlines()) != 1:
            raise WorktreeError("Redis 容器名称匹配不唯一")
        info = json.loads(docker("container", "inspect", identifier))[0]
        bindings = (info.get("NetworkSettings", {}).get("Ports") or {}).get(
            "6379/tcp"
        ) or []
        if bindings and any(binding["HostIp"] != "127.0.0.1" for binding in bindings):
            raise WorktreeError("独立 Redis 端口必须仅绑定 127.0.0.1")
        return {
            "owner": (info["Config"].get("Labels") or {}).get(OWNER_LABEL),
            "running": info["State"]["Running"],
            "port": int(bindings[0]["HostPort"]) if bindings else None,
        }

    def inspect(self) -> dict:
        return {
            "database": asyncio.run(self.database()),
            "runtime_role": asyncio.run(self.owned_runtime_role()),
            "redis": self.redis_info(),
            "buckets": {
                name: self.bucket_info(name)
                for name in self.resources["buckets"].values()
            },
        }

    def _arm_raster_redis_birth(self, owner):
        if (
            self.resources["mode"] != "e2e"
            or owner.data["raster"]["resources"] != self.resources
            or "redis_birth" in owner.data["raster"]
            or getattr(self, "_raster_redis_birth_owner", None) is not None
        ):
            raise WorktreeError("Raster Redis 出生身份无效；拒绝接管")
        self._raster_redis_birth_owner = owner

    def start_redis(self) -> None:
        self._require_preparation_allowed()
        import redis

        name = self.resources["redis_name"]
        observed = self.redis_info()
        present = require_owner(name, observed, self.resources["owner"])
        birth_owner = getattr(self, "_raster_redis_birth_owner", None)
        if birth_owner is not None and present:
            raise WorktreeError("Raster Redis 已存在；拒绝接管")
        if not present:
            data = state_path(self.root, self.resources["mode"], "redis")
            data.mkdir(mode=0o700, parents=True, exist_ok=True)
            identifier = docker(
                "run",
                "-d",
                "--name",
                name,
                "--label",
                f"{OWNER_LABEL}={self.resources['owner']}",
                "--publish",
                f"127.0.0.1:{unused_loopback_port()}:6379",
                "--volume",
                f"{data}:/data",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "redis:7-alpine",
                "redis-server",
                "--save",
                "",
                "--appendonly",
                "yes",
            )
            if birth_owner is not None:
                if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
                    raise WorktreeError("Raster Redis 创建身份不明确；保留资源")
                started = json.loads(docker("container", "inspect", identifier))[0]
                state = started.get("State") or {}
                if not (
                    started.get("Id") == identifier
                    and started.get("Name") == "/" + name
                    and (started.get("Config", {}).get("Labels") or {}).get(OWNER_LABEL)
                    == self.resources["owner"]
                    and started.get("Config", {}).get("Image") == "redis:7-alpine"
                    and started.get("RestartCount") == 0
                    and state.get("Status") == "running"
                    and state.get("Running") is True
                    and isinstance(state.get("Pid"), int)
                    and state["Pid"] > 0
                    and bool(state.get("StartedAt"))
                ):
                    raise WorktreeError("Raster Redis 出生进程未确认；保留资源")
                birth_owner.data["raster"]["redis_birth"] = {
                    "container_id": identifier,
                    "pid": state["Pid"],
                    "started_at": state["StartedAt"],
                }
                birth_owner.save()
                self._raster_redis_birth_owner = None
        elif not observed["running"]:
            docker("start", name)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            info = self.redis_info()
            require_owner(name, info, self.resources["owner"])
            if info and info["running"] and info["port"]:
                client = redis.Redis(
                    host="127.0.0.1",
                    port=info["port"],
                    socket_timeout=1,
                    socket_connect_timeout=1,
                )
                try:
                    if client.ping():
                        return
                except redis.RedisError:
                    pass
                finally:
                    client.close()
            time.sleep(0.1)
        raise WorktreeError("独立 Redis 未能在 20 秒内就绪")

    async def revisions(self) -> list[str]:
        engine = create_async_engine(
            self.url,
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"timeout": 5, "command_timeout": 10},
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                exists = await connection.scalar(
                    text("SELECT to_regclass('public.alembic_version')")
                )
                if not exists:
                    return []
                return list(
                    (
                        await connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        )
                    ).scalars()
                )
        finally:
            await engine.dispose()

    async def grant_runtime(self) -> None:
        """Apply the same ordinary capability profile as the LAN installer."""
        self._require_preparation_allowed()
        runtime = self.runtime_url()
        directory = self.code_root / "infra/docker"
        grants = read_runtime_grant_statements(
            directory / "m2b-runtime-grants.sql", role=runtime.username
        )
        revoke = (directory / "ordinary-runtime-revoke.sql").read_text()
        verify = (directory / "ordinary-runtime-verify.sql").read_text()
        engine = create_async_engine(self.url, poolclass=NullPool, hide_parameters=True)
        quote = engine.dialect.identifier_preparer.quote_identifier
        role = quote(runtime.username)
        database = quote(self.resources["database"])
        try:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "SET LOCAL search_path = pg_catalog, pg_temp"
                )
                await connection.execute(
                    text(
                        "SELECT set_config('yoao.application_role', :role, true), "
                        "set_config('yoao.expected_database', :database, true), "
                        "set_config('yoao.migration_role', :migration, true)"
                    ),
                    {
                        "role": runtime.username,
                        "database": self.resources["database"],
                        "migration": self.url.username,
                    },
                )
                if make_url(self.settings.database_url).username == self.url.username:
                    operator_oid = await connection.scalar(
                        text("SELECT oid FROM pg_roles WHERE rolname=current_user")
                    )
                    observed = await self._runtime_role_record(connection, operator_oid)
                    if observed is None:
                        raise WorktreeError("自有运行账号在授权期间消失；授权已回滚")
                    self._validate_runtime_role(
                        observed, self._runtime_credentials(required=True)
                    )
                await connection.exec_driver_sql(revoke)
                await connection.exec_driver_sql(
                    f"REVOKE ALL ON DATABASE {database} FROM {role}"
                )
                await connection.exec_driver_sql(
                    f"GRANT CONNECT, TEMPORARY ON DATABASE {database} TO {role}"
                )
                for statement in grants:
                    await connection.exec_driver_sql(statement)
                await connection.exec_driver_sql(verify)
        finally:
            await engine.dispose()

    async def prepare_fixture_inference_database(self) -> None:
        """Fence PUBLIC TEMP in the verified disposable E2E database only."""
        self._fixture_inference_prepared = False
        if self.resources["mode"] != "e2e":
            raise WorktreeError("推理夹具数据库权限只能用于 e2e 模式")
        runtime_role = self.runtime_url().username
        observed = await self.database()
        if not require_owner(
            self.resources["database"], observed, self.resources["owner"]
        ):
            raise WorktreeError("推理夹具数据库尚未准备")
        engine = create_async_engine(self.url, poolclass=NullPool, hide_parameters=True)
        try:
            async with engine.begin() as connection:
                database = (
                    (
                        await connection.execute(
                            text(
                                "SELECT datname, shobj_description(oid, 'pg_database') AS owner "
                                "FROM pg_database WHERE datname = current_database()"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                if database["datname"] != self.resources["database"]:
                    raise WorktreeError("推理夹具数据库与工作树资源不一致")
                require_owner(database["datname"], database, self.resources["owner"])
                public_temp = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_database d, "
                        "LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) a "
                        "WHERE d.datname=current_database() AND a.grantee=0 "
                        "AND a.privilege_type='TEMPORARY')"
                    )
                )
                if not public_temp:
                    self._fixture_inference_prepared = True
                    return
                sessions = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                        "AND backend_type IS DISTINCT FROM 'autovacuum worker'"
                    )
                )
                if sessions:
                    raise WorktreeError("e2e 数据库仍有其他连接；不会更改 PUBLIC 权限")
                previous_temp = await connection.scalar(
                    text(
                        "SELECT has_database_privilege(:role, current_database(), 'TEMP')"
                    ),
                    {"role": runtime_role},
                )
                quoted_database = engine.dialect.identifier_preparer.quote_identifier(
                    database["datname"]
                )
                quoted_role = engine.dialect.identifier_preparer.quote_identifier(
                    runtime_role
                )
                if previous_temp:
                    await connection.exec_driver_sql(
                        f"GRANT TEMPORARY ON DATABASE {quoted_database} TO {quoted_role}"
                    )
                await connection.exec_driver_sql(
                    f"REVOKE TEMPORARY ON DATABASE {quoted_database} FROM PUBLIC"
                )
                current_temp = await connection.scalar(
                    text(
                        "SELECT has_database_privilege(:role, current_database(), 'TEMP')"
                    ),
                    {"role": runtime_role},
                )
                public_temp = await connection.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_database d, "
                        "LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) a "
                        "WHERE d.datname=current_database() AND a.grantee=0 "
                        "AND a.privilege_type='TEMPORARY')"
                    )
                )
                if current_temp != previous_temp or public_temp:
                    raise WorktreeError("无法核对推理夹具数据库 TEMP 权限")
        finally:
            await engine.dispose()
        self._fixture_inference_prepared = True

    async def reattach_fixture_store(self, environment: dict) -> dict:
        """Read the original published store and verify its exact durable identity."""
        import uuid

        from cryptography.fernet import Fernet
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from yoao_assets_source.connectors.crypto import decrypt_secret
        from yoao_assets_source.connectors.host_guard import (
            assert_host_allowed,
            normalize_allowlist,
        )
        from app.db.models.dataset_asset import StorageLocation
        from app.db.models.fixture_run import FixtureOriginalTarget, FixtureRun
        from app.db.models.storage_connection import StorageConnection
        from app.services.managed_original_retirement import managed_storage_identity
        from app.services.storage_location import connector_location_id
        from fixture_store import _private_bytes, reader_credentials, verified_store
        from fixture_suite_coordinator import (
            _fixture_target_id,
            _scale_only,
            _settled_thread,
            _verified_database,
        )
        from verify_storage_candidates import Owner

        owner_url, resources, identity = await _verified_database(environment)
        if resources != self.resources:
            raise WorktreeError("原件夹具数据库拓扑不一致")
        manifest = state_path(self.root, resources["mode"], "fixture-store-owner.json")
        key_path = state_path(
            self.root, resources["mode"], "fixture-connector-key.json"
        )
        lock_path = state_path(self.root, resources["mode"], "fixture-store.lock")
        descriptor = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise WorktreeError("原件夹具锁身份不一致")
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if not manifest.exists() or manifest.is_symlink():
                raise WorktreeError("原件夹具记录缺失")
            owner = Owner(manifest)
            if owner.data["cleaned"] or "fixture_teardown" in owner.data:
                raise WorktreeError("原件夹具已进入清理状态")
            store, account = await _settled_thread(verified_store, owner, identity)
            if (
                store["state"] != "published"
                or owner.data["fixture_intent"]["state"] != "published"
            ):
                raise WorktreeError("原件夹具尚未发布")
            configured_key = self.settings.connector_encryption_key or ""
            key = configured_key
            if key_path.exists():
                saved = json.loads(_private_bytes(key_path))
                if (
                    set(saved) != {"owner", "database_oid", "key"}
                    or saved["owner"] != resources["owner"]
                    or saved["database_oid"] != identity["database_oid"]
                    or not isinstance(saved["key"], str)
                    or (configured_key and configured_key != saved["key"])
                ):
                    raise WorktreeError("原件夹具连接器密钥身份不一致")
                key = saved["key"]
            try:
                Fernet(key.encode("ascii"))
            except (ValueError, TypeError, UnicodeError):
                raise WorktreeError("原件夹具连接器密钥无法验证") from None
            allowlist = normalize_allowlist(
                [*self.settings.connector_host_allowlist, store["bridge_address"]]
            )
            connection_id = uuid.uuid5(
                uuid.NAMESPACE_URL, "fixture-connection:" + owner.data["nonce"]
            )
            location_id = connector_location_id(connection_id)
            target_id = _fixture_target_id(owner.data["nonce"])
            provisioning_id = uuid.UUID(
                owner.data["fixture_intent"]["provisioning_run_id"]
            )
            storage_identity = managed_storage_identity(
                store["endpoint"], store["bucket"]
            )
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
                async with sessions() as db:
                    connection = await db.get(StorageConnection, connection_id)
                    location = await db.get(StorageLocation, location_id)
                    target = await db.get(FixtureOriginalTarget, target_id)
                    roots = list(
                        await db.scalars(
                            select(FixtureRun).where(FixtureRun.parent_run_id.is_(None))
                        )
                    )
                    provisioning = next(
                        (item for item in roots if item.id == provisioning_id), None
                    )
                    override = await db.scalar(
                        text(
                            "SELECT value_json FROM public.system_settings WHERE key='connector_host_allowlist'"
                        )
                    )
                    if override is not None and not isinstance(override, list):
                        raise WorktreeError("原件夹具连接器 allowlist 无效")
                    effective = (
                        normalize_allowlist(override)
                        if override is not None
                        else allowlist
                    )
                    assert_host_allowed(store["endpoint"], effective)
                    try:
                        writer_matches = (
                            connection is not None
                            and decrypt_secret(connection.secret_enc, key) == account
                        )
                    except Exception:
                        raise WorktreeError(
                            "原件夹具连接器密钥无法解密原账号"
                        ) from None
                    if not (
                        writer_matches
                        and connection.kind == "s3"
                        and connection.scope == "global"
                        and connection.project_id is None
                        and connection.revision == 1
                        and connection.config == config
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
                        and target.store_receipt_sha256 == store["receipt_sha256"]
                        and provisioning is not None
                        and provisioning.family == "owned"
                        and provisioning.state == "retired"
                        and provisioning.scope_key is None
                        and provisioning.sealed_at is not None
                        and provisioning.retired_at is not None
                        and all(
                            _scale_only(item)
                            or (
                                item.original_target_id == target_id
                                and item.original_storage_identity == storage_identity
                                and item.original_store_receipt_sha256
                                == store["receipt_sha256"]
                            )
                            for item in roots
                        )
                    ):
                        raise WorktreeError("原件夹具登记身份或回执不一致")
            finally:
                await engine.dispose()
            reader = reader_credentials(owner, store)
            return {
                "CONNECTOR_ENCRYPTION_KEY": key,
                "CONNECTOR_HOST_ALLOWLIST": json.dumps(allowlist),
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT": store["endpoint"],
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET": store["bucket"],
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY": reader["access_key"],
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY": reader["secret_key"],
            }
        finally:
            os.close(descriptor)

    def prepare(self, *, skip_migrations: bool = False) -> None:
        self._require_preparation_allowed()
        self._prepared = False
        self._fixture_inference_prepared = False
        revisions, heads = migration_graph(self.code_root)
        inventory = self.inspect()
        validate_inventory(self.resources, inventory)
        current = asyncio.run(self.revisions()) if inventory["database"] else []
        validate_revisions(revisions, heads, current, skip=skip_migrations)
        asyncio.run(self.owned_runtime_role("check_create"))
        self.manage_worker_roles("check_create")
        self.touched = True
        path = state_path(self.root, self.resources["mode"], "resources.json")
        if not path.exists():
            atomic_json(path, self.manifest)
        self.upgrade_training_bucket()
        self.upgrade_inputs_bucket()
        inventory = self.inspect()
        validate_inventory(self.resources, inventory)
        atomic_json(
            state_path(self.root, self.resources["mode"], "resources.json"),
            self.manifest,
        )
        asyncio.run(self.owned_runtime_role("create"))
        self.manage_worker_roles("create")
        asyncio.run(self.database("create"))
        raster_owner = getattr(self, "_raster_redis_birth_owner", None)
        if raster_owner is not None and "postgres" in raster_owner.data["raster"]:
            postgres = raster_owner.data["raster"]["postgres"]
            oid = self.manifest["postgres"].get("database_oid")
            if (
                os.environ.get("AAP_PRIVATE_FIXTURE_RASTER_OWNER")
                != str(raster_owner.path)
                or "database_oid" in postgres
                or type(oid) is not int
                or oid <= 0
            ):
                raise WorktreeError("Raster PostgreSQL 数据库出生身份不明确；保留资源")
            postgres["database_oid"] = oid
            raster_owner.save()
        for name, observed in inventory["buckets"].items():
            if observed is None:
                self.s3.create_bucket(Bucket=name)
                self.s3.put_bucket_tagging(
                    Bucket=name,
                    Tagging={
                        "TagSet": [{"Key": OWNER_TAG, "Value": self.resources["owner"]}]
                    },
                )
            require_owner(name, self.bucket_info(name), self.resources["owner"])
        self.ensure_inputs_bucket_versioning()
        if self.resources["mode"] in {"test", "e2e"}:
            self._ensure_owned_bucket_versioning(
                self.resources["buckets"]["MINIO_IMPORT_BUCKET"]
            )
        self.start_redis()
        for part in ("data", "tmp"):
            state_path(self.root, self.resources["mode"], part).mkdir(
                mode=0o700, parents=True, exist_ok=True
            )
        if os.environ.get("AAP_PRIVATE_FIXTURE_RASTER_OWNER"):
            from fixture_suite_coordinator import _verified_database

            asyncio.run(_verified_database(self.environment()))
        if not skip_migrations and set(current) != set(heads):
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=self.code_root / "apps/api",
                env=self.environment(),
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode:
                raise WorktreeError(
                    f"独立数据库迁移失败，退出码 {result.returncode}；共享数据库未被迁移"
                )
        validate_revisions(revisions, heads, asyncio.run(self.revisions()), skip=True)
        if os.environ.get("AAP_PRIVATE_FIXTURE_RASTER_OWNER"):
            asyncio.run(_verified_database(self.environment()))
        asyncio.run(self.grant_runtime())
        asyncio.run(self.grant_jobs_recovery())
        asyncio.run(self.verify_runtime_login())
        asyncio.run(self.prepare_assets_worker_roles())
        if self.resources["mode"] in {"test", "e2e"}:
            from fixture_suite_coordinator import prepare_worktree_store

            self._fixture_overrides = asyncio.run(
                prepare_worktree_store(
                    self.environment(),
                    resources=self.resources,
                    configured_key=self.settings.connector_encryption_key,
                    configured_allowlist=self.settings.connector_host_allowlist,
                )
            )
        self._prepared = True
        print(
            f"[dev:worktree] {self.resources['database']} 已在 head {heads[0]}",
            file=sys.stderr,
        )

    def upgrade_training_bucket(self) -> None:
        """Called only under the mode lock; retain old data on partial failure."""
        self._require_preparation_allowed()
        target = _without_buckets(self.target_manifest, "MINIO_INPUTS_BUCKET")
        if self.manifest == target or self.manifest == self.target_manifest:
            return
        path = state_path(self.root, self.resources["mode"], "resources.json")
        saved = read_state(path)
        if saved == self.target_manifest or saved == target:
            self.manifest = saved
            self.resources = saved["resources"]
            return
        if saved != self.manifest:
            raise WorktreeError("资源清单在准备期间变化；请重新检查环境")
        if "MINIO_TRAINING_BUCKET" in self.resources["buckets"]:
            raise WorktreeError("旧资源清单缺少训练 bucket 升级阶段")
        validate_inventory(self.resources, self.inspect())
        name = target["resources"]["buckets"]["MINIO_TRAINING_BUCKET"]
        journal = state_path(
            self.root, self.resources["mode"], "training-bucket-upgrade.json"
        )
        observed = self.bucket_info(name)
        recovery = read_state(journal) if journal.exists() else None
        if recovery and recovery != {"bucket": name, "owner": self.resources["owner"]}:
            raise WorktreeError("训练 bucket 恢复记录与当前环境不一致")
        if recovery and observed is not None and observed.get("owner") is None:
            raise WorktreeError(
                f"训练 bucket {name} 的创建曾中断且缺少归属标签；"
                "请管理员核对恢复记录和桶内容，确认空桶后仅删除该空桶，再运行 init。"
                "不会自动接管或清空未标记的桶"
            )
        require_owner(name, observed, self.resources["owner"])
        if observed is None:
            atomic_json(journal, {"bucket": name, "owner": self.resources["owner"]})
            self.s3.create_bucket(Bucket=name)
            try:
                self.s3.put_bucket_tagging(
                    Bucket=name,
                    Tagging={
                        "TagSet": [{"Key": OWNER_TAG, "Value": self.resources["owner"]}]
                    },
                )
            except Exception:
                current = self.bucket_info(name)
                if current is not None and current.get("owner") is None:
                    try:
                        self.s3.delete_bucket(Bucket=name)
                    except Exception:
                        pass
                    else:
                        journal.unlink(missing_ok=True)
                raise
        if not require_owner(name, self.bucket_info(name), self.resources["owner"]):
            raise WorktreeError("训练 bucket 创建后未能验证；保留旧资源清单")
        atomic_json(path, target)
        self.manifest = target
        self.resources = self.manifest["resources"]
        journal.unlink(missing_ok=True)

    def _ensure_inputs_bucket(self) -> str:
        """Create/tag/enable versioning only for the exact mode-owned input bucket."""
        self._require_preparation_allowed()
        name = self.target_manifest["resources"]["buckets"]["MINIO_INPUTS_BUCKET"]
        owner = self.target_manifest["resources"]["owner"]
        journal = state_path(
            self.root,
            self.target_manifest["resources"]["mode"],
            "inputs-bucket-upgrade.json",
        )
        observed = self.bucket_info(name)
        recovery = read_state(journal) if journal.exists() else None
        expected_recovery = {"bucket": name, "owner": owner}
        if recovery is not None and recovery != expected_recovery:
            raise WorktreeError("输入 bucket 恢复记录与当前环境不一致")
        if recovery and observed is not None and observed.get("owner") is None:
            raise WorktreeError(
                f"输入 bucket {name} 的创建曾中断且缺少归属标签；"
                "请核对资源归属，不会自动接管或清空未标记的桶"
            )
        owned = require_owner(name, observed, owner)
        if not owned:
            atomic_json(journal, expected_recovery)
            self.s3.create_bucket(Bucket=name)
            try:
                self.s3.put_bucket_tagging(
                    Bucket=name,
                    Tagging={"TagSet": [{"Key": OWNER_TAG, "Value": owner}]},
                )
            except Exception:
                current = self.bucket_info(name)
                if current is None or current.get("owner") != owner:
                    raise WorktreeError(
                        f"输入 bucket {name} 创建后未能验证归属；保留恢复记录"
                    ) from None
        if not require_owner(name, self.bucket_info(name), owner):
            raise WorktreeError(f"输入 bucket {name} 创建后未能验证归属")
        self.ensure_inputs_bucket_versioning(name=name)
        return name

    def ensure_inputs_bucket_versioning(self, *, name: str | None = None) -> None:
        """Keep versioning confined to the mode-owned accepted-input bucket."""
        bucket = name or self.resources["buckets"].get("MINIO_INPUTS_BUCKET")
        if bucket is None:
            raise WorktreeError(
                "旧资源清单没有输入 bucket；请显式运行 init 完成资源升级"
            )
        self._ensure_owned_bucket_versioning(bucket)

    def _ensure_owned_bucket_versioning(self, bucket: str) -> None:
        self._require_preparation_allowed()
        if not require_owner(bucket, self.bucket_info(bucket), self.resources["owner"]):
            raise WorktreeError(f"bucket {bucket} 尚未归属于本环境")
        state = self.s3.get_bucket_versioning(Bucket=bucket)
        if state.get("Status") != "Enabled":
            self.s3.put_bucket_versioning(
                Bucket=bucket,
                VersioningConfiguration={"Status": "Enabled"},
            )
            state = self.s3.get_bucket_versioning(Bucket=bucket)
        if state.get("Status") != "Enabled":
            raise WorktreeError(f"bucket {bucket} 必须启用 versioning")

    def upgrade_inputs_bucket(self) -> None:
        """Explicit prepare step: upgrade a legacy 8/9-bucket mode to 10 buckets."""
        self._require_preparation_allowed()
        target = self.target_manifest
        intermediate = _without_buckets(target, "MINIO_INPUTS_BUCKET")
        if self.manifest == target:
            self._ensure_inputs_bucket()
            state_path(
                self.root, self.resources["mode"], "inputs-bucket-upgrade.json"
            ).unlink(missing_ok=True)
            return
        path = state_path(self.root, self.resources["mode"], "resources.json")
        saved = read_state(path)
        if saved == target:
            self.manifest = saved
            self.resources = saved["resources"]
            self._ensure_inputs_bucket()
            state_path(
                self.root, self.resources["mode"], "inputs-bucket-upgrade.json"
            ).unlink(missing_ok=True)
            return
        if saved != self.manifest:
            raise WorktreeError("资源清单在准备期间变化；请重新检查环境")
        if self.manifest != intermediate:
            raise WorktreeError("输入 bucket 升级必须从已核验的旧资源清单开始")
        validate_inventory(self.resources, self.inspect())
        self._ensure_inputs_bucket()
        atomic_json(path, target)
        self.manifest = target
        self.resources = target["resources"]
        state_path(
            self.root, self.resources["mode"], "inputs-bucket-upgrade.json"
        ).unlink(missing_ok=True)

    def stop_redis(self) -> None:
        observed = self.redis_info()
        if (
            require_owner(
                self.resources["redis_name"], observed, self.resources["owner"]
            )
            and observed["running"]
        ):
            docker("stop", self.resources["redis_name"])
            verified = self.redis_info()
            if verified and verified["running"]:
                raise WorktreeError("独立 Redis 停止未能验证")

    def _fixture_native_supervisor_closed(self, directory):
        evidence = {
            path.name: path
            for path in directory.iterdir()
            if path.name.startswith("native-")
        }
        if not evidence:
            return False

        def record(path):
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor) as stream:
                    info, current = os.fstat(stream.fileno()), path.lstat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1
                        or (info.st_dev, info.st_ino)
                        != (current.st_dev, current.st_ino)
                    ):
                        raise WorktreeError("原生进程回执身份或权限无效；保留资源")
                    value = json.load(stream)
            except (OSError, ValueError, UnicodeError) as exc:
                raise WorktreeError("原生进程回执缺失或损坏；保留资源") from exc
            if not isinstance(value, dict):
                raise WorktreeError("原生进程回执无效；保留资源")
            return value

        def canonical_uuid(value):
            try:
                return isinstance(value, str) and str(uuid.UUID(value)) == value
            except (ValueError, TypeError, AttributeError):
                return False

        def disposition(value):
            return (
                isinstance(value, dict)
                and type(value.get("pid")) is int
                and value["pid"] > 0
                and type(value.get("start")) is int
                and value["start"] > 0
                and type(value.get("exit_code")) is int
                and value["exit_code"] == 0
                and value.get("pidfd_exit") is True
            )

        supervisor = record(directory / "native-supervisor.json")
        suite_id = supervisor.get("suite_id")
        run_ids = supervisor.get("native_run_ids")
        api_owned = supervisor.get("api_owned")
        if (
            supervisor.get("schema") != "aap-fixture-native-supervisor/v1"
            or supervisor.get("resources") != self.resources
            or not canonical_uuid(suite_id)
            or (
                directory.name.startswith("suite-")
                and directory.name != "suite-" + uuid.UUID(suite_id).hex
            )
            or supervisor.get("state") != "retired"
            or not isinstance(run_ids, list)
            or not all(canonical_uuid(run_id) for run_id in run_ids)
            or len(set(run_ids)) != len(run_ids)
            or supervisor.get("resolver_retired") is not True
            or supervisor.get("business_retired") is not True
            or type(api_owned) is not bool
        ):
            raise WorktreeError("原生套件监督器未确认完整退役；保留资源")
        if not api_owned and (
            self.resources["mode"] != "test"
            or "api" in supervisor
            or "api_disposition" in supervisor
        ):
            raise WorktreeError("无 API 的原生监督拓扑与实际参与者不符；保留资源")
        if "root_origin" in supervisor:
            if (
                supervisor["root_origin"]
                != {"family": "screenshot", "parent_run_id": None}
                or directory.name != "suite-" + uuid.UUID(suite_id).hex
                or run_ids != [suite_id]
                or not canonical_uuid(supervisor.get("birth_admission_id"))
                or not isinstance(supervisor.get("authority_nonce"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", supervisor["authority_nonce"])
            ):
                raise WorktreeError("截图原生根运行出生证据不符；保留资源")
            generation = record(directory / "generation.json")
            if generation != {
                "resources": self.resources,
                "nonce": supervisor["authority_nonce"],
                "state": "closed",
            }:
                raise WorktreeError(
                    "截图原生根运行的 authority 代次未关闭或已变化；保留资源"
                )
        elif (
            suite_id in run_ids
            or "birth_admission_id" in supervisor
            or "authority_nonce" in supervisor
        ):
            raise WorktreeError("原生根运行缺少明确的截图出生证据；保留资源")
        for name, acknowledgment in (
            ("api", "lifespan_shutdown"),
            ("control", "settled_ack"),
        ):
            if name == "api" and not api_owned:
                continue
            result = supervisor.get(name + "_disposition")
            birth = supervisor.get(name)
            if (
                not disposition(result)
                or result.get(acknowledgment) is not True
                or result.get("descendants_drained") is not True
                or not isinstance(birth, dict)
                or type(birth.get("pid")) is not int
                or type(birth.get("start")) is not int
                or (result["pid"], result["start"])
                != (birth.get("pid"), birth.get("start"))
            ):
                raise WorktreeError("原生套件子进程未确认物理退出；保留资源")
        expected_directories = {
            "native-run-" + uuid.UUID(run_id).hex for run_id in run_ids
        }
        if {
            name for name in evidence if name.startswith("native-run-")
        } != expected_directories:
            raise WorktreeError("原生子运行回执集合不一致；保留资源")
        children = {
            "celery-media",
            "celery-pyramid",
            "media-worker",
            "image-worker",
            "proxy",
        }
        for run_id in run_ids:
            child = evidence["native-run-" + uuid.UUID(run_id).hex]
            info = child.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise WorktreeError("原生子运行目录身份无效；保留资源")
            run = record(child / "owner.json")
            drained = run.get("drained")
            outcomes = run.get("child_dispositions")
            proof = run.get("target_proof")
            if (
                run.get("schema") != "aap-fixture-native-run/v1"
                or run.get("suite_id") != suite_id
                or run.get("run_id") != run_id
                or run.get("state") != "retired"
                or not isinstance(drained, list)
                or not all(isinstance(name, str) for name in drained)
                or len(drained) != len(children)
                or set(drained) != children
                or not isinstance(outcomes, dict)
                or set(outcomes) != children
                or not isinstance(proof, dict)
                or proof.get("schema") != "aap-fixture-native-target-retirement/v1"
                or proof.get("run_id") != run_id
                or proof.get("target_receipt_sha256") != run.get("receipt_sha256")
                or not canonical_uuid(proof.get("retirement_id"))
                or not canonical_uuid(proof.get("location_id"))
                or type(proof.get("binding_generation")) is not int
                or proof["binding_generation"] < 1
                or not all(
                    isinstance(proof.get(key), str)
                    and re.fullmatch(r"[a-f0-9]{64}", proof[key])
                    for key in ("storage_identity", "target_receipt_sha256")
                )
            ):
                raise WorktreeError("原生子运行未确认完整退役；保留资源")
            for name, result in outcomes.items():
                birth = run.get(name)
                if (
                    not disposition(result)
                    or result.get("acknowledgment")
                    != ("warm_exit" if name.startswith("celery-") else "settled")
                    or result.get("descendants_drained") is not True
                    or not isinstance(birth, dict)
                    or type(birth.get("pid")) is not int
                    or type(birth.get("start")) is not int
                    or (result["pid"], result["start"])
                    != (birth.get("pid"), birth.get("start"))
                ):
                    raise WorktreeError("原生子运行后代仍未确认排空；保留资源")
        return True

    @contextmanager
    def fixture_authority_fence(self):
        if self.resources["mode"] not in {"test", "e2e"}:
            yield
            return
        path = state_path(self.root, self.resources["mode"], "fixture-authority.lock")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            current = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise WorktreeError("夹具 authority 锁的身份或权限无效；保留资源")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorktreeError("夹具 authority 仍在运行；保留资源") from None
            current = path.lstat()
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise WorktreeError("夹具 authority 锁身份变化；保留资源")
            authority_root = state_path(
                self.root, self.resources["mode"], "fixture-authority"
            )
            directories = []
            if authority_root.exists() or authority_root.is_symlink():
                root_info = authority_root.lstat()
                if (
                    not stat.S_ISDIR(root_info.st_mode)
                    or root_info.st_uid != os.geteuid()
                    or stat.S_IMODE(root_info.st_mode) != 0o700
                ):
                    raise WorktreeError("夹具 authority 目录身份或权限无效；保留资源")
                directories.append(authority_root)
                for child in sorted(authority_root.iterdir()):
                    child_info = child.lstat()
                    if not stat.S_ISDIR(child_info.st_mode) and not child.is_symlink():
                        continue
                    if re.fullmatch(
                        r"native-run-[a-f0-9]{32}", child.name
                    ) and stat.S_ISDIR(child_info.st_mode):
                        continue
                    if (
                        not re.fullmatch(r"suite-[a-f0-9]{32}", child.name)
                        or not stat.S_ISDIR(child_info.st_mode)
                        or child_info.st_uid != os.geteuid()
                        or stat.S_IMODE(child_info.st_mode) != 0o700
                    ):
                        raise WorktreeError(
                            "夹具 suite authority 目录身份无效；保留资源"
                        )
                    directories.append(child)
            for directory in directories:
                native_closed = self._fixture_native_supervisor_closed(directory)
                owner_path = directory / "owner.json"
                generation_path = directory / "generation.json"
                if owner_path.exists() or owner_path.is_symlink():
                    raise WorktreeError("夹具 authority 所有者记录仍存在；保留资源")
                if not generation_path.exists() and not generation_path.is_symlink():
                    if directory != authority_root or native_closed:
                        raise WorktreeError(
                            "夹具 suite authority 缺少代次记录；保留资源"
                        )
                    continue
                generation_fd = os.open(generation_path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(generation_fd) as generation_stream:
                    generation_info = os.fstat(generation_stream.fileno())
                    if (
                        not stat.S_ISREG(generation_info.st_mode)
                        or generation_info.st_uid != os.geteuid()
                        or stat.S_IMODE(generation_info.st_mode) != 0o600
                        or generation_info.st_nlink != 1
                    ):
                        raise WorktreeError("夹具 authority 代次记录权限无效；保留资源")
                    try:
                        generation = json.load(generation_stream)
                    except (ValueError, UnicodeError):
                        raise WorktreeError(
                            "夹具 authority 代次记录损坏；保留资源"
                        ) from None
                if (
                    not isinstance(generation, dict)
                    or set(generation) != {"resources", "nonce", "state"}
                    or generation["resources"] != self.resources
                    or not isinstance(generation["nonce"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", generation["nonce"])
                    or generation["state"] != "closed"
                ):
                    raise WorktreeError("夹具 authority 代次未确认关闭；保留资源")
            yield
        finally:
            os.close(descriptor)

    def destroy(self, confirmation: str | None) -> None:
        journal = state_path(
            self.root, self.resources["mode"], "legacy-original-retirement.json"
        )
        if journal.exists():
            raise WorktreeError(
                "受管原件退休日志仍存在；保留数据库和存储，使用其所属受限退休流程"
            )
        if getattr(self, "_retire_finished_bootstrap", False):
            _validate_environment_sources(
                self.resources,
                database_url=self.settings.database_url,
                migration_database_url=self.settings.effective_migration_database_url,
                minio_endpoint=self.settings.minio_endpoint,
                minio_use_ssl=self.settings.minio_use_ssl,
                redis_port=1,
                inherited=os.environ,
                retirement=True,
            )
        with self.fixture_authority_fence():
            self._destroy_owned(confirmation)

    @contextmanager
    def _prepare_owned_destruction(self, confirmation: str | None):
        if self._destroy_preparation is not None:
            raise WorktreeError("销毁准备已在进行；拒绝重复操作")
        input_journal = state_path(
            self.root, self.resources["mode"], "inputs-bucket-upgrade.json"
        )
        if (
            input_journal.exists()
            and "MINIO_INPUTS_BUCKET" not in self.resources["buckets"]
        ):
            raise WorktreeError(
                "输入 bucket 升级未完成；先显式运行 init 完成并登记资源，"
                "再使用精确确认值销毁"
            )
        inventory = self.inspect()
        validate_destruction(self.resources, inventory, confirmation)
        asyncio.run(self.owned_runtime_role("check_drop"))
        self.manage_worker_roles("check_drop")
        inference = None
        if self.resources["mode"] == "e2e":
            from fixture_inference_services import FixtureInferenceServices

            inference = FixtureInferenceServices(self, code_root=self.code_root)
            inference.check_destroy()
        fixture_owner = None
        fixture_lock = None
        try:
            if self.resources["mode"] in {"test", "e2e"}:
                from fixture_suite_coordinator import (
                    acquire_store_destruction_lock,
                    prepare_store_destruction,
                )

                fixture_lock = acquire_store_destruction_lock(self.resources)
                fixture_owner = asyncio.run(
                    prepare_store_destruction(
                        self.url, self.resources, inventory["database"] is not None
                    )
                )
            prepared = _WorktreeDestroyPreparation(self, fixture_owner, inference)
            self._destroy_preparation = prepared
            yield prepared
        finally:
            self._destroy_preparation = None
            if fixture_lock is not None:
                os.close(fixture_lock)

    def _require_destroy_preparation(self, prepared, phase):
        if (
            prepared is not self._destroy_preparation
            or prepared.backend is not self
            or prepared.phase != phase
        ):
            raise WorktreeError("销毁准备或排他锁状态不一致；保留资源")

    def _accept_legacy_original_retirement(self, prepared, journal, proof):
        """Bind an exact completed physical retirement to this locked teardown."""
        from app.services.dataset_assets import LEGACY_LOCATION_ID
        from app.services.managed_original_retirement import managed_storage_identity

        self._require_destroy_preparation(prepared, "prepared")
        path = state_path(
            self.root, self.resources["mode"], "legacy-original-retirement.json"
        )
        bucket = self.resources["buckets"]["MINIO_DATASETS_BUCKET"]
        endpoint = (
            f"{'https' if self.settings.minio_use_ssl else 'http'}://"
            f"{self.settings.minio_endpoint}"
        ).rstrip("/")
        if not (
            path.exists()
            and self.resources["mode"] == "test"
            and prepared.storage is None
            and prepared.raster_owner is None
            and read_state(path) == journal
            and journal.get("resources") == self.resources
            and journal.get("endpoint") == self.settings.minio_endpoint
            and journal.get("use_ssl") is self.settings.minio_use_ssl
            and journal.get("bucket") == bucket
            and journal.get("phase") == "bucket_retired"
            and journal.get("fence") == proof
            and "pending" in journal
            and journal["pending"] is None
            and journal.get("versions") == journal.get("retired")
            and isinstance(journal.get("versions"), list)
            and isinstance(proof, dict)
            and proof.get("location_id") == str(LEGACY_LOCATION_ID)
            and proof.get("storage_identity")
            == managed_storage_identity(endpoint, bucket)
            and type(proof.get("database_oid")) is int
            and proof["database_oid"] > 0
            and self.bucket_info(bucket) is None
        ):
            raise WorktreeError("受管原件退休身份或回执不明确；保留数据库")
        prepared.legacy_original_retirement = proof

    def _bind_raster_destroy_identity(self, prepared, owner):
        self._require_destroy_preparation(prepared, "prepared")
        if (
            prepared.storage is not None
            or prepared.raster_owner is not None
            or prepared.fixture_owner is None
            or owner.data["raster"]["resources"] != self.resources
        ):
            raise WorktreeError("Raster 销毁身份不明确；保留资源")
        identity = prepared.fixture_owner.data["fixture_store"]["database"]
        oid = identity.get("database_oid")
        if (
            type(oid) is not int
            or oid <= 0
            or identity.get("database") != self.resources["database"]
            or identity.get("ownership") != self.resources["owner"]
            or identity.get("resource_root") != self.resources["root"]
        ):
            raise WorktreeError("Raster 原始数据库身份不明确；保留资源")
        from fixture_store import _raster_require_live_birth

        _raster_require_live_birth(prepared.fixture_owner, self.resources)
        self._raster_redis_live(owner)
        prepared.raster_owner = owner
        prepared.raster_database_oid = oid

    def _raster_redis_live(self, owner):
        name = self.resources["redis_name"]
        birth = owner.data["raster"].get("redis_birth") or {}
        ids = docker(
            "container",
            "ls",
            "-a",
            "--no-trunc",
            "--filter",
            f"name=^/{name}$",
            "--format",
            "{{.ID}}",
        ).splitlines()
        if len(ids) != 1 or re.fullmatch(r"[0-9a-f]{64}", ids[0]) is None:
            raise WorktreeError("Raster Redis 身份不唯一；保留资源")
        identifier = ids[0]
        observed = json.loads(docker("container", "inspect", identifier))[0]
        state = observed.get("State") or {}
        if not (
            observed.get("Id") == identifier
            and identifier == birth.get("container_id")
            and observed.get("Name") == "/" + name
            and (observed.get("Config", {}).get("Labels") or {}).get(OWNER_LABEL)
            == self.resources["owner"]
            and observed.get("Config", {}).get("Image") == "redis:7-alpine"
            and observed.get("RestartCount") == 0
            and state.get("Status") == "running"
            and state.get("Running") is True
            and isinstance(state.get("Pid"), int)
            and state["Pid"] > 0
            and bool(state.get("StartedAt"))
            and state["Pid"] == birth.get("pid")
            and state["StartedAt"] == birth.get("started_at")
        ):
            raise WorktreeError("Raster Redis 原始进程身份不匹配；保留资源")
        return observed

    def _accept_retired_raster_provider(self, prepared, owner):
        self._require_destroy_preparation(prepared, "prepared")
        if prepared.storage is not None or prepared.raster_owner is not owner:
            raise WorktreeError("Raster 存储退休状态重复")
        from raster_exclusive_provider import require_retired_provider

        require_retired_provider(owner, self.resources)
        prepared.storage = "raster_provider_retired"

    def _retire_prepared_raster_redis(self, prepared, owner):
        """Stop the exact Raster Redis without Docker stop's forced-kill fallback."""
        self._require_destroy_preparation(prepared, "prepared")
        if (
            prepared.storage != "raster_provider_retired"
            or prepared.raster_owner is not owner
            or prepared.raster_redis_retired
        ):
            raise WorktreeError("Raster Redis 退休顺序不一致；保留资源")
        if "redis_retirement" in owner.data["raster"]:
            raise WorktreeError("Raster Redis 退休已开始；保留原始证据")
        name = self.resources["redis_name"]
        observed = self._raster_redis_live(owner)
        identifier = observed["Id"]
        state = observed["State"]
        receipt = {
            "state": "signal_intent",
            "container_id": identifier,
            "started_at": state["StartedAt"],
            "pid": state["Pid"],
        }
        owner.data["raster"]["redis_retirement"] = receipt
        owner.save()
        signal_ack = docker("kill", "--signal", "SIGTERM", identifier)
        if signal_ack not in {identifier, name}:
            raise WorktreeError("Raster Redis 终止请求未确认；保留资源")
        receipt.update(
            state="signal_acknowledged", signal="SIGTERM", signal_ack=signal_ack
        )
        owner.save()
        status = docker("wait", identifier)
        if not status.isdecimal() or int(status) > 255:
            raise WorktreeError("Raster Redis 退出结果不明确；保留资源")
        receipt.update(state="wait_acknowledged", wait_exit=int(status))
        owner.save()
        if status != "0":
            raise WorktreeError("Raster Redis 未正常退出；保留资源")
        observed = json.loads(docker("container", "inspect", identifier))[0]
        state = observed.get("State") or {}
        if not (
            observed.get("Id") == identifier
            and observed.get("RestartCount") == 0
            and state.get("Status") == "exited"
            and state.get("Running") is False
            and state.get("Pid") == 0
            and state.get("ExitCode") == 0
            and state.get("OOMKilled") is False
            and not state.get("Error")
            and state.get("StartedAt") == receipt["started_at"]
            and bool(state.get("FinishedAt"))
        ):
            raise WorktreeError("Raster Redis 退出状态不明确；保留资源")
        receipt.update(
            state="remove_intent",
            finished_at=state["FinishedAt"],
            exit_state={
                "status": state["Status"],
                "running": state["Running"],
                "pid": state["Pid"],
                "exit_code": state["ExitCode"],
                "oom_killed": state["OOMKilled"],
                "error_empty": not state.get("Error"),
                "restart_count": observed["RestartCount"],
            },
        )
        owner.save()
        removed = docker("rm", identifier)
        if removed not in {identifier, name}:
            raise WorktreeError("Raster Redis 删除未确认；保留资源")
        receipt.update(state="remove_acknowledged", removed=removed)
        owner.save()
        if docker(
            "container",
            "ls",
            "-a",
            "--no-trunc",
            "--filter",
            f"id={identifier}",
            "--format",
            "{{.ID}}",
        ):
            raise WorktreeError("Raster Redis 删除未能验证；保留资源")
        receipt["state"] = "retired"
        owner.save()
        prepared.raster_redis_retired = True

    def _require_raster_redis_retired(self, prepared):
        self._require_destroy_preparation(prepared, "database_intent")
        owner = prepared.raster_owner
        birth = owner.data["raster"].get("redis_birth") or {}
        receipt = owner.data["raster"].get("redis_retirement") or {}
        identifier = birth.get("container_id")
        name = self.resources["redis_name"]
        if not (
            prepared.raster_redis_retired
            and isinstance(identifier, str)
            and re.fullmatch(r"[0-9a-f]{64}", identifier)
            and type(birth.get("pid")) is int
            and birth["pid"] > 0
            and bool(birth.get("started_at"))
            and receipt.get("state") == "retired"
            and receipt.get("container_id") == identifier
            and receipt.get("pid") == birth["pid"]
            and receipt.get("started_at") == birth["started_at"]
            and receipt.get("signal") == "SIGTERM"
            and receipt.get("signal_ack") in {identifier, name}
            and receipt.get("wait_exit") == 0
            and bool(receipt.get("finished_at"))
            and receipt.get("exit_state")
            == {
                "status": "exited",
                "running": False,
                "pid": 0,
                "exit_code": 0,
                "oom_killed": False,
                "error_empty": True,
                "restart_count": 0,
            }
            and receipt.get("removed") in {identifier, name}
        ):
            raise WorktreeError("Raster Redis 退休证明不完整；保留数据库")
        for selector in (f"id={identifier}", f"name=^/{name}$"):
            if docker(
                "container",
                "ls",
                "-a",
                "--no-trunc",
                "--filter",
                selector,
                "--format",
                "{{.ID}}",
            ):
                raise WorktreeError("Raster Redis 退休后身份被替换；保留数据库")

    def _stop_prepared_raster_fixture_source(self, prepared, owner):
        """Use the already-guarded source owner while both destruction locks remain held."""
        self._require_destroy_preparation(prepared, "prepared")
        if (
            prepared.storage != "raster_provider_retired"
            or prepared.raster_owner is not owner
            or not prepared.raster_redis_retired
            or prepared.raster_source_stopped
            or prepared.fixture_owner is None
        ):
            raise WorktreeError("Raster 原始存储退休顺序不一致；保留资源")
        from fixture_store import _raster_stop_for_destroy

        _raster_stop_for_destroy(prepared.fixture_owner, self.resources)
        prepared.raster_source_stopped = True

    def _destroy_prepared_database(self, prepared):
        self._require_destroy_preparation(prepared, "prepared")
        journal = state_path(
            self.root, self.resources["mode"], "legacy-original-retirement.json"
        )
        if journal.exists():
            proof = prepared.legacy_original_retirement
            if not isinstance(proof, dict):
                raise WorktreeError("受管原件尚未按日志退休；保留数据库")
            self._accept_legacy_original_retirement(
                prepared, read_state(journal), proof
            )
        if (
            prepared.storage == "raster_provider_retired"
            and not prepared.raster_redis_retired
        ):
            raise WorktreeError("Raster Redis 未证明退休；保留数据库")
        if prepared.storage == "raster_provider_retired":
            if not prepared.raster_source_stopped or prepared.fixture_owner is None:
                raise WorktreeError("Raster 原始存储未证明停止；保留数据库")
            from fixture_store import (
                _raster_remove_stopped_for_destroy,
                _raster_require_stopped_for_destroy,
            )

            _raster_require_stopped_for_destroy(prepared.fixture_owner, self.resources)
            prepared.raster_owner.data["raster"]["fixture_source_retirement"] = "intent"
            prepared.raster_owner.save()
            _raster_remove_stopped_for_destroy(prepared.fixture_owner, self.resources)
            prepared.raster_owner.data["raster"]["fixture_source_retirement"] = (
                "completed"
            )
            prepared.raster_owner.save()
        prepared.phase = "database_intent"
        if prepared.storage == "raster_provider_retired":
            asyncio.run(self._database("drop", raster_prepared=prepared))
        elif prepared.legacy_original_retirement is not None:
            asyncio.run(self._database("drop", legacy_prepared=prepared))
        else:
            asyncio.run(self.database("drop"))
        if prepared.raster_owner is not None:
            prepared.raster_owner.data["raster"]["images"]["state"] = "database_removed"
            prepared.raster_owner.save()
        if prepared.fixture_owner is not None:
            from fixture_suite_coordinator import finalize_store_destruction

            if prepared.raster_owner is not None:
                prepared.raster_owner.data["raster"]["fixture_source_finalization"] = (
                    "intent"
                )
                prepared.raster_owner.save()
            if prepared.storage == "raster_provider_retired":
                from fixture_store import _raster_finish_removed_for_destroy

                _raster_finish_removed_for_destroy(
                    prepared.fixture_owner, self.resources
                )
            finalize_store_destruction(prepared.fixture_owner, self.resources)
            if prepared.raster_owner is not None:
                prepared.raster_owner.data["raster"]["fixture_source_finalization"] = (
                    "completed"
                )
                prepared.raster_owner.save()
        prepared.phase = "database_removed"

    def _destroy_prepared_services(self, prepared):
        self._require_destroy_preparation(prepared, "database_removed")
        if (
            prepared.storage == "raster_provider_retired"
            and self.redis_info() is not None
        ):
            raise WorktreeError("Raster Redis 退休身份发生变化；保留角色")
        prepared.phase = "services_intent"
        inference = prepared.inference
        if inference is not None:
            inference.finish_destroy()
        asyncio.run(self.owned_runtime_role("drop"))
        self.manage_worker_roles("drop")
        if prepared.storage != "raster_provider_retired" and require_owner(
            self.resources["redis_name"], self.redis_info(), self.resources["owner"]
        ):
            self.stop_redis()
            docker("rm", self.resources["redis_name"])
            if self.redis_info() is not None:
                raise WorktreeError("Redis 删除未能验证")
        prepared.phase = "services_removed"

    def _destroy_prepared_buckets(self, prepared):
        self._require_destroy_preparation(prepared, "services_removed")
        if prepared.storage is not None:
            raise WorktreeError("存储已退休；拒绝重复 bucket 删除")
        for name in self.resources["buckets"].values():
            if not require_owner(name, self.bucket_info(name), self.resources["owner"]):
                continue
            for page in self.s3.get_paginator("list_multipart_uploads").paginate(
                Bucket=name
            ):
                for upload in page.get("Uploads", []):
                    self.s3.abort_multipart_upload(
                        Bucket=name, Key=upload["Key"], UploadId=upload["UploadId"]
                    )
            for page in self.s3.get_paginator("list_object_versions").paginate(
                Bucket=name
            ):
                objects = [
                    {"Key": item["Key"], "VersionId": item["VersionId"]}
                    for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
                ]
                if objects:
                    result = self.s3.delete_objects(
                        Bucket=name, Delete={"Objects": objects}
                    )
                    if result.get("Errors"):
                        raise WorktreeError(
                            f"bucket {name} 存在无法删除的对象；已停止清理"
                        )
            self.s3.delete_bucket(Bucket=name)
            if self.bucket_info(name) is not None:
                raise WorktreeError(f"bucket {name} 删除未能验证")
        prepared.storage = "buckets_removed"

    def _finish_prepared_destruction(self, prepared):
        self._require_destroy_preparation(prepared, "services_removed")
        if prepared.storage not in {"buckets_removed", "raster_provider_retired"}:
            raise WorktreeError("存储未证明退休；保留本地状态")
        if (
            prepared.storage == "raster_provider_retired"
            and self.redis_info() is not None
        ):
            raise WorktreeError("Raster Redis 退休身份发生变化；保留本地状态")
        for part in ("data", "tmp", "redis"):
            directory = state_path(self.root, self.resources["mode"], part)
            if directory.exists():
                shutil.rmtree(directory)
        for purpose in ("runtime", *_JOBS_ROLE_PURPOSES):
            self._owned_role_path(purpose).unlink(missing_ok=True)
        state_path(self.root, self.resources["mode"], "resources.json").unlink(
            missing_ok=True
        )
        self.manifest["postgres"].pop("database_oid", None)
        self.target_manifest["postgres"].pop("database_oid", None)
        print(
            f"[dev:worktree] 已删除本环境资源 {self.resources['confirmation']}",
            file=sys.stderr,
        )
        prepared.phase = "finished"

    def _destroy_owned(self, confirmation: str | None) -> None:
        with self._prepare_owned_destruction(confirmation) as prepared:
            self._destroy_prepared_database(prepared)
            self._destroy_prepared_services(prepared)
            self._destroy_prepared_buckets(prepared)
            self._finish_prepared_destruction(prepared)
