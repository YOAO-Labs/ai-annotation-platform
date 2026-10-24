"""Local PostgreSQL/MinIO ownership and Redis lifecycle for worktree environments."""

from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import warnings

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from worktree_env import (
    WorktreeError,
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

OWNER_LABEL = "org.aap.worktree-owner"
OWNER_TAG = "aap-worktree-owner"
VIDEO_RUNTIME_CAPABILITIES = (
    "aap_assets_v1.capture_accepted_video_manifest(uuid,uuid,uuid,jsonb)",
    "aap_assets_v1.read_accepted_video_manifest(uuid,uuid,uuid)",
    "aap_assets_v1.read_accepted_video_manifest_page(uuid,uuid,uuid,text,text,bigint,integer)",
    "aap_assets_v1.prepare_accepted_video_member(uuid,uuid,uuid,text,bigint,uuid,text,bigint,jsonb,text)",
    "aap_assets_v1.seal_accepted_video_manifest(uuid,uuid,uuid,text)",
    "aap_assets_v1.read_accepted_video_input(uuid,uuid,uuid,uuid)",
    "aap_assets_v1.read_accepted_video_input_work(uuid,uuid,uuid,uuid)",
    "aap_assets_v1.read_accepted_video_times(uuid,uuid,uuid,uuid,text,bigint,integer)",
    "aap_assets_v1.require_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint)",
    "aap_assets_v1.read_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint)",
    "aap_assets_v1.claim_accepted_video_input(uuid,uuid,integer)",
    "aap_assets_v1.heartbeat_accepted_video_input(uuid,bigint,uuid,integer)",
    "aap_assets_v1.stage_accepted_video_times(uuid,bigint,uuid,bigint,jsonb)",
    "aap_assets_v1.finish_accepted_video_input(uuid,bigint,uuid,jsonb)",
    "aap_assets_v1.fail_accepted_video_input(uuid,bigint,uuid,text)",
    "aap_assets_v1.claim_accepted_video_frame(uuid,uuid,integer)",
    "aap_assets_v1.heartbeat_accepted_video_frame(uuid,bigint,uuid,integer)",
    "aap_assets_v1.finish_accepted_video_frame(uuid,bigint,uuid,jsonb)",
    "aap_assets_v1.fail_accepted_video_frame(uuid,bigint,uuid,text)",
    "aap_assets_v1.lock_accepted_video_frame(uuid,uuid,uuid,uuid,text,bigint,jsonb,jsonb)",
    "aap_assets_v1.lock_accepted_video_input(uuid,uuid,uuid,uuid,text)",
    "aap_assets_v1._lock_accepted_video_input_pin(uuid,uuid,jsonb,uuid)",
    "aap_labeling_v1.attach_annotation_video_input_source(uuid,uuid,jsonb)",
    "aap_labeling_v1.copy_annotation_video_input_sources(uuid,uuid)",
    "aap_labeling_v1.accepted_video_input_referenced(uuid)",
)
IMAGE_MEDIA_RUNTIME_CAPABILITIES = (
    "aap_assets_v1.read_image_source(uuid,uuid,uuid)",
    "aap_assets_v1.observe_image_source(uuid,uuid,uuid,text,jsonb)",
    "aap_assets_v1.read_image_summaries(uuid,jsonb,text)",
    "aap_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean)",
    "aap_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text)",
    "aap_assets_v1.read_image_work(uuid,uuid)",
    "aap_assets_v1.retry_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean)",
    "aap_assets_v1.invalidate_image_media(uuid,uuid,uuid,jsonb)",
    "aap_assets_v1.cancel_image_media(uuid,uuid,uuid)",
    "aap_assets_v1.resolve_internal_image_source(text,uuid,text,jsonb)",
    "aap_assets_v1.schedule_image_media(uuid,text,uuid,jsonb,boolean)",
    "aap_assets_v1.read_internal_image_work(uuid)",
    "aap_assets_v1.scan_image_backfill(jsonb,text,uuid,integer)",
    "aap_assets_v1.read_image_usage()",
    "aap_assets_v1.claim_image_media(uuid,uuid,integer,text[])",
    "aap_assets_v1.heartbeat_image_media(uuid,bigint,uuid,integer)",
    "aap_assets_v1.finish_image_media(uuid,bigint,uuid,jsonb)",
    "aap_assets_v1.fail_image_media(uuid,bigint,uuid,text,text)",
    "aap_assets_v1.advance_image_backfill(uuid,bigint,uuid,integer,jsonb)",
    "aap_assets_v1.reconcile_image_media(jsonb,integer)",
    "aap_assets_v1.claim_image_garbage(uuid,integer,integer)",
    "aap_assets_v1.complete_image_garbage(uuid,uuid)",
)
IMAGE_ACCEPTED_INPUT_RUNTIME_CAPABILITIES = (
    "aap_assets_v1.find_accepted_image_input(uuid,uuid,uuid)",
    "aap_assets_v1.reserve_accepted_image_input(uuid,uuid,uuid,text,jsonb)",
    "aap_assets_v1.read_accepted_image_input(uuid,uuid,uuid)",
    "aap_assets_v1.read_accepted_image_input_work(uuid,uuid,uuid)",
    "aap_assets_v1.claim_accepted_image_input(uuid,uuid,integer)",
    "aap_assets_v1.heartbeat_accepted_image_input(uuid,bigint,uuid,integer)",
    "aap_assets_v1.finish_accepted_image_input(uuid,bigint,uuid,jsonb)",
    "aap_assets_v1.fail_accepted_image_input(uuid,bigint,uuid,text)",
    "aap_assets_v1.lock_accepted_image_input(uuid,uuid,uuid)",
)
IMAGE_MANIFEST_RUNTIME_CAPABILITIES = (
    "aap_assets_v1.capture_accepted_image_input_manifest(uuid,uuid,uuid,jsonb)",
    "aap_assets_v1.read_accepted_image_input_manifest(uuid,uuid,uuid)",
    "aap_assets_v1.read_accepted_image_input_manifest_page(uuid,uuid,uuid,text,text,bigint,integer)",
    "aap_assets_v1.prepare_accepted_image_input_manifest_member(uuid,uuid,uuid,text,bigint,uuid,text,jsonb,text)",
    "aap_assets_v1.seal_accepted_image_input_manifest(uuid,uuid,uuid,text)",
)
IMAGE_LABELING_INPUT_RUNTIME_CAPABILITIES = (
    "aap_labeling_v1.attach_annotation_input_source(uuid,uuid,text)",
    "aap_labeling_v1.copy_annotation_input_sources(uuid,uuid)",
)
IMAGE_ACCEPTED_RUNTIME_CAPABILITIES = (
    *IMAGE_ACCEPTED_INPUT_RUNTIME_CAPABILITIES,
    *IMAGE_MANIFEST_RUNTIME_CAPABILITIES,
    *IMAGE_LABELING_INPUT_RUNTIME_CAPABILITIES,
)


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
        list(scripts.walk_revisions())  # Validate missing ancestors/cycles too.
        heads = list(scripts.get_heads())
    validate_revisions(revisions, heads, [], skip=False)
    return revisions, heads


NOTIFICATION_DELIVERY_READ_CAPABILITIES = (
    "aap_notifications_v1.read_notification(uuid)",
    "aap_notifications_v1.find_terminal_notification(uuid,text,uuid)",
    "aap_notifications_v1.delivery_job_scopes(uuid[])",
    "aap_notifications_v1.delivery_member_pairs(jsonb)",
    "aap_notifications_v1.delivery_reviewer_pairs(jsonb)",
    "aap_notifications_v1.delivery_owner_pairs(jsonb)",
    "aap_notifications_v1.delivery_super_pairs(jsonb)",
)

NOTIFICATION_RUNTIME_CAPABILITIES = (
    "aap_notifications_v1.preference_command(jsonb)",
    "aap_notifications_v1.append_notification(jsonb)",
    "aap_notifications_v1.append_job_terminal_notification(jsonb)",
    "aap_notifications_v1.read_state_command(jsonb)",
    "aap_notifications_v1.delete_command(pg_catalog.jsonb)",
    "aap_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)",
    "aap_notifications_v1.inbox_total_count(uuid,text)",
    "aap_notifications_v1.inbox_unread_count(uuid,text)",
    *NOTIFICATION_DELIVERY_READ_CAPABILITIES,
)


class LocalResources:
    def __init__(self, resources: dict, settings, *, code_root: Path):
        self.root = Path(resources["root"])
        expected = topology(self.root, load_identity(self.root), resources["mode"])
        if resources != expected:
            raise WorktreeError("资源拓扑与本工作树 identity 不一致")
        self.resources = resources
        self.settings = settings
        self.code_root = code_root.resolve()
        self.touched = False
        # Validate every source before connecting or writing. Old manifests remain
        # inspectable by doctor/stop/destroy; only an actual child process requires
        # the new dedicated input bucket, after explicit prepare has upgraded it.
        self._environment(1, require_inputs_bucket=False)
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
        if path.exists():
            saved = read_state(path)
            if saved != self.manifest:
                legacy_8 = _without_buckets(
                    self.manifest, "MINIO_TRAINING_BUCKET", "MINIO_INPUTS_BUCKET"
                )
                legacy_9 = _without_buckets(self.manifest, "MINIO_INPUTS_BUCKET")
                if saved != legacy_8 and saved != legacy_9:
                    raise WorktreeError(
                        "基础设施目标与已有资源清单不一致；先用原配置销毁旧环境"
                    )
                # Inspection/destruction use exactly the saved owned inventory.
                # Only explicit preparation upgrades this known old topology.
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
                retries={"max_attempts": 1},
            ),
        )

    def _environment(self, port: int, *, require_inputs_bucket: bool = True) -> dict:
        return isolated_environment(
            self.resources,
            database_url=self.settings.database_url,
            migration_database_url=self.settings.effective_migration_database_url,
            minio_endpoint=self.settings.minio_endpoint,
            minio_use_ssl=self.settings.minio_use_ssl,
            redis_port=port,
            inherited=os.environ,
            require_inputs_bucket=require_inputs_bucket,
        )

    def environment(self) -> dict:
        observed = self.redis_info()
        require_owner(self.resources["redis_name"], observed, self.resources["owner"])
        if not observed or not observed["running"] or not observed["port"]:
            raise WorktreeError("独立 Redis 未运行；请先 init")
        return self._environment(observed["port"])

    async def database(self, action: str = "inspect") -> dict | None:
        # Same session-lock / AUTOCOMMIT pattern as apps/api/scripts/prepare_e2e_db.py;
        # additionally require a database ownership marker rather than adopting by name.
        engine = create_async_engine(
            self.url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"timeout": 5, "command_timeout": 10},
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
                                        "SELECT shobj_description(oid, 'pg_database') AS owner, "
                                        "(SELECT count(*) FROM pg_stat_activity WHERE datname = :name) AS sessions "
                                        "FROM pg_database WHERE datname = :name"
                                    ),
                                    {"name": name},
                                )
                            )
                            .mappings()
                            .first()
                        )
                        return dict(row) if row else None

                    observed = await read()
                    if action == "inspect":
                        return observed
                    present = require_owner(name, observed, self.resources["owner"])
                    quoted = engine.dialect.identifier_preparer.quote_identifier(name)
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
                    if action == "drop" and verified is not None:
                        raise WorktreeError("数据库删除未能验证")
                    return verified
                finally:
                    if action != "inspect":
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(hashtext(:name))"),
                            {"name": name},
                        )
        finally:
            await engine.dispose()

    async def verify_database_server(self, owner_connection) -> None:
        """Prove both logins share one server before inspecting or changing resources."""
        runtime = make_url(self.settings.database_url).set(database="postgres")
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
            "redis": self.redis_info(),
            "buckets": {
                name: self.bucket_info(name)
                for name in self.resources["buckets"].values()
            },
        }

    def start_redis(self) -> None:
        import redis

        name = self.resources["redis_name"]
        observed = self.redis_info()
        present = require_owner(name, observed, self.resources["owner"])
        if not present:
            data = state_path(self.root, self.resources["mode"], "redis")
            data.mkdir(mode=0o700, parents=True, exist_ok=True)
            docker(
                "run",
                "-d",
                "--name",
                name,
                "--label",
                f"{OWNER_LABEL}={self.resources['owner']}",
                "--publish",
                "127.0.0.1::6379",
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
        """Keep the broad legacy runtime usable; module clients use owner grants."""
        runtime = make_url(self.settings.database_url)
        if runtime.username == self.url.username:
            return
        engine = create_async_engine(self.url, poolclass=NullPool, hide_parameters=True)
        try:
            role = engine.dialect.identifier_preparer.quote_identifier(runtime.username)
            database = engine.dialect.identifier_preparer.quote_identifier(
                self.resources["database"]
            )
            video_function_grant = ", ".join(VIDEO_RUNTIME_CAPABILITIES)
            image_media_function_grant = ", ".join(IMAGE_MEDIA_RUNTIME_CAPABILITIES)
            image_accepted_function_grant = ", ".join(
                IMAGE_ACCEPTED_RUNTIME_CAPABILITIES
            )
            notification_function_grant = ", ".join(NOTIFICATION_RUNTIME_CAPABILITIES)
            async with engine.begin() as connection:
                await connection.execute(
                    text("SELECT set_config('aap.application_role', :role, true)"),
                    {"role": runtime.username},
                )
                for sql in (
                    f"GRANT CONNECT ON DATABASE {database} TO {role}",
                    f"GRANT USAGE ON SCHEMA public TO {role}",
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
                    f"GRANT USAGE ON SCHEMA aap_workspace_v1, aap_assets_v1, aap_labeling_v1, aap_jobs_v1 TO {role}",
                    "GRANT SELECT ON aap_assets_v1.spatial_item_references, "
                    f"aap_assets_v1.spatial_scene_references TO {role}",
                    f"GRANT EXECUTE ON FUNCTION aap_workspace_v1.lock_project_reference(uuid), "
                    "aap_workspace_v1.lock_project_manager(uuid, uuid), "
                    "aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb), "
                    "aap_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid), "
                    "aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb), "
                    "aap_assets_v1.lock_dataset_reference(uuid), "
                    "aap_assets_v1.lock_current_association(uuid, uuid), "
                    "aap_assets_v1.associate_project(uuid, uuid, uuid), "
                    "aap_assets_v1.lock_scene_source_references(uuid[]), "
                    "aap_assets_v1.delete_dataset(uuid, uuid, uuid, text, jsonb), "
                    "aap_assets_v1.delete_dataset_item(uuid, uuid, uuid, uuid, text, jsonb), "
                    "aap_assets_v1.reserve_dataset_upload(uuid, uuid, uuid, text, text), "
                    "aap_assets_v1.lock_dataset_upload(uuid, uuid, uuid), "
                    "aap_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text), "
                    "aap_assets_v1.complete_dataset_upload(uuid, uuid, uuid, uuid, bigint, text, integer, integer), "
                    "aap_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text), "
                    "aap_assets_v1.pending_storage_deletions(uuid, integer), "
                    "aap_assets_v1.complete_storage_deletion(uuid, uuid), "
                    "aap_assets_v1.lock_legacy_dataset_upload(uuid, uuid, uuid, uuid), "
                    "aap_assets_v1.read_legacy_dataset_upload(uuid, uuid, uuid, uuid), "
                    "aap_assets_v1.complete_legacy_dataset_upload(uuid, uuid, uuid, uuid, text, bigint, text, integer, integer), "
                    "aap_assets_v1.list_catalog_datasets(uuid, text, text, boolean, integer, bigint), "
                    "aap_assets_v1.get_catalog_dataset(uuid, uuid), "
                    "aap_assets_v1.list_catalog_items(uuid, uuid, integer, bigint), "
                    "aap_assets_v1.create_catalog_dataset(uuid, uuid, jsonb), "
                    "aap_assets_v1.update_catalog_dataset(uuid, uuid, uuid, jsonb), "
                    "aap_assets_v1.list_dataset_projects(uuid, uuid), "
                    "aap_assets_v1.list_project_datasets(uuid, uuid), "
                    "aap_assets_v1.guard_spatial_actor(uuid,text), "
                    "aap_assets_v1.list_spatial_scenes(uuid,uuid,timestamp with time zone,uuid,integer), "
                    "aap_assets_v1.get_spatial_scene(uuid,uuid), "
                    "aap_assets_v1.read_spatial_layout(uuid,uuid,uuid,uuid,integer), "
                    "aap_assets_v1.read_spatial_items(uuid,uuid[],uuid,integer), "
                    "aap_assets_v1.read_spatial_poses(uuid,uuid,integer,integer), "
                    "aap_assets_v1.read_spatial_pose_pairs(jsonb), "
                    "aap_assets_v1.read_spatial_calibrations(uuid[],boolean,uuid,integer,integer), "
                    "aap_assets_v1.lock_spatial_calibration_facts(uuid,uuid,text,uuid,jsonb), "
                    "aap_assets_v1.mutate_spatial_scene(uuid,uuid,uuid,jsonb,jsonb), "
                    "aap_assets_v1.apply_spatial_layout(uuid,uuid,uuid,text,boolean,jsonb,jsonb), "
                    "aap_assets_v1.read_spatial_layout_receipt(uuid,uuid,uuid,text,boolean), "
                    "aap_assets_v1.upsert_spatial_poses(uuid,uuid,jsonb), "
                    "aap_assets_v1.create_spatial_scene(uuid,uuid,uuid,text,text,jsonb,uuid), "
                    "aap_assets_v1.assign_spatial_items(uuid,uuid,jsonb), "
                    "aap_assets_v1.set_spatial_scene_source(uuid,uuid,text,jsonb), "
                    "aap_assets_v1.update_spatial_calibration(uuid,uuid,uuid,text,integer,text,jsonb,text,text,jsonb,jsonb), "
                    "aap_assets_v1.read_pointcloud_preparation(uuid,uuid,integer), "
                    "aap_assets_v1.apply_pointcloud_preparation(uuid,jsonb,jsonb,jsonb), "
                    "aap_assets_v1.read_spatial_camera_target(uuid,uuid,text), "
                    "aap_assets_v1.record_spatial_calibration_provenance(uuid,uuid,jsonb), "
                    "aap_assets_v1.set_spatial_dataset_axis(uuid,uuid,text), "
                    "aap_assets_v1.prepare_dataset_import(uuid, uuid, uuid), "
                    "aap_assets_v1.accept_dataset_import(uuid, uuid, uuid, uuid, jsonb, jsonb, jsonb), "
                    "aap_assets_v1.cancel_dataset_import(uuid, uuid), "
                    "aap_assets_v1.claim_dataset_import_delivery(uuid, integer), "
                    "aap_assets_v1.dataset_import_delivery_target_is_current(text, uuid), "
                    "aap_assets_v1.complete_dataset_import_delivery(uuid, uuid), "
                    "aap_assets_v1.begin_dataset_ingest(uuid, uuid, uuid, jsonb), "
                    "aap_assets_v1.prepare_dataset_ingest_batch(uuid, uuid, uuid, uuid, jsonb), "
                    "aap_assets_v1.complete_dataset_ingest_batch(uuid, uuid, uuid, uuid, jsonb), "
                    "aap_assets_v1.read_dataset_ingest_layout(uuid, uuid, uuid, uuid, integer), "
                    "aap_assets_v1.finish_dataset_ingest(uuid, uuid, uuid, jsonb), "
                    "aap_assets_v1.list_storage_connections(uuid, timestamptz, uuid, integer), "
                    "aap_assets_v1.read_storage_connection(uuid, uuid), "
                    "aap_assets_v1.read_storage_connection_policy(uuid, jsonb), "
                    "aap_assets_v1.capture_storage_connection(uuid, uuid, text), "
                    "aap_assets_v1.read_storage_connection_receipt(uuid, uuid, text, uuid, jsonb), "
                    "aap_assets_v1.create_storage_connection(uuid, uuid, jsonb, jsonb, text, text, jsonb, jsonb), "
                    "aap_assets_v1.update_storage_connection(uuid, uuid, uuid, jsonb, jsonb, text, text, text, jsonb, jsonb), "
                    "aap_assets_v1.delete_storage_connection(uuid, uuid, uuid, jsonb), "
                    "aap_assets_v1.record_storage_connection_test(uuid, uuid, uuid, text, text, jsonb, jsonb, jsonb), "
                    "aap_assets_v1.create_operator_image_dataset(uuid, uuid, text, text), "
                    "aap_assets_v1.ensure_operator_nuscenes_dataset(uuid, uuid, jsonb), "
                    "aap_assets_v1.append_operator_items(uuid, uuid, jsonb), "
                    "aap_assets_v1.patch_operator_items(uuid, uuid, jsonb), "
                    "aap_assets_v1.recount_operator_items(uuid, uuid), "
                    "aap_assets_v1.read_operator_items(uuid, uuid, integer), "
                    "aap_assets_v1.read_media_source(uuid, uuid, uuid), "
                    "aap_assets_v1.read_media_summaries(uuid[]), "
                    "aap_assets_v1.ensure_video_media(uuid, uuid, uuid, jsonb), "
                    "aap_assets_v1.observe_media_source(uuid, uuid, uuid, text, jsonb), "
                    "aap_assets_v1.request_video_media(uuid, uuid, uuid, jsonb, boolean), "
                    "aap_assets_v1.read_video_timetable(uuid, uuid, uuid, integer, integer, integer, integer), "
                    "aap_assets_v1.read_video_samples(uuid, uuid, uuid, integer, integer, integer), "
                    "aap_assets_v1.read_video_failures(uuid, integer, bigint), "
                    "aap_assets_v1.retry_video_media(uuid, uuid, jsonb), "
                    "aap_assets_v1.invalidate_video_media(uuid, uuid, uuid, jsonb), "
                    "aap_assets_v1.cancel_video_media(uuid, uuid, uuid), "
                    "aap_assets_v1.read_media_preferences(), "
                    "aap_assets_v1.resolve_internal_media_source(uuid, text, jsonb), "
                    "aap_assets_v1.schedule_video_media(uuid, uuid, jsonb, boolean), "
                    "aap_assets_v1.read_video_work(uuid, uuid), "
                    "aap_assets_v1.scan_video_backfill(jsonb, uuid, integer), "
                    "aap_assets_v1.read_video_media_totals(uuid[]), "
                    "aap_assets_v1.read_internal_video_frame(uuid, integer, integer, text), "
                    "aap_assets_v1.claim_video_media(uuid, uuid, integer, text[]), "
                    "aap_assets_v1.heartbeat_video_media(uuid, bigint, uuid, integer), "
                    "aap_assets_v1.stage_video_timetable(uuid, bigint, uuid, jsonb), "
                    "aap_assets_v1.finish_video_media(uuid, bigint, uuid, jsonb), "
                    "aap_assets_v1.fail_video_media(uuid, bigint, uuid, text, text), "
                    "aap_assets_v1.expire_video_media(integer, integer, integer), "
                    "aap_assets_v1.claim_video_media_garbage(uuid, integer, integer), "
                    "aap_assets_v1.complete_video_media_garbage(uuid, uuid), "
                    "aap_assets_v1.advance_video_backfill(uuid, bigint, uuid, integer), "
                    "aap_assets_v1.read_video_media_usage(), "
                    "aap_assets_v1.claim_dataset_source_ingest_delivery(uuid, integer), "
                    "aap_assets_v1.complete_dataset_source_ingest_delivery(uuid, uuid), "
                    "aap_labeling_v1.read_media_visible_video_item(uuid, uuid), "
                    "aap_labeling_v1.read_mask_write_epochs(jsonb), "
                    "aap_labeling_v1.begin_mask_content_write(jsonb), "
                    "aap_labeling_v1.lock_mask_write_epochs(jsonb), "
                    "aap_labeling_v1.register_tracker_running_mask_root(jsonb), "
                    "aap_labeling_v1.claim_mask_gc_sweep(jsonb), "
                    "aap_labeling_v1.stage_mask_gc_page(jsonb), "
                    "aap_labeling_v1.claim_mask_gc_deletes(jsonb), "
                    "aap_labeling_v1.finish_mask_gc_delete(jsonb), "
                    "aap_labeling_v1.read_mask_gc_progress(jsonb), "
                    "aap_labeling_v1.inspect_mask_gc_page(jsonb), "
                    "aap_labeling_v1.admit_prediction_retry(jsonb), "
                    "aap_labeling_v1.read_prediction_retry(jsonb), "
                    "aap_labeling_v1.finish_prediction_retry_write(jsonb), "
                    "aap_labeling_v1.finish_prediction_retry_failure(jsonb), "
                    "aap_jobs_v1.claim_prediction_retry_deliveries(jsonb), "
                    "aap_jobs_v1.finish_prediction_retry_delivery(jsonb), "
                    "aap_labeling_v1.begin_batch_result(jsonb), "
                    "aap_labeling_v1.put_batch_result_part(jsonb), "
                    "aap_labeling_v1.seal_batch_result(jsonb), "
                    "aap_labeling_v1.read_batch_result(jsonb), "
                    "aap_labeling_v1.lock_batch_result_write(jsonb), "
                    "aap_labeling_v1.finish_batch_result_write(jsonb), "
                    "aap_labeling_v1.finish_batch_result_failure(jsonb), "
                    "aap_jobs_v1.claim_batch_result_execution(jsonb), "
                    "aap_jobs_v1.heartbeat_batch_result_execution(jsonb), "
                    "aap_jobs_v1.release_batch_result_execution(jsonb), "
                    "aap_jobs_v1.stage_batch_result_members(jsonb), "
                    "aap_jobs_v1.start_batch_result_execution(jsonb), "
                    "aap_jobs_v1.read_batch_result_execution(jsonb), "
                    "aap_jobs_v1.mark_batch_result_overwrite(jsonb), "
                    "aap_jobs_v1.skip_batch_result_member(jsonb), "
                    "aap_jobs_v1.finish_batch_result_execution(jsonb), "
                    "aap_jobs_v1.start_batch_result_member(jsonb), "
                    "aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid), "
                    "aap_labeling_v1.accept_dataset_task_creation(uuid, uuid, uuid, uuid, uuid, uuid, text, integer), "
                    "aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb), "
                    "aap_labeling_v1.read_dataset_task_creation(uuid), "
                    "aap_labeling_v1.advance_dataset_task_creation(uuid, integer), "
                    "aap_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid), "
                    "aap_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid), "
                    "aap_jobs_v1.claim_task_creation_job(uuid, text), "
                    "aap_jobs_v1.finish_task_creation_job(uuid, uuid), "
                    "aap_jobs_v1.fail_task_creation_job(uuid, uuid, text), "
                    f"aap_jobs_v1.cancel_task_creation_job(uuid, uuid) TO {role}",
                    f"GRANT EXECUTE ON FUNCTION {video_function_grant} TO {role}",
                    f"GRANT EXECUTE ON FUNCTION {image_media_function_grant} TO {role}",
                    f"GRANT EXECUTE ON FUNCTION {image_accepted_function_grant} TO {role}",
                ):
                    await connection.exec_driver_sql(sql)
                for sql in (
                    f"GRANT USAGE ON SCHEMA aap_notifications_v1 TO {role}",
                    f"GRANT EXECUTE ON FUNCTION {notification_function_grant} TO {role}",
                    f"""REVOKE ALL ON TABLE public.notifications, public.notification_preferences,
                        public.notification_delivery_outbox FROM {role}, PUBLIC""",
                    f"""REVOKE ALL (id, user_id, type, target_type, target_id, payload, read_at, created_at)
                        ON TABLE public.notifications FROM {role}, PUBLIC""",
                    f"""REVOKE ALL (user_id, type, channels, updated_at)
                        ON TABLE public.notification_preferences FROM {role}, PUBLIC""",
                    f"""REVOKE ALL (notification_id, operation_id, recipient_id, notification_type,
                        state, attempt, available_at, worker_id, lease_id, lease_expires_at,
                        last_reason, created_at, delivered_at)
                        ON TABLE public.notification_delivery_outbox FROM {role}, PUBLIC""",
                    f"""REVOKE ALL ON TABLE aap_notifications_v1.notification_facts
                        FROM {role}, PUBLIC""",
                    f"""REVOKE ALL (id, user_id, type, target_type, target_id, payload, read_at, created_at)
                        ON TABLE aap_notifications_v1.notification_facts FROM {role}, PUBLIC""",
                    f"""REVOKE EXECUTE ON FUNCTION aap_notifications_v1._visible_inbox_rows(uuid,text)
                        FROM {role}, PUBLIC""",
                    """DO $notification_private_acl$
                    DECLARE
                        v_runtime pg_catalog.name := current_setting('aap.application_role');
                        v_relation pg_catalog.regclass;
                    BEGIN
                        FOREACH v_relation IN ARRAY ARRAY[
                            'public.notifications'::pg_catalog.regclass,
                            'public.notification_preferences'::pg_catalog.regclass,
                            'public.notification_delivery_outbox'::pg_catalog.regclass
                        ] LOOP
                            IF pg_catalog.has_table_privilege(v_runtime, v_relation,
                                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                               OR pg_catalog.has_any_column_privilege(v_runtime, v_relation,
                                    'SELECT,INSERT,UPDATE,REFERENCES') THEN
                                RAISE EXCEPTION 'The runtime role still has direct Notifications storage access';
                            END IF;
                        END LOOP;
                        IF pg_catalog.has_table_privilege(v_runtime,
                                'aap_notifications_v1.notification_facts', 'SELECT')
                           OR pg_catalog.has_any_column_privilege(v_runtime,
                                'aap_notifications_v1.notification_facts', 'SELECT')
                           OR pg_catalog.has_table_privilege(v_runtime,
                                'aap_notifications_v1.notification_facts',
                                'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                           OR pg_catalog.has_any_column_privilege(v_runtime,
                                'aap_notifications_v1.notification_facts', 'INSERT,UPDATE,REFERENCES') THEN
                            RAISE EXCEPTION 'The runtime role can read Notifications facts directly';
                        END IF;
                        IF NOT pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1.read_notification(uuid)', 'EXECUTE')
                           OR NOT pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1.find_terminal_notification(uuid,text,uuid)', 'EXECUTE')
                           OR NOT pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1.delivery_job_scopes(uuid[])', 'EXECUTE')
                           OR NOT pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1.delivery_member_pairs(jsonb)', 'EXECUTE')
                           OR NOT pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1.delivery_reviewer_pairs(jsonb)', 'EXECUTE')
                           OR NOT pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1.delivery_owner_pairs(jsonb)', 'EXECUTE')
                           OR NOT pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1.delivery_super_pairs(jsonb)', 'EXECUTE')
                           OR NOT pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1.inbox_page(uuid,text,boolean,integer,integer)', 'EXECUTE')
                           OR NOT pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1.inbox_total_count(uuid,text)', 'EXECUTE')
                           OR NOT pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1.inbox_unread_count(uuid,text)', 'EXECUTE')
                           OR pg_catalog.has_function_privilege(v_runtime,
                                'aap_notifications_v1._visible_inbox_rows(uuid,text)', 'EXECUTE') THEN
                            RAISE EXCEPTION 'The runtime role is missing fixed Notifications reads';
                        END IF;
                    END;
                    $notification_private_acl$;""",
                ):
                    await connection.exec_driver_sql(sql)
        finally:
            await engine.dispose()

    def prepare(self, *, skip_migrations: bool = False) -> None:
        self.upgrade_training_bucket()
        self.upgrade_inputs_bucket()
        revisions, heads = migration_graph(self.code_root)
        inventory = self.inspect()
        validate_inventory(self.resources, inventory)
        current = asyncio.run(self.revisions()) if inventory["database"] else []
        validate_revisions(revisions, heads, current, skip=skip_migrations)
        atomic_json(
            state_path(self.root, self.resources["mode"], "resources.json"),
            self.manifest,
        )
        self.touched = True
        asyncio.run(self.database("create"))
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
        self.start_redis()
        for part in ("data", "tmp"):
            state_path(self.root, self.resources["mode"], part).mkdir(
                mode=0o700, parents=True, exist_ok=True
            )
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
                # SQLAlchemy exceptions can contain credentials/parameters. Do not relay raw output.
                raise WorktreeError(
                    f"独立数据库迁移失败，退出码 {result.returncode}；共享数据库未被迁移"
                )
        validate_revisions(revisions, heads, asyncio.run(self.revisions()), skip=True)
        asyncio.run(self.grant_runtime())
        print(
            f"[dev:worktree] {self.resources['database']} 已在 head {heads[0]}",
            file=sys.stderr,
        )

    def upgrade_training_bucket(self) -> None:
        """Called only under the mode lock; retain old data on partial failure."""
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
                # This invocation received a successful creation response. Compensate
                # only that new, still untagged empty bucket; never delete its objects.
                current = self.bucket_info(name)
                if current is not None and current.get("owner") is None:
                    try:
                        self.s3.delete_bucket(Bucket=name)
                    except Exception:
                        pass  # Keep the journal for explicit recovery after a crash.
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
        if not require_owner(bucket, self.bucket_info(bucket), self.resources["owner"]):
            raise WorktreeError(f"输入 bucket {bucket} 尚未归属于本环境")
        state = self.s3.get_bucket_versioning(Bucket=bucket)
        if state.get("Status") != "Enabled":
            self.s3.put_bucket_versioning(
                Bucket=bucket,
                VersioningConfiguration={"Status": "Enabled"},
            )
            state = self.s3.get_bucket_versioning(Bucket=bucket)
        if state.get("Status") != "Enabled":
            raise WorktreeError(f"输入 bucket {bucket} 必须启用 versioning")

    def upgrade_inputs_bucket(self) -> None:
        """Explicit prepare step: upgrade a legacy 8/9-bucket mode to 10 buckets."""
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

    def destroy(self, confirmation: str | None) -> None:
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
        # Validate every resource before the first destructive operation. Each backend
        # then rechecks ownership at the write boundary. Never use DROP ... FORCE.
        asyncio.run(self.database("drop"))
        if require_owner(
            self.resources["redis_name"], self.redis_info(), self.resources["owner"]
        ):
            self.stop_redis()
            docker("rm", self.resources["redis_name"])
            if self.redis_info() is not None:
                raise WorktreeError("Redis 删除未能验证")
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
        for part in ("data", "tmp", "redis"):
            directory = state_path(self.root, self.resources["mode"], part)
            if directory.exists():
                shutil.rmtree(directory)
        state_path(self.root, self.resources["mode"], "resources.json").unlink(
            missing_ok=True
        )
        print(
            f"[dev:worktree] 已删除本环境资源 {self.resources['confirmation']}",
            file=sys.stderr,
        )
