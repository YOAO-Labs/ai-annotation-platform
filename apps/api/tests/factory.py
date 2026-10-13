"""测试与 E2E 共用的对象工厂。

提取用户 / 项目 / 任务 / 批次的最小构造逻辑，供 pytest 与
e2e/fixtures/seed.ts 通过 _test_seed router 调用。

约束：
  - 只在显式开启 E2E Seed 且数据库名以 _e2e / _test 结尾时使用
    （_test_seed router 自身有环境与数据库纵深守卫）
  - 数据可重入（display_id 加随机后缀，避免重复 truncate 造数）
  - 不写 audit_log（避免污染 audit 测试）
  - 用户工厂只创建平台身份；项目成员与职责由测试/调用方显式创建，
    工厂不从平台角色推断 membership
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from functools import cache
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


DEFAULT_PASSWORD = "Test1234"


@cache
def _default_password_hash() -> str:
    # Fixture users share a known password; retain the real bcrypt cost and verifier.
    from app.core.security import hash_password

    return hash_password(DEFAULT_PASSWORD)


def build_tool_bindings(
    classes: list[str | dict[str, Any]],
    *,
    unit: str = "bbox",
    attribute_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按现行数据模型直接构造 tool_bindings（唯一存储真值）。

    产出一个启用的工具单位：类别条目直接携带 name（必填）与 color / alias /
    order（可选，缺省按入参顺序编号），attribute_schema 缺省为空字段集。
    结构与 API 写入路径产出的绑定同形，不经过任何旧扁平字段兼容翻译。
    unit 缺省 bbox（image-det / video-track 常规标注）；分割/掩码语义显式传
    unit="region"。
    """
    entries: list[dict[str, Any]] = []
    for i, c in enumerate(classes):
        entry: dict[str, Any] = dict(c) if isinstance(c, dict) else {"name": c}
        entry.setdefault("order", i)
        entries.append(entry)
    return {
        unit: {
            "enabled": True,
            "classes": entries,
            "attribute_schema": attribute_schema or {"fields": []},
        }
    }


def make_user_dict(
    role: str, email: str, name: str, password: str = DEFAULT_PASSWORD
) -> dict:
    from app.core.security import hash_password

    return {
        "id": uuid.uuid4(),
        "email": email,
        "name": name,
        "password_hash": (
            _default_password_hash()
            if password == DEFAULT_PASSWORD
            else hash_password(password)
        ),
        "role": role,
        "is_active": True,
    }


async def create_user(
    db: AsyncSession,
    role: str,
    email: str,
    name: str,
    password: str = DEFAULT_PASSWORD,
):
    from app.db.models.user import User

    data = make_user_dict(role, email, name, password)
    user = User(**data)
    db.add(user)
    await db.flush()
    return user


async def create_project(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    name: str = "E2E Project",
    type_key: str = "image-det",
    type_label: str = "图像目标检测",
    classes: list[str] | None = None,
):
    from app.db.models.project import Project

    suffix = secrets.token_hex(3)
    # tool_bindings 是唯一存储真值；classes 经工厂直接构造成现行绑定结构。
    project = Project(
        display_id=f"P-E2E-{suffix}",
        name=name,
        type_label=type_label,
        type_key=type_key,
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(list(classes or ["car", "person"])),
        ai_enabled=False,
    )
    db.add(project)
    await db.flush()
    return project


async def create_task(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    display_id: str | None = None,
    status: str = "pending",
):
    from app.db.models.task import Task

    suffix = secrets.token_hex(3)
    task = Task(
        display_id=display_id or f"T-E2E-{suffix}",
        project_id=project_id,
        status=status,
        file_name=f"e2e-{suffix}.jpg",
        file_path=f"e2e/{suffix}.jpg",
        file_type="image",
    )
    db.add(task)
    await db.flush()
    return task


async def create_batch(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    name: str | None = None,
    status: str = "draft",
):
    from app.db.models.task_batch import TaskBatch

    suffix = secrets.token_hex(3)
    batch = TaskBatch(
        project_id=project_id,
        display_id=f"B-E2E-{suffix}",
        name=name or f"E2E Batch {suffix}",
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    db.add(batch)
    await db.flush()
    return batch


async def create_membership(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    assigned_by: uuid.UUID | None = None,
    version: int | None = None,
    weekly_target: int | None = None,
):
    """Create one explicit project membership with a project role.

    Platform identity stays on the user; a membership is never inferred from
    the account role.  Callers that need a specific ``version`` (role-change
    CAS tests), ``weekly_target`` or an explicit ``assigned_by`` auditor pass
    them explicitly; the common case only names project, user and role.
    """

    from app.db.models.project_member import ProjectMember

    kwargs: dict = {"project_id": project_id, "user_id": user_id, "role": role}
    if assigned_by is not None:
        kwargs["assigned_by"] = assigned_by
    if version is not None:
        kwargs["version"] = version
    if weekly_target is not None:
        kwargs["weekly_target"] = weekly_target
    member = ProjectMember(**kwargs)
    db.add(member)
    await db.flush()
    return member
