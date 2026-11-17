# 审计日志模型：audit_logs 表（第十七批「统计分析与运营后台」规格十七）
# 必须记录：登录登出 / 修改权限 / 删除内容 / 恢复备份 / 下架作品 / 处理举报 /
#           修改系统配置 / 查看受保护内容 / 报表导出。
# IP 仅保存脱敏哈希（ip_hash），user_agent 原样记录，metadata 为 JSON 字符串。
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    # ===== 第二十四批「审计日志」：组织范围 + 变更前后值 =====
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    actor_type: Mapped[str] = mapped_column(
        String(20),
        default="user",
        nullable=False
    )

    action: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        index=True
    )

    target_type: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        index=True
    )

    target_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True
    )

    # ===== 第二十四批「审计日志」：变更前后值 =====
    before_value: Mapped[str | None] = mapped_column(Text)
    after_value: Mapped[str | None] = mapped_column(Text)

    reason: Mapped[str | None] = mapped_column(Text)

    ip_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True
    )

    user_agent: Mapped[str | None] = mapped_column(String(255))

    metadata_json: Mapped[str | None] = mapped_column("metadata", Text)

    # ===== 阶段 J「审计补齐 + 备份调度」：内容级可追溯审计（补充 10 / R13） =====
    # action 扩展取值：content_save / content_restore / ai_apply / backfill，
    # 配合以下字段实现「谁在哪个版本改了什么字段」的正文级追溯。
    # 注意：正文全文不写入审计表（避免审计表体积膨胀），只保存哈希/长度/摘要 +
    #       字段级 diff（diff_json）。
    project_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )
    # 字段级 diff 明细（JSON：changed_fields / fields.content.before_hash 等）
    diff_json: Mapped[str | None] = mapped_column(Text)
    content_hash_before: Mapped[str | None] = mapped_column(String(64))
    content_hash_after: Mapped[str | None] = mapped_column(String(64))
    # 变更后的正文版本号（chapter_contents.version）
    content_version: Mapped[int | None] = mapped_column(Integer)
    # 变更来源：manual / ai / restore / backfill / system
    change_source: Mapped[str | None] = mapped_column(String(30))

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )
