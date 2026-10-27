"""Connection-row authorization helpers shared by storage-location flows."""

from __future__ import annotations

from app.db.enums import UserRole
from app.db.models.storage_connection import StorageConnection
from app.db.models.user import User


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


def target_host(conn: StorageConnection) -> str:
    """connector_guard 校验用的目标地址（endpoint / host）。"""
    if conn.kind == "s3":
        return str(conn.config.get("endpoint", ""))
    return str(conn.config.get("host", ""))
