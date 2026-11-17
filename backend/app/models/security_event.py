# 安全事件模型（第十八批「性能优化、可靠性与安全加固」规格十八/二十四）
# 登录失败 / 登录成功 / 账号锁定 / 新设备提醒 / 异地登录提醒 /
# 密码修改 / 权限变更 / 数据导出 / 备份恢复 / 查看私人内容等安全事件记录。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True
    )

    ip_hash: Mapped[str | None] = mapped_column(
        String(64)
    )

    device_info: Mapped[str | None] = mapped_column(
        String(255)
    )

    details: Mapped[str | None] = mapped_column(
        Text
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )
