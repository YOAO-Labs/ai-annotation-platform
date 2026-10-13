import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, func, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class NotificationPreference(Base):
    """v0.7.0 · 用户级通知偏好（按 type 配置接收与弹出）。

    PK = (user_id, type)。channels JSONB 形如
    `{"in_app": true, "toast": false, "email": false}`：

    - ``in_app``：接收开关；False 时未来事件不落库也不推送。无记录默认 true。
    - ``toast``：弹出提示（瞬时提醒）选择；键缺省表示未选择，GET 按类型的
      默认重要程度（services/notification_preferences.py）计算有效值。
      关闭接收不重置已存的弹出选择，重新开启接收时恢复。
    - ``email``：保留待邮件渠道落地。

    PUT 只合并请求中出现的键（JSONB ``channels || patch``），未提供的键与
    并发标签页写入的其它键互不覆盖。
    """

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    type: Mapped[str] = mapped_column(String(60), primary_key=True)
    channels: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text('\'{"in_app": true, "email": false}\'::jsonb'),
        default=lambda: {"in_app": True, "email": False},
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
