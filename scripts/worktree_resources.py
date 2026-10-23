"""Local PostgreSQL/MinIO ownership and Redis lifecycle for worktree environments."""

from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import re
import stat
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
IDENTITY_RUNTIME_CAPABILITIES = (
    "aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])",
    "aap_identity_v1.authentication_policy()",
    "aap_identity_v1.read_provisioning_account(text)",
    "aap_identity_v1.create_provisioning_account(jsonb)",
    "aap_identity_v1.create_seed_persona_account(jsonb)",
    "aap_identity_v1.read_scale_seed_super_admin()",
    "aap_identity_v1.read_scale_seed_annotator(uuid[])",
    "aap_identity_v1.read_actor_context(uuid,text)",
    "aap_identity_v1.read_actor_facts(uuid)",
    "aap_identity_v1.lock_accounts_share(uuid[],boolean)",
    "aap_identity_v1.lock_accounts_update(uuid[],boolean)",
    "aap_identity_v1.lock_accounts_key_share(uuid[],boolean)",
    "aap_identity_v1.lock_super_admin_invariant()",
    "aap_identity_v1.super_admin_status(uuid)",
    "aap_identity_briefs_v1.briefs_policy()",
    "aap_identity_briefs_v1.execute_briefs_command(jsonb)",
    "aap_identity_deactivation_v1.deactivation_policy()",
    "aap_identity_deactivation_v1.execute_deactivation_command(jsonb)",
    "aap_identity_deactivation_v1.due_policy()",
    "aap_identity_deactivation_v1.process_due_deactivations(jsonb)",
    "aap_notifications_v1.identity_delivery_policy()",
    "aap_notifications_v1.claim_identity_deliveries(jsonb)",
    "aap_notifications_v1.finish_identity_delivery(jsonb)",
    "aap_identity_v1.read_jwt_principal(uuid,uuid,bigint,timestamptz,boolean)",
    "aap_identity_v1.read_api_key_candidates(text,uuid,integer)",
    "aap_identity_v1.confirm_api_key_principal(uuid,text)",
    "aap_identity_v1.get_credential_generation(uuid)",
    "aap_identity_v1.advance_credential_generation(uuid)",
    "aap_identity_v1.revoke_jti(uuid,timestamptz)",
    "aap_identity_v1.is_jti_revoked(uuid)",
    "aap_identity_v1.retire_account_credentials(uuid)",
    "aap_identity_v1.lock_current_principal(uuid,text,uuid,bigint,timestamptz,text[])",
    "aap_identity_keys_v1.key_policy()",
    "aap_identity_keys_v1.execute_key_command(jsonb)",
    "aap_identity_sessions_v1.session_policy()",
    "aap_identity_sessions_v1.read_login_candidate(text)",
    "aap_identity_sessions_v1.execute_session_command(jsonb)",
    "aap_identity_directory_v1.directory_policy()",
    "aap_identity_directory_v1.execute_directory_command(jsonb)",
    "aap_identity_admin_passwords_v1.admin_password_policy()",
    "aap_identity_admin_passwords_v1.execute_admin_password_command(jsonb)",
    "aap_identity_platform_roles_v1.platform_role_policy()",
    "aap_identity_platform_roles_v1.execute_platform_role_command(jsonb)",
    "aap_identity_suspensions_v1.suspension_policy()",
    "aap_identity_suspensions_v1.execute_suspension_command(jsonb)",
    "aap_identity_deletions_v1.deletion_policy()",
    "aap_identity_deletions_v1.execute_deletion_command(jsonb)",
    "aap_identity_offboarding_v1.offboarding_policy()",
    "aap_identity_offboarding_v1.prepare_offboarding_command(jsonb)",
    "aap_identity_offboarding_v1.apply_offboarding_command(jsonb)",
    "aap_identity_groups_v1.group_policy()",
    "aap_identity_groups_v1.execute_group_command(jsonb)",
    "aap_identity_invitations_v1.invitation_policy()",
    "aap_identity_invitations_v1.prepare_invitation_command(jsonb)",
    "aap_identity_invitations_v1.apply_invitation_command(jsonb)",
    "aap_identity_mail_v2.mail_policy()",
    "aap_identity_mail_v2.claim_mail(jsonb)",
    "aap_identity_mail_v2.renew_mail(jsonb)",
    "aap_identity_mail_v2.finish_mail(jsonb)",
    "aap_identity_accounts_v1.account_policy()",
    "aap_identity_accounts_v1.prepare_account_command(jsonb)",
    "aap_identity_accounts_v1.apply_account_command(jsonb)",
    "aap_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)",
    "aap_identity_recovery_v1.recovery_policy()",
    "aap_identity_recovery_v1.prepare_recovery_command(jsonb)",
    "aap_identity_recovery_v1.apply_recovery_command(jsonb)",
    "aap_identity_recovery_v1.read_registration_status(jsonb)",
    "aap_identity_recovery_v1.mail_policy()",
    "aap_identity_recovery_v1.claim_mail(jsonb)",
    "aap_identity_recovery_v1.renew_mail(jsonb)",
    "aap_identity_recovery_v1.finish_mail(jsonb)",
    "aap_settings_v1.mail_delivery_policy(jsonb)",
)
NOTIFICATION_RUNTIME_CAPABILITIES = (
    "aap_notifications_v1.preference_command(jsonb)",
    "aap_notifications_v1.append_notification(jsonb)",
    "aap_notifications_v1.append_job_terminal_notification(jsonb)",
    "aap_notifications_v1.read_state_command(jsonb)",
    "aap_notifications_v1.delete_command(pg_catalog.jsonb)",
)
AUDIT_RUNTIME_CAPABILITIES = (
    "aap_audit_v1.append_events(jsonb)",
    "aap_audit_v1.query_events(pg_catalog.jsonb)",
)
_IDENTITY_PRIVATE_RUNTIME_ACL = """
-- Preserve embedded command capabilities while denying direct Identity storage.
DO $identity_private_acl$
DECLARE
    v_runtime name := current_setting('aap.application_role');
    v_relation record;
    v_columns text;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR v_runtime=current_user
       OR pg_catalog.pg_has_role(v_runtime,current_user,'MEMBER')
       OR EXISTS (
           SELECT 1 FROM pg_catalog.pg_auth_members
           WHERE member=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=v_runtime)
       )
       OR NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=v_runtime AND rolcanlogin
             AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
       ) THEN
        RAISE EXCEPTION 'Invalid embedded Identity runtime ownership';
    END IF;
    FOR v_relation IN
        SELECT c.oid,n.nspname,c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','f') AND (
            (n.nspname='public' AND (
                c.relname IN ('users','api_keys','groups','organizations',
                    'organization_members','user_invitations',
                    'password_reset_tokens','email_verification_tokens')
                OR pg_catalog.left(c.relname,9)='identity_'
            )) OR (
                n.nspname ~ '^aap_identity(_[a-z0-9]+)*_v[0-9]+$'
                AND c.relkind <> 'v'
            )
        )
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON TABLE %I.%I FROM %I',
            v_relation.nspname,v_relation.relname,v_runtime);
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname),',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns,v_relation.nspname,v_relation.relname,v_runtime);
        END IF;
        IF pg_catalog.has_table_privilege(v_runtime,v_relation.oid,
                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_runtime,v_relation.oid,
                'SELECT,INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'Embedded runtime retains private Identity access: %.%',
                v_relation.nspname,v_relation.relname;
        END IF;
    END LOOP;
END;
$identity_private_acl$;
"""
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
INFERENCE_ROUTING_RUNTIME_CAPABILITIES = (
    "aap_inference_routing_v1.routing_policy()",
    "aap_inference_routing_v1.routing_step(text,text)",
    "aap_inference_routing_v1.scope_retirement_step(text,text)",
    "aap_inference_routing_v1.video_execution_scope_step(text,text)",
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

    def _environment(
        self,
        port: int,
        *,
        require_inputs_bucket: bool = True,
        database_url: str | None = None,
    ) -> dict:
        return isolated_environment(
            self.resources,
            database_url=database_url
            or self.runtime_url().render_as_string(hide_password=False),
            migration_database_url=self.settings.effective_migration_database_url,
            minio_endpoint=self.settings.minio_endpoint,
            minio_use_ssl=self.settings.minio_use_ssl,
            redis_port=port,
            inherited=os.environ,
            require_inputs_bucket=require_inputs_bucket,
        )

    def _runtime_credentials(self, *, required: bool = False) -> dict | None:
        path = state_path(self.root, self.resources["mode"], "runtime-credentials.json")
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
        expected = self._runtime_credential_identity()
        if (
            set(document) != {*expected, "password"}
            or any(document.get(key) != value for key, value in expected.items())
            or not isinstance(document.get("password"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", document["password"])
        ):
            raise WorktreeError("运行账号凭据与本环境或原迁移账号不一致；拒绝接管")
        return document

    def _runtime_credential_identity(self) -> dict:
        return {
            "schema": 1,
            "owner": self.resources["owner"],
            "role": self.resources["database"] + "_app",
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

    async def _runtime_role_record(self, connection, operator_oid):
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
                        "role": self._runtime_credential_identity()["role"],
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

    async def owned_runtime_role(self, action: str = "inspect") -> dict | None:
        """Keep generated LOGIN creation and deletion bound to this exact mode."""
        if action not in {"inspect", "check_create", "create", "check_drop", "drop"}:
            raise WorktreeError("无效的运行账号操作")
        credentials = self._runtime_credentials()
        shared_owner = (
            make_url(self.settings.database_url).username == self.url.username
        )
        if credentials is None and not shared_owner:
            return None
        if action in {"check_create", "create"} and not shared_owner:
            raise WorktreeError(
                "此环境已有自有运行账号；先用原配置销毁，再更换账号配置"
            )
        identity = self._runtime_credential_identity()
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

                observed = await self._runtime_role_record(connection, operator["oid"])
                if observed is not None:
                    self._validate_runtime_role(observed, credentials)
                if action in {"check_create", "create"} and observed is None:
                    if not (operator["rolsuper"] or operator["rolcreaterole"]):
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
                            state_path(
                                self.root,
                                self.resources["mode"],
                                "runtime-credentials.json",
                            ),
                            credentials,
                            exclusive=True,
                        )
                        credentials = self._runtime_credentials(required=True)
                    await connection.execute(
                        text(
                            "SELECT set_config('aap.worktree_runtime_role',:role,true),"
                            "set_config('aap.worktree_runtime_password',:password,true),"
                            "set_config('aap.worktree_runtime_owner',:owner,true)"
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
                            current_setting('aap.worktree_runtime_role'),current_setting('aap.worktree_runtime_password'));
                        EXECUTE format('COMMENT ON ROLE %I IS %L',
                            current_setting('aap.worktree_runtime_role'),current_setting('aap.worktree_runtime_owner'));
                    END;
                    $create_runtime$;""")
                    observed = await self._runtime_role_record(
                        connection, operator["oid"]
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
                            connection, operator["oid"]
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
            "runtime_role": asyncio.run(self.owned_runtime_role()),
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
        """Grant embedded capabilities and fence a distinct application role."""
        runtime = self.runtime_url()
        engine = create_async_engine(self.url, poolclass=NullPool, hide_parameters=True)
        try:
            role = engine.dialect.identifier_preparer.quote_identifier(runtime.username)
            database = engine.dialect.identifier_preparer.quote_identifier(
                self.resources["database"]
            )
            video_function_grant = ", ".join(VIDEO_RUNTIME_CAPABILITIES)
            audit_function_grant = ", ".join(AUDIT_RUNTIME_CAPABILITIES)
            identity_capabilities = IDENTITY_RUNTIME_CAPABILITIES
            if self.resources["database"].endswith(("_test", "_e2e")):
                identity_capabilities += (
                    "aap_identity_v1.replace_fixture_preferences(uuid,jsonb)",
                )
            fixture_grants = ()
            if self.resources["database"].lower().endswith(("_test", "_e2e")):
                identity_capabilities += (
                    "aap_identity_fixture_v1.create_account(uuid,text,text,text,text)",
                    "aap_identity_fixture_v1.mark_manual_inactive(uuid,timestamptz,uuid)",
                    "aap_identity_fixture_v1.create_invitations(jsonb)",
                    "aap_identity_fixture_v1.delete_accounts(boolean,text[])",
                    "aap_identity_fixture_v1.delete_reset_tokens(uuid[])",
                    "aap_identity_fixture_v1.delete_invitations(uuid[])",
                    "aap_identity_fixture_v1.delete_organization_members(uuid[])",
                )
                fixture_grants = (
                    f"GRANT USAGE ON SCHEMA aap_identity_fixture_v1 TO {role}",
                    f"GRANT SELECT ON aap_identity_fixture_v1.account_profiles TO {role}",
                    f"GRANT USAGE ON SCHEMA aap_workspace_fixture_v1 TO {role}",
                    f"GRANT SELECT ON aap_workspace_fixture_v1.templates TO {role}",
                    "GRANT EXECUTE ON FUNCTION "
                    "aap_workspace_fixture_v1.create_project(jsonb), "
                    "aap_workspace_fixture_v1.create_templates(jsonb), "
                    "aap_workspace_fixture_v1.guard_project_cleanup(uuid[]), "
                    "aap_workspace_fixture_v1.delete_projects(uuid[]), "
                    f"aap_workspace_fixture_v1.delete_templates(boolean,text[]) TO {role}",
                )
            identity_function_grant = ", ".join(identity_capabilities)
            image_media_function_grant = ", ".join(IMAGE_MEDIA_RUNTIME_CAPABILITIES)
            image_accepted_function_grant = ", ".join(
                IMAGE_ACCEPTED_RUNTIME_CAPABILITIES
            )
            inference_routing_function_grant = ", ".join(
                INFERENCE_ROUTING_RUNTIME_CAPABILITIES
            )
            notification_function_grant = ", ".join(NOTIFICATION_RUNTIME_CAPABILITIES)
            async with engine.begin() as connection:
                for sql in (
                    f"GRANT CONNECT ON DATABASE {database} TO {role}",
                    f"GRANT USAGE ON SCHEMA public TO {role}",
                    *fixture_grants,
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
                    f"REVOKE ALL ON TABLE public.mv_audit_bi_daily FROM {role}, PUBLIC",
                    "REVOKE ALL (day, action, target_type, actor_role, status_family, event_count) "
                    f"ON TABLE public.mv_audit_bi_daily FROM {role}, PUBLIC",
                    f"REVOKE ALL ON TABLE public.notifications FROM {role}, PUBLIC",
                    "REVOKE ALL (id, user_id, type, target_type, target_id, payload, read_at, created_at) "
                    f"ON TABLE public.notifications FROM {role}, PUBLIC",
                    f"REVOKE ALL ON TABLE public.notification_preferences FROM {role}, PUBLIC",
                    "REVOKE ALL (user_id, type, channels, updated_at) "
                    f"ON TABLE public.notification_preferences FROM {role}, PUBLIC",
                    f"REVOKE ALL ON TABLE public.notification_delivery_outbox FROM {role}, PUBLIC",
                    "REVOKE ALL (notification_id, operation_id, recipient_id, notification_type, "
                    "state, attempt, available_at, worker_id, lease_id, lease_expires_at, "
                    "last_reason, created_at, delivered_at) "
                    f"ON TABLE public.notification_delivery_outbox FROM {role}, PUBLIC",
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
                    f"GRANT USAGE ON SCHEMA aap_workspace_v1, aap_workspace_membership_v1, aap_workspace_owner_transfer_v1, aap_workspace_configuration_v1, aap_workspace_template_query_v1, aap_workspace_project_query_v1, aap_workspace_directory_query_v1, aap_inference_pipelines_v1, aap_inference_backend_probe_v1, aap_inference_health_observation_v1, aap_inference_health_data_v1, aap_inference_registry_ingress_v1, aap_inference_routing_v1, aap_assets_v1, aap_labeling_v1, aap_labeling_class_query_v1, aap_jobs_v1, aap_learning_v1, aap_identity_v1, aap_identity_keys_v1, aap_identity_sessions_v1, aap_identity_accounts_v1, aap_identity_recovery_v1, aap_identity_groups_v1, aap_identity_deactivation_v1, aap_identity_invitations_v1, aap_identity_mail_v2, aap_identity_briefs_v1, aap_identity_directory_v1, aap_identity_admin_passwords_v1, aap_identity_platform_roles_v1, aap_identity_suspensions_v1, aap_identity_deletions_v1, aap_identity_offboarding_v1, aap_audit_v1, aap_notifications_v1, aap_settings_v1 TO {role}",
                    "GRANT SELECT ON aap_assets_v1.spatial_item_references, "
                    f"aap_assets_v1.spatial_scene_references TO {role}",
                    "GRANT SELECT ON aap_identity_v1.account_profiles, "
                    f"aap_identity_v1.invitation_audit_targets TO {role}",
                    "REVOKE ALL ON TABLE aap_notifications_v1.notification_facts "
                    f"FROM {role}, PUBLIC",
                    "REVOKE ALL (id, user_id, type, target_type, target_id, payload, read_at, created_at) "
                    "ON TABLE aap_notifications_v1.notification_facts "
                    f"FROM {role}, PUBLIC",
                    f"GRANT SELECT ON aap_notifications_v1.notification_facts TO {role}",
                    f"REVOKE ALL ON TABLE aap_audit_v1.audit_events FROM {role}, PUBLIC",
                    "REVOKE ALL (id, actor_id, actor_email, actor_role, action, target_type, "
                    "target_id, method, path, status_code, ip, detail_json, request_id, created_at) "
                    f"ON TABLE aap_audit_v1.audit_events FROM {role}, PUBLIC",
                    f"GRANT SELECT ON aap_audit_v1.audit_events TO {role}",
                    f"REVOKE ALL ON TABLE aap_audit_v1.audit_daily_facts FROM {role}, PUBLIC",
                    "REVOKE ALL (day, action, target_type, actor_role, status_family, event_count) "
                    f"ON TABLE aap_audit_v1.audit_daily_facts FROM {role}, PUBLIC",
                    f"GRANT SELECT ON aap_audit_v1.audit_daily_facts TO {role}",
                    f"REVOKE ALL ON TABLE aap_jobs_v1.notification_scope_facts FROM {role}, PUBLIC",
                    "REVOKE ALL (id, kind, scope_project_id) "
                    f"ON TABLE aap_jobs_v1.notification_scope_facts FROM {role}, PUBLIC",
                    f"GRANT SELECT ON aap_jobs_v1.notification_scope_facts TO {role}",
                    "REVOKE EXECUTE ON FUNCTION aap_audit_v1.refresh_audit_bi_mv() "
                    f"FROM {role}, PUBLIC",
                    f"GRANT SELECT ON aap_workspace_v1.project_member_facts TO {role}",
                    "GRANT SELECT ON aap_workspace_v1.project_facts, "
                    f"aap_labeling_v1.project_catalog_counters TO {role}",
                    f"GRANT EXECUTE ON FUNCTION aap_workspace_owner_transfer_v1.owner_transfer_policy(), "
                    "aap_workspace_owner_transfer_v1.transfer_project_owner(jsonb), "
                    "aap_identity_v1.workspace_template_query_org_ids(uuid), "
                    "aap_workspace_template_query_v1.template_query_policy(), "
                    "aap_workspace_template_query_v1.template_query_step(jsonb, text, jsonb, text, text[], text), "
                    "aap_labeling_class_query_v1.class_query_policy(), "
                    "aap_labeling_class_query_v1.class_counts(uuid), "
                    "aap_labeling_class_query_v1.attribute_rows(uuid), "
                    "aap_workspace_directory_query_v1.directory_query_policy(), "
                    "aap_workspace_directory_query_v1.directory_query_step(jsonb, text, jsonb), "
                    "aap_workspace_project_query_v1.project_query_policy(), "
                    "aap_workspace_project_query_v1.project_query_step(jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamptz, timestamptz, bigint, integer), "
                    "aap_workspace_project_query_v1.project_search_scope(uuid, text), "
                    "aap_workspace_project_query_v1.project_search_member_ids(uuid, text), "
                    "aap_workspace_configuration_v1.configuration_policy(), "
                    "aap_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[]), "
                    "aap_inference_pipelines_v1.pipeline_policy(), "
                    "aap_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[]), "
                    "aap_workspace_membership_v1.membership_policy(), "
                    "aap_workspace_membership_v1.prepare_membership_command(jsonb), "
                    "aap_workspace_membership_v1.apply_membership_command(jsonb), "
                    f"aap_workspace_v1.lock_project_reference(uuid), "
                    "aap_workspace_v1.lock_project_reference_nowait(uuid), "
                    "aap_workspace_v1.read_project_row(uuid), "
                    "aap_workspace_v1.actor_project_memberships(uuid), "
                    "aap_workspace_v1.lock_project_references_for_update(uuid[]), "
                    "aap_labeling_v1.write_project_counter_projection(uuid, jsonb), "
                    "aap_labeling_v1.decrement_project_review_count(uuid), "
                    "aap_labeling_v1.decrement_project_completed_count(uuid), "
                    "aap_labeling_v1.record_project_review_approval(uuid), "
                    "aap_workspace_v1.lock_project_member_reference(uuid, uuid, boolean), "
                    "aap_workspace_v1.project_member_rows(uuid, uuid[], boolean), "
                    "aap_workspace_v1.lock_project_manager(uuid, uuid), "
                    "aap_workspace_v1.project_access_step(uuid, uuid, text), "
                    "aap_workspace_v1.lock_project_row(uuid, boolean), "
                    "aap_workspace_v1.clear_primary_backend_bindings(uuid), "
                    "aap_workspace_v1.read_import_project(text), "
                    "aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb), "
                    "aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb), "
                    "aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb), "
                    "aap_workspace_v1.write_seed_members(jsonb), "
                    "aap_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb), "
                    "aap_workspace_v1.write_seed_project_projection(uuid,jsonb), "
                    "aap_workspace_v1.pin_seed_member_assignment(uuid,timestamptz), "
                    "aap_workspace_v1.create_scale_seed_project(uuid,text), "
                    "aap_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid), "
                    "aap_workspace_v1.guard_scale_seed_cleanup(text), "
                    "aap_workspace_v1.delete_scale_seed_projects(text,uuid[]), "
                    "aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean), "
                    "aap_workspace_v1.write_point_cloud_quality_config(uuid,jsonb), "
                    "aap_inference_backend_probe_v1.backend_probe_policy(), "
                    "aap_inference_backend_probe_v1.open_backend_probe_subject(text,uuid), "
                    "aap_inference_health_observation_v1.health_observation_policy(), "
                    "aap_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision), "
                    "aap_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid), "
                    "aap_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text), "
                    "aap_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text), "
                    "aap_inference_health_data_v1.health_data_policy(), "
                    "aap_inference_health_data_v1.backend_row(uuid,boolean), "
                    "aap_inference_health_data_v1.membership_row(uuid,text,boolean), "
                    "aap_inference_health_data_v1.observation_clock(), "
                    "aap_inference_health_data_v1.lock_resource(text), "
                    "aap_inference_health_data_v1.try_promotion(), "
                    "aap_inference_health_data_v1.write_backend(uuid,text,timestamp with time zone,text,boolean,boolean), "
                    "aap_inference_health_data_v1.member_pool(uuid), "
                    "aap_inference_health_data_v1.pool_row(uuid,uuid), "
                    "aap_inference_health_data_v1.member_row(uuid), "
                    "aap_inference_health_data_v1.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text), "
                    "aap_inference_registry_ingress_v1.registry_ingress_policy(), "
                    "aap_inference_registry_ingress_v1.registry_by_url(text), "
                    "aap_inference_registry_ingress_v1.registry_candidates(text), "
                    "aap_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text), "
                    "aap_inference_registry_ingress_v1.singleton_pool(uuid), "
                    "aap_inference_registry_ingress_v1.create_singleton_pool(uuid,uuid,uuid,text,text,text), "
                    "aap_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamp with time zone,text,boolean,text), "
                    "aap_inference_registry_ingress_v1.disconnect_env_registry(uuid,text), "
                    "aap_inference_registry_ingress_v1.tag_seed_registry(uuid,text), "
                    "aap_inference_registry_ingress_v1.sync_result(uuid,text), "
                    "aap_inference_registry_ingress_v1.record_sync_result(uuid,text,integer), "
                    f"{inference_routing_function_grant}, "
                    "aap_learning_v1.registry_evaluation_reference_exists(uuid), "
                    "aap_jobs_v1.count_running_registry_predictions(uuid), "
                    "aap_jobs_v1.video_execution_step(text,text), "
                    "aap_jobs_v1.video_execution_checkpoint_step(text,text), "
                    "aap_labeling_v1.video_writer_step(text,text), "
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
                    "aap_labeling_v1.capture_video_tracker_input(uuid,uuid,integer), "
                    "aap_labeling_v1.lock_video_tracker_input_for_stage(uuid,uuid,uuid,uuid,text,text), "
                    "aap_labeling_v1.lock_video_tracker_input_for_accept(uuid,uuid,uuid,uuid,text,text), "
                    "aap_labeling_v1.attach_annotation_video_tracker_input_source(uuid,uuid,uuid,jsonb), "
                    "aap_labeling_v1.copy_annotation_video_tracker_input_sources(uuid,uuid,uuid), "
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
                    f"GRANT EXECUTE ON FUNCTION {audit_function_grant} TO {role}",
                    f"GRANT EXECUTE ON FUNCTION {identity_function_grant} TO {role}",
                    f"GRANT EXECUTE ON FUNCTION {notification_function_grant} TO {role}",
                    f"GRANT EXECUTE ON FUNCTION {image_media_function_grant} TO {role}",
                    f"GRANT EXECUTE ON FUNCTION {image_accepted_function_grant} TO {role}",
                ):
                    await connection.exec_driver_sql(sql)
                await connection.execute(
                    text(
                        "SELECT set_config('aap.application_role', :role, true), "
                        "set_config('aap.expected_database', :database, true)"
                    ),
                    {"role": runtime.username, "database": self.resources["database"]},
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
                await connection.exec_driver_sql(_IDENTITY_PRIVATE_RUNTIME_ACL)
                notification_result = await connection.execute(
                    text(
                        "SELECT pg_catalog.has_function_privilege("
                        ":role, pg_catalog.to_regprocedure("
                        "'aap_notifications_v1.preference_command(jsonb)'), 'EXECUTE') "
                        "AS preferences, pg_catalog.has_function_privilege("
                        ":role, pg_catalog.to_regprocedure("
                        "'aap_notifications_v1.append_notification(jsonb)'), 'EXECUTE') "
                        "AS append, pg_catalog.has_function_privilege("
                        ":role, pg_catalog.to_regprocedure("
                        "'aap_notifications_v1.append_job_terminal_notification(jsonb)'), 'EXECUTE') "
                        "AS terminal_append, pg_catalog.has_function_privilege("
                        ":role, pg_catalog.to_regprocedure("
                        "'aap_notifications_v1.read_state_command(jsonb)'), 'EXECUTE') "
                        "AS read_state, pg_catalog.has_function_privilege("
                        ":role, pg_catalog.to_regprocedure("
                        "'aap_notifications_v1.delete_command(pg_catalog.jsonb)'), 'EXECUTE') "
                        "AS notification_delete, (pg_catalog.has_table_privilege("
                        ":role, 'public.notifications', "
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                        "pg_catalog.has_table_privilege(:role, 'public.notification_preferences', "
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                        "pg_catalog.has_table_privilege(:role, 'public.notification_delivery_outbox', "
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')) "
                        "AS notification_private, (pg_catalog.has_any_column_privilege("
                        ":role, 'public.notifications', 'SELECT,INSERT,UPDATE,REFERENCES') OR "
                        "pg_catalog.has_any_column_privilege(:role, 'public.notification_preferences', "
                        "'SELECT,INSERT,UPDATE,REFERENCES') OR "
                        "pg_catalog.has_any_column_privilege(:role, 'public.notification_delivery_outbox', "
                        "'SELECT,INSERT,UPDATE,REFERENCES')) "
                        "AS notification_column_private, pg_catalog.has_table_privilege("
                        ":role, 'aap_notifications_v1.notification_facts', 'SELECT') "
                        "AS notification_facts, pg_catalog.has_table_privilege("
                        ":role, 'aap_notifications_v1.notification_facts', "
                        "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                        "AS notification_facts_write, pg_catalog.has_any_column_privilege("
                        ":role, 'aap_notifications_v1.notification_facts', "
                        "'INSERT,UPDATE,REFERENCES') "
                        "AS notification_facts_column_write, pg_catalog.has_function_privilege("
                        ":role, pg_catalog.to_regprocedure("
                        "'aap_audit_v1.append_events(jsonb)'), 'EXECUTE') "
                        "AS audit_append, pg_catalog.has_function_privilege("
                        ":role, pg_catalog.to_regprocedure("
                        "'aap_audit_v1.query_events(pg_catalog.jsonb)'), 'EXECUTE') "
                        "AS audit_query, pg_catalog.has_table_privilege("
                        ":role, 'aap_audit_v1.audit_events', 'SELECT') AS audit_read, "
                        "pg_catalog.has_table_privilege("
                        ":role, 'aap_audit_v1.audit_events', "
                        "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS audit_read_write, "
                        "pg_catalog.has_any_column_privilege("
                        ":role, 'aap_audit_v1.audit_events', "
                        "'INSERT,UPDATE,REFERENCES') AS audit_read_column_write, "
                        "pg_catalog.has_table_privilege("
                        ":role, 'aap_audit_v1.audit_daily_facts', 'SELECT') AS audit_daily_read, "
                        "(pg_catalog.has_table_privilege("
                        ":role, 'aap_audit_v1.audit_daily_facts', "
                        "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                        "pg_catalog.has_any_column_privilege("
                        ":role, 'aap_audit_v1.audit_daily_facts', "
                        "'INSERT,UPDATE,REFERENCES')) AS audit_daily_write, "
                        "(pg_catalog.has_table_privilege("
                        ":role, 'public.mv_audit_bi_daily', "
                        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                        "pg_catalog.has_any_column_privilege("
                        ":role, 'public.mv_audit_bi_daily', "
                        "'SELECT,INSERT,UPDATE,REFERENCES')) AS audit_daily_private, "
                        "pg_catalog.has_function_privilege("
                        ":role, 'aap_audit_v1.refresh_audit_bi_mv()', 'EXECUTE') "
                        "AS audit_daily_refresh, pg_catalog.has_table_privilege("
                        ":role, 'aap_jobs_v1.notification_scope_facts', 'SELECT') "
                        "AS jobs_notification_scope, (pg_catalog.has_table_privilege("
                        ":role, 'aap_jobs_v1.notification_scope_facts', "
                        "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                        "pg_catalog.has_any_column_privilege("
                        ":role, 'aap_jobs_v1.notification_scope_facts', "
                        "'INSERT,UPDATE,REFERENCES')) AS jobs_notification_scope_write"
                    ),
                    {"role": runtime.username},
                )
                notification_grants = notification_result.mappings().one()
                if (
                    notification_grants["preferences"] is not True
                    or notification_grants["append"] is not True
                    or notification_grants["terminal_append"] is not True
                    or notification_grants["read_state"] is not True
                    or notification_grants["notification_delete"] is not True
                    or notification_grants["notification_private"] is not False
                    or notification_grants["notification_column_private"] is not False
                    or notification_grants["notification_facts"] is not True
                    or notification_grants["notification_facts_write"] is not False
                    or notification_grants["notification_facts_column_write"]
                    is not False
                    or notification_grants["audit_append"] is not True
                    or notification_grants["audit_query"] is not True
                    or notification_grants["audit_read"] is not True
                    or notification_grants["audit_read_write"] is not False
                    or notification_grants["audit_read_column_write"] is not False
                    or notification_grants["audit_daily_read"] is not True
                    or notification_grants["audit_daily_write"] is not False
                    or notification_grants["audit_daily_private"] is not False
                    or notification_grants["audit_daily_refresh"] is not False
                    or notification_grants["jobs_notification_scope"] is not True
                    or notification_grants["jobs_notification_scope_write"] is not False
                ):
                    raise WorktreeError(
                        "Notifications read/write boundary and Audit "
                        f"capabilities were not granted: {notification_grants}"
                    )
        finally:
            await engine.dispose()

    def prepare(self, *, skip_migrations: bool = False) -> None:
        revisions, heads = migration_graph(self.code_root)
        inventory = self.inspect()
        validate_inventory(self.resources, inventory)
        current = asyncio.run(self.revisions()) if inventory["database"] else []
        validate_revisions(revisions, heads, current, skip=skip_migrations)
        asyncio.run(self.owned_runtime_role("check_create"))
        self.upgrade_training_bucket()
        self.upgrade_inputs_bucket()
        inventory = self.inspect()
        validate_inventory(self.resources, inventory)
        atomic_json(
            state_path(self.root, self.resources["mode"], "resources.json"),
            self.manifest,
        )
        self.touched = True
        asyncio.run(self.owned_runtime_role("create"))
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
        asyncio.run(self.verify_runtime_login())
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
        asyncio.run(self.owned_runtime_role("check_drop"))
        # Validate every resource before the first destructive operation. Each backend
        # then rechecks ownership at the write boundary. Never use DROP ... FORCE.
        asyncio.run(self.database("drop"))
        asyncio.run(self.owned_runtime_role("drop"))
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
        state_path(
            self.root, self.resources["mode"], "runtime-credentials.json"
        ).unlink(missing_ok=True)
        state_path(self.root, self.resources["mode"], "resources.json").unlink(
            missing_ok=True
        )
        print(
            f"[dev:worktree] 已删除本环境资源 {self.resources['confirmation']}",
            file=sys.stderr,
        )
