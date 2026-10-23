from typing import Literal
from pydantic import BaseModel, Field, field_validator
from uuid import UUID
from datetime import datetime

from aap_identity.preference_models import (
    AIToolPreferences as AIToolPreferences,
    CameraPanelState as CameraPanelState,
    FloatingPanelState as FloatingPanelState,
    FloatingSelectionState as FloatingSelectionState,
    LabelContentByType as LabelContentByType,
    NamedPresetsRevision as NamedPresetsRevision,
    OnboardingPreferences as OnboardingPreferences,
    OnboardingProjectState as OnboardingProjectState,
    PointcloudCameraState as PointcloudCameraState,
    ShortcutBinding as ShortcutBinding,
    TriViewFloatState as TriViewFloatState,
    UserPreferences as UserPreferences,
    UserPreferencesRead as UserPreferencesRead,
    UIPreferences as UIPreferences,
    WorkbenchCommonPreferences as WorkbenchCommonPreferences,
    WorkbenchImagePreferences as WorkbenchImagePreferences,
    WorkbenchLayoutPreferences as WorkbenchLayoutPreferences,
    WorkbenchLayoutPreferencesRead as WorkbenchLayoutPreferencesRead,
    WorkbenchPointcloudPreferences as WorkbenchPointcloudPreferences,
    WorkbenchPreferences as WorkbenchPreferences,
    WorkbenchPreferencesRead as WorkbenchPreferencesRead,
    WorkbenchShortcutPreferences as WorkbenchShortcutPreferences,
    WorkbenchVideoPreferences as WorkbenchVideoPreferences,
    shortcut_command_domain as shortcut_command_domain,
)
from aap_identity.workbench_workspace import (
    WorkbenchWorkspacePreferences as WorkbenchWorkspacePreferences,
)


class UserCreate(BaseModel):
    email: str
    name: str
    password: str
    #: Account/platform role.  Administrator-created ordinary staff default to
    #: ``employee``; public registration is always a viewer and does not use
    #: this schema.  Legacy global annotator/reviewer inputs are rejected.
    role: str = "employee"

    @field_validator("role")
    @classmethod
    def _validate_platform_role(cls, value: str) -> str:
        from app.db.enums import LEGACY_PLATFORM_STAFF_ROLES, PLATFORM_ROLES

        if value in LEGACY_PLATFORM_STAFF_ROLES:
            raise ValueError("旧全局角色已废弃，请使用 employee")
        if value not in PLATFORM_ROLES:
            raise ValueError(f"非法角色: {value}")
        return value


class UserOut(BaseModel):
    id: UUID
    email: str
    name: str
    role: str
    group_name: str | None
    group_id: UUID | None = None
    status: str
    is_active: bool = True
    disabled_kind: str | None = None
    disabled_at: datetime | None = None
    disabled_by: UUID | None = None
    disabled_reason: str | None = None
    # v0.12.0 · 邮箱验证时间戳；None = 未验证。仅开放注册 + 验证开关打开时作登录 gate。
    email_verified_at: datetime | None = None
    last_login_at: datetime | None = None
    # v0.8.3 · 心跳机制：最近一次活跃时间（登录 / POST /me/heartbeat / 关键操作）
    last_seen_at: datetime | None = None
    # v0.8.1 · 非空 = 管理员刚重置密码，前端登录后跳「强制改密」页
    password_admin_reset_at: datetime | None = None
    # v0.8.1 · 自助注销冷静期信息（已申请时返回 scheduled_at）；未申请均为 None
    deactivation_requested_at: datetime | None = None
    deactivation_scheduled_at: datetime | None = None
    # v0.9.41 · 标注偏好（workbench 渲染配置等）。空对象 = 用客户端默认。
    preferences: dict = Field(default_factory=dict)
    # 头像引用（preset:<slug> / upload:<token>）；None = 首字母回退。
    avatar_ref: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class UserReceiverOption(BaseModel):
    """A role-specific, currently eligible offboarding receiver."""

    id: UUID
    name: str
    email: str
    role: str
    project_member_role: str | None = None


class OffboardingBlocker(BaseModel):
    code: str
    message: str


class OffboardingBatchRef(BaseModel):
    batch_id: UUID
    batch_name: str


class OffboardingRolePreview(BaseModel):
    present: bool = False
    batches: list[OffboardingBatchRef] = Field(default_factory=list)
    receiver_options: list[UserReceiverOption] = Field(default_factory=list)


class OffboardingProjectPreview(BaseModel):
    project_id: UUID
    project_name: str
    roles: dict[str, OffboardingRolePreview]
    tasks: dict[str, dict[str, int]]
    locked_task_count: int = 0
    blockers: list[OffboardingBlocker] = Field(default_factory=list)


class OffboardingApiKeyPreview(BaseModel):
    id: UUID
    name: str
    key_prefix: str
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class OffboardingPreview(BaseModel):
    user: UserOut
    preview_version: str
    generated_at: datetime
    projects: list[OffboardingProjectPreview] = Field(default_factory=list)
    api_keys: list[OffboardingApiKeyPreview] = Field(default_factory=list)
    blockers: list[OffboardingBlocker] = Field(default_factory=list)
    can_commit: bool = False


class OffboardingProjectRequest(BaseModel):
    project_id: UUID
    owner_receiver_id: UUID | None = None
    annotator_receiver_id: UUID | None = None
    reviewer_receiver_id: UUID | None = None


class OffboardingCommitRequest(BaseModel):
    preview_version: str
    reason: str = Field(default="", max_length=500)
    mode: Literal["handoff", "emergency_suspend"] = "handoff"
    projects: list[OffboardingProjectRequest] = Field(default_factory=list)


class OffboardingTransferResult(BaseModel):
    project_id: UUID
    role: str
    receiver_id: UUID | None = None
    batch_ids: list[UUID] = Field(default_factory=list)
    task_count: int = 0
    lock_count: int = 0


class OffboardingUnresolvedResult(BaseModel):
    project_id: UUID | None = None
    role: str | None = None
    reason: str
    batch_ids: list[UUID] = Field(default_factory=list)
    task_count: int = 0
    lock_count: int = 0


class OffboardingResult(BaseModel):
    user: UserOut
    status: str
    mode: Literal["handoff", "emergency_suspend"]
    transfers: list[OffboardingTransferResult] = Field(default_factory=list)
    unresolved: list[OffboardingUnresolvedResult] = Field(default_factory=list)
    revoked_api_key_ids: list[UUID] = Field(default_factory=list)
    audit_id: int | None = None


class ReactivateRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class UserBrief(BaseModel):
    """v0.7.2 · 责任人可视化用：列表 / 卡片侧 inline 渲染头像 + 名字 + 角色。"""

    id: UUID
    name: str
    email: str
    role: str | None = None
    avatar_initial: str
    # 头像引用（preset:<slug> / upload:<token>）；None = 前端回退 avatar_initial。
    avatar_ref: str | None = None

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: str
    password: str
    # v0.9.3 · progressive CAPTCHA：同 IP 失败 ≥ 阈值后必填；前端从 401 响应头
    # X-Login-Failed-Count 拿到当前 count，达阈值时渲染 <Captcha>。
    captcha_token: str | None = None
