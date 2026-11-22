"""ADR-0050 · ML Backend 服务池数据模型。

在 ``ml_backend_registry`` (物理实例, ADR-0044) 之上叠加一层逻辑服务池:
一个 pool 表示一组可互换的等价实例。项目绑定与路由 lineage 使用 pool id；
既有 pipeline / 用户偏好等公开配置仍使用 registry id，router 在边界解析到 pool。

本模块只定义数据模型; 路由逻辑在 ``app.services.ml_routing`` (P2/P3)。
registry 行语义不变 — URL / auth / GPU claim / health 仍在 registry 行上, 不复制到 pool 表。
"""

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


class MLBackendServicePool(Base):
    """ADR-0050 · 逻辑服务池。

    一行 = 一组可互换的等价 registry 实例。pool id 是**逻辑请求身份**
    (项目关系绑定与路由 lineage 引用它), registry id 是**物理执行身份**
    (GPU dispatch / transport 用它), 二者永不互换。

    非空且 enabled 的 pool 必须有 ``legacy_instance_id``, 且它必须是本池
    active 成员; 它只服务 ``off`` / ``observe`` 兼容 dispatch, 不参与
    ``enforce`` 优先级 (ADR-0050 D15)。

    本表**不**保存 URL / auth / GPU claim / model residency / 实例 health —
    这些仍是 registry 行的端点固有属性 (ADR-0044)。
    """

    __tablename__ = "ml_backend_service_pools"
    __table_args__ = (
        CheckConstraint(
            "(enabled = false) OR (legacy_instance_id IS NOT NULL)",
            name="ck_ml_backend_service_pools_enabled_has_legacy",
        ),
        CheckConstraint(
            "routing_policy = 'smooth_weighted_round_robin'",
            name="ck_ml_backend_service_pools_routing_policy",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    routing_policy: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="smooth_weighted_round_robin",
        server_default="smooth_weighted_round_robin",
    )
    legacy_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ml_backend_registry.id", ondelete="RESTRICT"),
        nullable=True,
    )
    routing_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    capability_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    capability_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MLBackendPoolMember(Base):
    """ADR-0050 · 服务池成员关系。

    一个 registry instance 同时最多属于一个 service pool (``registry_id``
    单列 unique, D2)。``traffic_state`` 控制 router 是否可选该成员:
    ``active`` 可接收新 lease; ``draining`` 保留既有 lease 不接收新 lease;
    ``disabled`` 不接收新 lease 且 resume 需重新验证 (D5)。

    ``weight`` 仅影响 SWRR 分配比例, 不突破实例并发上限 (D10 / §10.1)。
    """

    __tablename__ = "ml_backend_pool_members"
    __table_args__ = (
        UniqueConstraint("pool_id", "registry_id", name="uq_ml_backend_pool_members"),
        UniqueConstraint("registry_id", name="uq_ml_backend_pool_members_registry"),
        CheckConstraint(
            "traffic_state IN ('active', 'draining', 'disabled')",
            name="ck_ml_backend_pool_members_traffic_state",
        ),
        CheckConstraint(
            "weight >= 1 AND weight <= 100",
            name="ck_ml_backend_pool_members_weight",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ml_backend_service_pools.id", ondelete="CASCADE"),
        index=True,
    )
    registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ml_backend_registry.id", ondelete="RESTRICT"),
        unique=True,
    )
    traffic_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    weight: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
