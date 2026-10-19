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
    """v0.19.0 · 全局 ML backend 注册表(ADR-0044)。

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
    # 端点接入属性(上提到全局, 不做项目覆盖)
    auth_method: Mapped[str] = mapped_column(String(20), default="none")
    auth_token: Mapped[str | None] = mapped_column(String(500))
    # Database-managed revision fences health/control observations across rotation,
    # including rotations back to a previously used credential.
    auth_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    extra_params: Mapped[dict] = mapped_column(JSONB, default=dict)
    # ADR-0049 · 一个 registry backend 本期只能 claim 一个稳定物理 GPU/MIG
    # resource。CPU / 尚未完成 GPU 配置的存量行保持 null/null，不猜卡。
    gpu_resource_id: Mapped[str | None] = mapped_column(
        String(512), nullable=True, index=True
    )
    vram_budget_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eviction_priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 能力快照单份真值: check_health 对注册项探测、写回此行
    health_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # 'manual' = superadmin 在 ModelMarket 注册; 'env' = 启动钩子按 env URL 自动 upsert
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
    """v0.23.3 ADR-0050 · 项目 × 服务池关联(原 ADR-0044 ``ProjectMLBackend``)。

    项目层退化为「启用开关 + 项目级覆盖」: enabled 控制该项目能否选用此服务池;
    default_variants 是项目级变体覆盖(可空,空=用全局默认), 覆盖语义改为 pool 级。
    预标 / DAG 下游 / backends>=2 门控读 enabled=true 集合。

    v0.23.3 把 ``registry_id`` 改为 ``pool_id``: 项目不再绑定单个物理实例,
    而是绑定一个可包含多个等价副本的服务池 (ADR-0050)。原 ``registry_id``
    列经 alembic 0132 迁移为 ``pool_id`` (singleton backfill 保证每 registry
    恰有一个 singleton pool, off mode 行为不变)。
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
    # v0.23.3 · 原 registry_id 迁移为 pool_id (ADR-0050 singleton backfill)。
    # 项目启用和主池必须一致; 禁用主池时显式清空或先换主池, 不能留悬空引用。
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ml_backend_service_pools.id", ondelete="CASCADE"),
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 项目级业务覆盖(可空)。阈值(box/text)已退役: per-backend 调参由运行时按 /setup.params
    # 通用渲染, 项目级单值 project.box_threshold 兜底。变体覆盖 v0.23.3 改为 pool 级。
    default_variants: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
