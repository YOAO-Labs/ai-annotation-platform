"""SceneFramePose ORM

scene 的时序骨架:逐帧 ego pose(ego→global,世界系)+ 时间戳。
grain = (scene_id, frame_index),一帧一行;历史 scene 无位姿 → 无行,
消费方(trajectory API / manifest / 跨帧自动化)按"无轨迹"降级。

坐标系约定:ego_translation [x,y,z] / ego_rotation [w,x,y,z] 按 nuScenes
ego_pose 原样存 ego→global;世界系本身是 ISO 8855。跨帧相对位移
= inv(pose_i) @ pose_j,由消费方算,不预存。
"""

from datetime import datetime
import uuid

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SceneFramePose(Base):
    __tablename__ = "scene_frame_poses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scene_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scenes.id", ondelete="CASCADE"),
        nullable=False,
    )
    frame_index: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp_us: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ego_translation: Mapped[list] = mapped_column(JSONB, nullable=False)
    ego_rotation: Mapped[list] = mapped_column(JSONB, nullable=False)
    source_metadata: Mapped[dict] = mapped_column(
        JSONB, server_default="{}", nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("scene_id", "frame_index", name="uq_scene_frame_pose"),
    )
