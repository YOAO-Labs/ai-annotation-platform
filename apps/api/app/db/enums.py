from enum import Enum


class UserRole(str, Enum):
    """Platform account role.

    ``employee`` is the platform identity for staff who annotate and/or review.
    ``annotator`` / ``reviewer`` are retained as *historical* values: migration
    converts them to ``employee`` and the new authorization model never grants
    authority from them.  Keep the enum entries so migration adapters, audit
    rows and preserved historical data remain readable.
    """

    SUPER_ADMIN = "super_admin"
    PROJECT_ADMIN = "project_admin"
    EMPLOYEE = "employee"
    REVIEWER = "reviewer"
    ANNOTATOR = "annotator"
    VIEWER = "viewer"


class PlatformRole(str, Enum):
    """Account-level role.  The database/account field stays named ``role``."""

    SUPER_ADMIN = "super_admin"
    PROJECT_ADMIN = "project_admin"
    EMPLOYEE = "employee"
    VIEWER = "viewer"


class ProjectRole(str, Enum):
    """Per-project responsibility stored on ``project_members.role``."""

    ANNOTATOR = "annotator"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


PLATFORM_ROLES = frozenset(r.value for r in PlatformRole)
PROJECT_ROLES = frozenset(r.value for r in ProjectRole)
MANAGER_PLATFORM_ROLES = frozenset(
    {PlatformRole.SUPER_ADMIN.value, PlatformRole.PROJECT_ADMIN.value}
)
LEGACY_PLATFORM_STAFF_ROLES = frozenset({"annotator", "reviewer"})
WORK_PROJECT_ROLES = frozenset(
    {ProjectRole.ANNOTATOR.value, ProjectRole.REVIEWER.value}
)


class ProjectStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PENDING_REVIEW = "pending_review"
    ARCHIVED = "archived"


class TaskStatus(str, Enum):
    UPLOADING = "uploading"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REVIEW = "review"
    REJECTED = "rejected"


WORKBENCH_AI_EDITABLE_TASK_STATUSES = frozenset(
    {
        TaskStatus.PENDING.value,
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.REJECTED.value,
    }
)


class BatchStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PRE_ANNOTATED = "pre_annotated"
    ANNOTATING = "annotating"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class AnnotationSource(str, Enum):
    MANUAL = "manual"
    PREDICTION_BASED = "prediction_based"


class MLBackendState(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    PREDICTING = "predicting"


class MLBackendAuthMethod(str, Enum):
    NONE = "none"
    BASIC = "basic"
    TOKEN = "token"


class OrgMemberRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class DatasetDataType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    POINT_CLOUD = "point_cloud"
    MULTIMODAL = "multimodal"
    OTHER = "other"
