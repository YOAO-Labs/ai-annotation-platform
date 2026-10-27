"""v0.11.16 · 存储连接器 service：CRUD + 连通性测试。

密钥加解密只在本层发生，绝不出层；所有出网操作（test）前先过 connector_guard
（白名单 + SSRF）。实际数据拉取的 SourceAdapter 在 v0.11.15。
"""

from __future__ import annotations

import uuid
import errno
import socket
from contextlib import contextmanager
from threading import Timer
from functools import partial

from fastapi import HTTPException
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
    EndpointConnectionError,
)

import anyio
import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret, encrypt_secret
from app.db.enums import UserRole
from app.db.models.storage_connection import StorageConnection
from app.db.models.user import User
from app.services import connector_guard
from app.services.storage_access import close_registered, mark_unsettled

# 各 kind 的必填字段
_REQUIRED_CONFIG = {
    "s3": {"endpoint", "bucket"},
    "sftp": {"host", "username"},
}
# config 中允许保留的非密钥键（白名单式裁剪，杜绝密钥混入 config）
_ALLOWED_CONFIG = {
    "s3": {"endpoint", "bucket", "region", "base_prefix", "use_ssl"},
    "sftp": {"host", "port", "username", "base_path", "auth_type"},
}

_TEST_SAMPLE_LIMIT = 20


class ConnectorValidationError(Exception):
    """config/secret 校验失败。"""


class ConnectorAccessDenied(Exception):
    """当前用户不可使用或管理该连接器。"""


def _role_value(role) -> str:
    return getattr(role, "value", role)


def _is_super_admin(user: User) -> bool:
    return _role_value(user.role) == UserRole.SUPER_ADMIN.value


def can_use_connection(user: User, conn: StorageConnection) -> bool:
    if _is_super_admin(user) or conn.scope == "global":
        return True
    return conn.created_by is not None and conn.created_by == user.id


def assert_connection_usable(user: User, conn: StorageConnection) -> None:
    if not can_use_connection(user, conn):
        raise ConnectorAccessDenied("连接器不存在")


def assert_connection_admin(user: User, conn: StorageConnection) -> None:
    if conn.scope == "global":
        if _is_super_admin(user):
            return
        raise ConnectorAccessDenied("仅超级管理员可管理全局连接器")
    if _is_super_admin(user) or (
        conn.created_by is not None and conn.created_by == user.id
    ):
        return
    raise ConnectorAccessDenied("仅创建者或超级管理员可管理连接器")


def _validate_and_sanitize_config(kind: str, config: dict) -> dict:
    missing = _REQUIRED_CONFIG[kind] - set(config or {})
    if missing:
        raise ConnectorValidationError(
            f"{kind} 缺少必填配置: {', '.join(sorted(missing))}"
        )
    return {k: config[k] for k in _ALLOWED_CONFIG[kind] if k in config}


def _validate_secret(kind: str, secret: dict) -> None:
    secret = secret or {}
    if kind == "s3":
        if not secret.get("access_key") or not secret.get("secret_key"):
            raise ConnectorValidationError("s3 需提供 access_key 与 secret_key")
    elif kind == "sftp":
        auth = (secret.get("private_key") and "key") or (
            secret.get("password") and "password"
        )
        if not auth:
            raise ConnectorValidationError("sftp 需提供 password 或 private_key")


def target_host(conn: StorageConnection) -> str:
    """connector_guard 校验用的目标地址（endpoint / host）。"""
    if conn.kind == "s3":
        return str(conn.config.get("endpoint", ""))
    return str(conn.config.get("host", ""))


def to_out_dict(conn: StorageConnection) -> dict:
    """转 Out（脱敏：不含 secret，仅 secret_set）。"""
    return {
        "id": conn.id,
        "revision": conn.revision,
        "name": conn.name,
        "kind": conn.kind,
        "config": conn.config or {},
        "scope": "global" if conn.scope == "global" else "owner",
        "project_id": conn.project_id,
        "secret_set": conn.secret_enc is not None,
        "created_by": conn.created_by,
        "created_at": conn.created_at,
        "updated_at": conn.updated_at,
    }


