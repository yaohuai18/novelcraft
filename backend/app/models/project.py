import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )

    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False
    )

    # 第十三批「发布与阅读模式」：公开信息字段
    subtitle: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)  # 作品简介
    cover_text: Mapped[str | None] = mapped_column(String(255))
    project_slug: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        index=True
    )
    # 发布状态：draft / review / published / unlisted / archived（与内部 status 分离）
    publication_status: Mapped[str] = mapped_column(
        String(30),
        default="draft",
        nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime)

    # 第二十一批「国际化、多语言和多区域部署」：多语言作品支持
    default_language: Mapped[str] = mapped_column(
        String(16),
        default="zh-CN",
        nullable=False
    )
    # 支持的语言列表（JSON 数组字符串，如 ["zh-CN","en-US"]）
    supported_languages: Mapped[str] = mapped_column(
        String(255),
        default='["zh-CN"]',
        nullable=False
    )
    # 第二十一批「内容和年龄限制」：服务端执行地区与年龄权限判断
    # 年龄分级：G / PG / PG-13 / R / X
    content_rating: Mapped[str | None] = mapped_column(String(16))
    # 地区可见性（JSON 数组，如 ["CN","US"]；为空表示所有地区可见）
    region_visibility: Mapped[str | None] = mapped_column(String(255))
    # 敏感内容标签（JSON 数组）
    sensitive_tags: Mapped[str | None] = mapped_column(String(255))
    # 人工审核状态：pending / approved / rejected / auto
    review_status: Mapped[str] = mapped_column(
        String(30),
        default="pending",
        nullable=False
    )

    genre: Mapped[str | None] = mapped_column(String(100))
    background: Mapped[str | None] = mapped_column(Text)
    world_setting: Mapped[str | None] = mapped_column(Text)
    main_conflict: Mapped[str | None] = mapped_column(Text)
    ending_direction: Mapped[str | None] = mapped_column(Text)
    narrative_perspective: Mapped[str | None] = mapped_column(String(100))
    writing_style: Mapped[str | None] = mapped_column(Text)
    forbidden_settings: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        nullable=False
    )

    # ===== 第二十四批「企业级协作」：组织归属 + 项目协作状态流转 =====
    # 协作状态：draft / collaborating / pending_review / revising / approved / published / archived
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )
    collab_status: Mapped[str] = mapped_column(
        String(30),
        default="draft",
        nullable=False,
        index=True
    )
    default_edit_permission: Mapped[str] = mapped_column(
        String(30),
        default="author_own",
        nullable=False
    )
    project_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True,
        index=True
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime)

    # 第十七批「内容运营」：官方标签 / 隐藏标记（status=hidden 表示被运营隐藏）
    official_tag: Mapped[str | None] = mapped_column(String(60))

    # 第十四批「公开作品索引」统计字段：全部由服务端更新，不信任客户端提交
    view_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    unique_reader_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    favorite_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    chapter_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    word_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    completion_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False
    )
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    # 软删除（2026-11-15 第一档升级）：删除作品改为写回收站，30 天后才真删
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
