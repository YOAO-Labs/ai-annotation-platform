import uuid
from datetime import datetime
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class MLBackendRegistry(Base):
    """全局 ML backend 注册表(ADR-0044)。

    backend 实例从「项目子资源」上提为「全局注册项」: 一个物理 backend(url) 注册一次,
    能力快照 health_meta.capabilities 单份真值,所有启用项目共享。

    auth_method / auth_token / extra_params 是「如何调用该 url」的端点固有属性,与项目无关,
    故随 url 进全局行(不做项目覆盖)。项目级覆盖只放真正业务相关的阈值/变体,见 ProjectMLBackendPool。
    """

    __tablename__ = "ml_backend_registry"
    __table_args__ = (
        CheckConstraint(
            "auth_revision >= 0", name="ck_ml_backend_auth_revision_nonnegative"
        ),
        CheckConstraint(
            "(gpu_resource_id IS NULL) = (vram_budget_mb IS NULL)",
            name="ck_ml_backend_registry_gpu_claim_pair",
        ),
        CheckConstraint(
            "vram_budget_mb IS NULL OR vram_budget_mb > 0",
            name="ck_ml_backend_registry_vram_budget_positive",
        ),
        CheckConstraint(
            "gpu_resource_id IS NULL OR ("
            "gpu_resource_id = btrim(gpu_resource_id) AND "
            "gpu_resource_id !~ '[[:space:],]' AND "
            "position('/' in gpu_resource_id) > 1 AND "
            "position('/' in gpu_resource_id) < char_length(gpu_resource_id))",
            name="ck_ml_backend_registry_gpu_resource_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(30), default="disconnected")
    is_interactive: Mapped[bool] = mapped_column(Boolean, default=False)
    auth_method: Mapped[str] = mapped_column(String(20), default="none")
    auth_token: Mapped[str | None] = mapped_column(String(500))
    auth_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    extra_params: Mapped[dict] = mapped_column(JSONB, default=dict)
    gpu_resource_id: Mapped[str | None] = mapped_column(
        String(512), nullable=True, index=True
    )
    vram_budget_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eviction_priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    health_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    error_message: Mapped[str | None] = mapped_column(String)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProjectMLBackendPool(Base):
    """项目与服务池的启用关联（ADR-0050）。

    enabled 决定该项目能否选用此服务池；default_variants 是项目级变体覆盖。
    覆盖按池划分；缺省沿用全局默认。
    预标 / DAG 下游 / backends>=2 门控读 enabled=true 集合。

    ``pool_id`` 指向服务池而非单个物理实例，池可包含多个等价副本。
    Alembic 0132 将旧 ``registry_id`` 关联迁移为 ``pool_id``：
    每个注册实例先分配 singleton pool，保留 off 模式派发行为。
    """

    __tablename__ = "project_ml_backend_pool"
    __table_args__ = (
        UniqueConstraint("project_id", "pool_id", name="uq_project_ml_backend_pool"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ml_backend_service_pools.id", ondelete="CASCADE"),
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_variants: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