class StorageConnectionService:
    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        name: str,
        kind: str,
        config: dict,
        secret: dict,
        scope: str,
        created_by: uuid.UUID,
        project_id: uuid.UUID | None = None,
    ) -> StorageConnection:
        clean_config = _validate_and_sanitize_config(kind, config)
        _validate_secret(kind, secret)
        conn = StorageConnection(
            name=name,
            kind=kind,
            config=clean_config,
            secret_enc=encrypt_secret(secret),
            scope=scope,
            project_id=None,
            created_by=created_by,
        )
        # 落库前先过白名单/SSRF：拒绝建一个根本连不出去的连接器。
        await connector_guard.assert_connection_target_allowed(db, target_host(conn))
        db.add(conn)
        await db.flush()
        from app.services.storage_location import register_connection

        await register_connection(db, conn)
        return conn

    @staticmethod
    async def get(db: AsyncSession, conn_id: uuid.UUID) -> StorageConnection | None:
        return await db.get(StorageConnection, conn_id)

    @staticmethod
    async def list_visible(
        db: AsyncSession,
        *,
        all_scopes: bool,
        user_id: uuid.UUID,
    ) -> list[StorageConnection]:
        """all_scopes=True（超管）→ 全部；否则 global + 当前用户创建的。"""
        stmt = select(StorageConnection).order_by(StorageConnection.created_at.desc())
        if not all_scopes:
            stmt = stmt.where(
                (StorageConnection.scope == "global")
                | (StorageConnection.created_by == user_id)
            )
        rows = await db.execute(stmt)
        return list(rows.scalars().all())

    @staticmethod
    async def update(
        db: AsyncSession,
        conn: StorageConnection,
        *,
        expected_revision: int,
        actor_id: uuid.UUID,
        api_key_id: uuid.UUID | None = None,
        name: str | None,
        config: dict | None,
        secret: dict | None,
    ) -> StorageConnection:
        from app.db.models.dataset_asset import StorageLocation
        from app.services.storage_location import location_is_referenced
        from app.services.dataset_content import key_authority

        actor = await _active_actor(db, actor_id, manage=True)
        assert_connection_admin(actor, conn)
        proof = await key_authority(db, actor, api_key_id, "datasets:write")
        _assert_revision(conn, expected_revision)
        candidate_config = (
            _validate_and_sanitize_config(conn.kind, config)
            if config is not None
            else dict(conn.config or {})
        )
        config_changed = candidate_config != (conn.config or {})
        needs_validation = config_changed or secret is not None
        candidate_secret = None
        if secret is not None:
            _validate_secret(conn.kind, secret)
            candidate_secret = encrypt_secret(secret)
        allowlist = None
        if config_changed:
            location = await db.scalar(
                select(StorageLocation).where(StorageLocation.connection_id == conn.id)
            )
            if location and await location_is_referenced(db, location.id):
                raise HTTPException(
                    409, "Referenced storage configuration cannot be redirected"
                )
        if needs_validation:
            target = _target_host(conn.kind, candidate_config)
            allowlist = await _allowed_target(db, target)
            plain = (
                secret
                if secret is not None
                else (decrypt_secret(conn.secret_enc) if conn.secret_enc else {})
            )
            ok, message, _ = await _probe(
                conn.kind, candidate_config, plain, allowlist=allowlist
            )
            if not ok:
                raise ConnectorValidationError(f"{message}；原配置未修改")
            # DNS/remote I/O must finish before taking either SQL row lock.
            allowlist = await _allowed_target(db, target)

        conn = await db.scalar(
            select(StorageConnection)
            .where(StorageConnection.id == conn.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if conn is None:
            raise HTTPException(404, "连接器不存在")
        _assert_revision(conn, expected_revision)
        actor = await _active_actor(db, actor_id, manage=True)
        assert_connection_admin(actor, conn)
        if await key_authority(db, actor, api_key_id, "datasets:write") != proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        if needs_validation and await _fresh_allowlist(db) != allowlist:
            raise HTTPException(409, "主机白名单已变更，请重新验证后保存")
        location = await db.scalar(
            select(StorageLocation)
            .where(StorageLocation.connection_id == conn.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            config_changed
            and location
            and await location_is_referenced(db, location.id)
        ):
            raise HTTPException(
                409, "Referenced storage configuration cannot be redirected"
            )
        if name is not None:
            conn.name = name
        if config_changed:
            conn.config = candidate_config
        if secret is not None:
            conn.secret_enc = candidate_secret
        conn.revision += 1
        if location and needs_validation:
            location.config_revision += 1
        await db.flush()
        return conn

    @staticmethod
    async def delete(db: AsyncSession, conn: StorageConnection) -> None:
        from app.db.models.dataset_asset import StorageLocation
        from app.services.storage_location import location_is_referenced
        from fastapi import HTTPException

        conn = await db.scalar(
            select(StorageConnection)
            .where(StorageConnection.id == conn.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if conn is None:
            raise HTTPException(404, "Storage connection not found")
        location = await db.scalar(
            select(StorageLocation)
            .where(StorageLocation.connection_id == conn.id)
            .with_for_update()
        )
        if location:
            if await location_is_referenced(db, location.id):
                raise HTTPException(409, "Storage location is still referenced")
            await db.delete(location)
            await db.flush()
        await db.delete(conn)

    @staticmethod
    async def test_connection(
        db: AsyncSession,
        conn: StorageConnection,
        *,
        actor_id: uuid.UUID,
        api_key_id: uuid.UUID | None = None,
    ) -> tuple[bool, str, int | None]:
        from app.services.dataset_content import key_authority
        from app.services import storage_access
        from app.services.storage_location import connector_location_id

        actor = await _active_actor(db, actor_id)
        assert_connection_usable(actor, conn)
        proof = await key_authority(db, actor, api_key_id, "datasets:read")
        conn_id, revision = conn.id, conn.revision
        target = target_host(conn)
        allowlist = await _allowed_target(db, target)
        secret = decrypt_secret(conn.secret_enc) if conn.secret_enc else {}
        location_id = connector_location_id(conn_id)
        maintenance_revision = await storage_access.check(db, location_id)
        result = await _probe(
            conn.kind,
            dict(conn.config or {}),
            secret,
            allowlist=allowlist,
            io=partial(
                storage_access.run,
                db,
                location_id,
                "connection_test",
                expected_revision=maintenance_revision,
            ),
        )
        await storage_access.check(
            db, location_id, expected_revision=maintenance_revision, lock=True
        )
        await _allowed_target(db, target)
        conn = await db.get(StorageConnection, conn_id, populate_existing=True)
        if conn is None:
            raise HTTPException(404, "连接器不存在")
        _assert_revision(conn, revision)
        actor = await _active_actor(db, actor_id)
        assert_connection_usable(actor, conn)
        if await key_authority(db, actor, api_key_id, "datasets:read") != proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})
        return result


def _assert_revision(conn, expected_revision):
    if conn.revision != expected_revision:
        raise HTTPException(
            409,
            detail={
                "code": "connection_revision_conflict",
                "message": "连接配置已被其他操作更新，请取消编辑并重新打开后再保存",
            },
        )


async def _active_actor(db, actor_id, *, manage=False):
    actor = await db.get(User, actor_id, populate_existing=True)
    if (
        actor is None
        or not actor.is_active
        or (
            manage
            and _role_value(actor.role)
            not in {
                UserRole.SUPER_ADMIN.value,
                UserRole.PROJECT_ADMIN.value,
            }
        )
    ):
        raise HTTPException(403, detail={"code": "access_revoked"})
    return actor


async def _fresh_allowlist(db):
    from app.services.system_settings_service import SystemSettingsService

    values = await SystemSettingsService.get_many(
        db, ["connector_host_allowlist"], bypass_cache=True
    )
    return values["connector_host_allowlist"] or []


def _target_host(kind, config):
    return str(config.get("endpoint" if kind == "s3" else "host", ""))


async def _allowed_target(db, target):
    allowlist = await _fresh_allowlist(db)
    try:
        with anyio.fail_after(5):
            await anyio.to_thread.run_sync(
                connector_guard.assert_host_allowed,
                target,
                allowlist,
                abandon_on_cancel=True,
            )
    except TimeoutError:
        raise ConnectorValidationError("连接验证失败：地址解析超时") from None
    return allowlist


_PROBE_TIMEOUT_SECONDS = 35


async def _probe(kind, config, secret, *, allowlist=None, io=None):
    from app.services.content_snapshot import ContentError

    try:
        if io is not None:
            count = await io(
                _test_blocking,
                kind,
                config,
                secret,
                allowlist,
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        else:
            # Inline candidates do not establish a registered-location quiet proof.
            with anyio.fail_after(_PROBE_TIMEOUT_SECONDS):
                count = await anyio.to_thread.run_sync(
                    _test_blocking,
                    kind,
                    config,
                    secret,
                    allowlist,
                    abandon_on_cancel=True,
                )
        return True, "认证与目录列举成功", count
    except ContentError:
        raise
    except Exception as exc:  # Never expose SDK errors: they may contain secrets/URLs.
        if io is not None and getattr(exc, "storage_operation_unsettled", False):
            raise mark_unsettled(
                ContentError("storage_operation_unsettled", 503)
            ) from None
        return False, _probe_failure_message(exc), None


def _probe_failure_message(exc):
    import paramiko
    from app.services.filesystem_storage import FilesystemStorageError

    if isinstance(
        exc, (TimeoutError, socket.timeout, ConnectTimeoutError, ReadTimeoutError)
    ):
        reason = "连接超时"
    elif isinstance(exc, paramiko.AuthenticationException):
        reason = "认证失败"
    elif isinstance(exc, (paramiko.BadHostKeyException, paramiko.SSHException)):
        reason = "SSH 连接或主机密钥验证失败"
    elif isinstance(exc, FilesystemStorageError):
        reason = {
            "sftp_host_key_mismatch": "SSH 主机密钥验证失败",
            "permission_denied": "无目录列举权限",
            "path_unavailable": "目标目录不存在",
            "connection_unavailable": "认证或连接凭据不可用",
        }.get(exc.code, "目标不可用")
    elif isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        reason = {
            "InvalidAccessKeyId": "认证失败",
            "SignatureDoesNotMatch": "认证失败",
            "ExpiredToken": "认证失败",
            "InvalidToken": "认证失败",
            "AccessDenied": "无目录列举权限",
            "NoSuchBucket": "目标桶不存在",
        }.get(code, "对象存储拒绝请求")
    elif isinstance(exc, FileNotFoundError):
        reason = "目标目录不存在"
    elif isinstance(exc, PermissionError):
        reason = "无目录列举权限"
    elif isinstance(
        exc, (ConnectionError, EndpointConnectionError, socket.gaierror)
    ) or (
        isinstance(exc, OSError)
        and exc.errno in {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}
    ):
        reason = "网络不可达"
    else:
        reason = "目标不可用"
    return f"连接验证失败：{reason}"


def _test_blocking(kind: str, config: dict, secret: dict, allowlist=None) -> int:
    """同步连通性探测（在线程池执行）。返回采样到的条目数。"""
    if kind == "s3":
        return _test_s3(config, secret)
    return _test_sftp(config, secret, allowlist)


def _test_s3(config: dict, secret: dict) -> int:
    scheme = "https" if config.get("use_ssl") else "http"
    endpoint = config["endpoint"]
    if "://" not in endpoint:
        endpoint = f"{scheme}://{endpoint}"
    try:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=secret.get("access_key"),
            aws_secret_access_key=secret.get("secret_key"),
            region_name=config.get("region"),
            config=BotoConfig(
                connect_timeout=10, read_timeout=15, retries={"total_max_attempts": 1}
            ),
        )
    except BaseException as exc:
        raise mark_unsettled(exc)
    try:
        resp = client.list_objects_v2(
            Bucket=config["bucket"],
            Prefix=config.get("base_prefix", "") or "",
            MaxKeys=_TEST_SAMPLE_LIMIT,
        )
        return min(int(resp.get("KeyCount", 0)), _TEST_SAMPLE_LIMIT)
    finally:
        close_registered(client)


@contextmanager
def _close_on_deadline(close):
    # Deadline close is physical I/O too: cancel() cannot join a running callback.
    failures = []

    def finish_on_deadline():
        try:
            close()
        except BaseException as exc:
            failures.append(mark_unsettled(exc))

    timer = Timer(_PROBE_TIMEOUT_SECONDS, finish_on_deadline)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        timer.join()
        if failures:
            raise failures[0]


def _sample_sftp(sftp, path):
    from itertools import islice

    entries = sftp.listdir_iter(path, read_aheads=1)
    try:
        return sum(1 for _ in islice(entries, _TEST_SAMPLE_LIMIT))
    finally:
        close_registered(entries)


def _test_sftp(config: dict, secret: dict, allowlist=None) -> int:
    import paramiko

    from app.services.sources.sftp import load_sftp_private_key
    from app.services.sources.sftp_reference import SftpAccess, qualified_root
    from app.services.filesystem_storage import FilesystemStorageError

    try:
        qualification = qualified_root(config)
    except FilesystemStorageError:
        qualification = None
    if qualification is not None:
        access = SftpAccess(
            config, encrypt_secret(secret), tuple(allowlist or []), *qualification
        )
        with access.client() as sftp:
            with _close_on_deadline(sftp.get_channel().get_transport().close):
                return _sample_sftp(sftp, "/")

    client = paramiko.SSHClient()
    # 不用 AutoAddPolicy（静默接受 = MITM 风险）；首版用 RejectPolicy + 系统 known_hosts。
    # TODO(v0.11.15): 支持超管预置 / TOFU 记录指纹到 system_settings。
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    with _close_on_deadline(client.close):
        return _test_legacy_sftp(client, config, secret, load_sftp_private_key)


def _test_legacy_sftp(client, config, secret, load_sftp_private_key):
    try:
        pkey = load_sftp_private_key(secret)
        client.connect(
            hostname=config["host"],
            port=int(config.get("port", 22)),
            username=config["username"],
            password=secret.get("password") if not pkey else None,
            pkey=pkey,
            timeout=10,
            auth_timeout=10,
            banner_timeout=10,
            channel_timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )
        sftp = client.open_sftp()
        try:
            sftp.get_channel().settimeout(10)
            return _sample_sftp(sftp, config.get("base_path", ".") or ".")
        finally:
            close_registered(sftp)
    finally:
        close_registered(client)
