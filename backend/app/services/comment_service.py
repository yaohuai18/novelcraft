# 评论服务（第十五批「通知、评论与协作反馈」规格实现）
# 评论创建/回复/编辑/软删/隐藏/解决/重开；提及解析（@用户名，去重/不存在/无权访问处理）；
# 内容审核（敏感词过滤）+ 评论防滥用（频率限制，IP 脱敏哈希）；按类型可见范围过滤。
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.comment import (
    COMMENT_STATUSES,
    COMMENT_TYPES,
    Comment,
    CommentRateLimit,
    Mention,
)
from app.models.project import Project
from app.models.user import User
from app.schemas.comment import CommentCreate
from app.services import collaboration_service

# 敏感词表（初版内置演示词；后续可扩展为配置/词库）
SENSITIVE_WORDS = [
    "广告代写",
    "加微信领红包",
    "赌博上分",
    "色情交易",
]

# 频率限制：同一窗口（秒）内允许的最大评论数
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_COUNT = 10

# IP 脱敏哈希盐（防彩虹表；仅存哈希不存明文 IP）
_IP_SALT = "novelcraft-comment-ip-salt-v1"

# @用户名 解析：字母/数字/下划线/中文/连字符
MENTION_RE = re.compile(r"@([A-Za-z0-9_\u4e00-\u9fff-]{1,50})")

# 评论目标类型（规格七：作品/章节/段落/大纲条目/知识库资料/版本差异）
COMMENT_TARGET_TYPES = [
    "project", "chapter", "paragraph", "outline", "knowledge", "version",
]


class CommentError(ValueError):
    """评论业务错误（映射 4xx）。"""


class SensitiveContentError(CommentError):
    """内容命中敏感词。"""


class RateLimitError(CommentError):
    """评论频率超限。"""


def _dumps(payload: dict | None) -> str | None:
    return json.dumps(payload, ensure_ascii=False) if payload is not None else None


def _loads(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def ip_hash(ip: str | None) -> str | None:
    """IP 脱敏哈希：sha256(ip + salt)，不保存原始 IP。"""
    if not ip:
        return None
    return hashlib.sha256(f"{_IP_SALT}:{ip}".encode("utf-8")).hexdigest()


def check_sensitive(content: str) -> list[str]:
    """返回内容中命中的敏感词列表。"""
    return [w for w in SENSITIVE_WORDS if w in content]


def parse_mentions(content: str) -> list[str]:
    """解析内容中的 @用户名，保留出现顺序并去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for match in MENTION_RE.finditer(content):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


async def check_rate_limit(
    db: AsyncSession,
    user_id: uuid.UUID,
    ip: str | None,
) -> None:
    """评论频率限制：同一窗口内超过上限抛 RateLimitError。"""
    now = datetime.utcnow()
    window_start = now - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    ip = ip_hash(ip)

    filters = [CommentRateLimit.window_start >= window_start]
    if user_id is not None:
        filters.append(CommentRateLimit.user_id == user_id)
    elif ip is not None:
        filters.append(CommentRateLimit.ip_hash == ip)
    else:
        raise RateLimitError("无法识别评论来源")

    row = (
        await db.execute(select(CommentRateLimit).where(*filters))
    ).scalars().first()
    if row is None:
        db.add(CommentRateLimit(
            user_id=user_id,
            ip_hash=ip,
            window_start=now,
            count=1,
        ))
        await db.flush()
        return
    if row.count >= RATE_LIMIT_MAX_COUNT:
        raise RateLimitError("评论过于频繁，请稍后再试")
    row.count += 1
    await db.flush()


async def create_comment(
    db: AsyncSession,
    project: Project,
    user: User,
    data: CommentCreate,
    *,
    ip: str | None = None,
    parent: Comment | None = None,
) -> Comment:
    """创建评论（含审核/频率限制/提及解析）。回复评论由 parent 指定时继承类型与目标。"""
    if data.visibility not in COMMENT_TYPES:
        raise CommentError(f"非法评论类型: {data.visibility}")

    hits = check_sensitive(data.content)
    if hits:
        raise SensitiveContentError(f"内容包含敏感词: {'、'.join(hits)}")

    await check_rate_limit(db, user.id, ip)

    if parent is not None:
        if parent.deleted_at is not None or parent.status == "deleted":
            raise CommentError("无法回复已删除的评论")
        if parent.status == "hidden":
            raise CommentError("无法回复已隐藏的评论")
        comment_type = parent.comment_type
        target_type = parent.target_type
        target_id = parent.target_id
    else:
        comment_type = data.visibility
        target_type = data.target_type
        target_id = data.target_id
        if target_type not in COMMENT_TARGET_TYPES:
            raise CommentError(f"非法评论目标类型: {target_type}")

    position_raw = _dumps(
        data.position_data.model_dump()
        if data.position_data is not None else None
    )

    comment = Comment(
        project_id=project.id,
        user_id=user.id,
        target_type=target_type,
        target_id=target_id,
        parent_id=parent.id if parent is not None else None,
        content=data.content,
        comment_type=comment_type,
        status="open",
        position_data=position_raw,
    )
    db.add(comment)
    await db.flush()
    return comment


async def resolve_mentions(
    db: AsyncSession,
    comment: Comment,
    actor: User,
) -> list[uuid.UUID]:
    """解析评论中的 @用户名 并写入 Mention：
    - 不存在的用户名：跳过
    - 重复提及：去重
    - 无权访问目标项目（非成员/非作者）：跳过
    返回成功创建的提及用户 ID 列表。
    """
    names = parse_mentions(comment.content)
    if not names:
        return []

    result = await db.execute(
        select(User).where(User.username.in_(names))
    )
    users = list(result.scalars().all())

    created_ids: list[uuid.UUID] = []
    for target in users:
        if target.id == actor.id:
            continue  # 不给自己发提及
        # 无权访问目标内容：不是项目作者也不是项目成员则跳过
        role = await collaboration_service.get_role(
            db, comment.project_id, target.id
        )
        if role is None:
            continue
        exists = (
            await db.execute(
                select(Mention).where(
                    Mention.comment_id == comment.id,
                    Mention.mentioned_user_id == target.id,
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        db.add(Mention(
            comment_id=comment.id,
            mentioned_user_id=target.id,
        ))
        created_ids.append(target.id)
    await db.flush()
    return created_ids


async def list_mentions(
    db: AsyncSession,
    comment_id: uuid.UUID,
) -> list[uuid.UUID]:
    result = await db.execute(
        select(Mention).where(Mention.comment_id == comment_id)
    )
    return [m.mentioned_user_id for m in result.scalars().all()]


def _comment_visible(
    comment: Comment,
    *,
    role: str | None,
    viewer_id: uuid.UUID,
    is_public_project: bool,
) -> bool:
    """按评论类型判断当前用户是否可见（规格八）。"""
    if comment.status == "deleted":
        return False
    if comment.status == "hidden" and role != "owner":
        return False
    if comment.comment_type == "private_comment":
        return comment.user_id == viewer_id
    if comment.comment_type == "review_comment":
        return role in ("owner", "editor", "reviewer")
    if comment.comment_type == "collaboration":
        return role is not None
    # public_comment：作者/成员/公开作品读者可见
    return role is not None or is_public_project


async def list_comments(
    db: AsyncSession,
    project: Project,
    viewer: User | None,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    status: str | None = None,
    parent_id: uuid.UUID | None = None,
    include_replies: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Comment], int]:
    """评论列表（按类型可见范围过滤；created_at 倒序）。"""
    role = await collaboration_service.get_role(db, project.id, viewer.id) if viewer else None
    is_public_project = project.publication_status == "published"

    filters = [
        Comment.project_id == project.id,
        Comment.deleted_at.is_(None),
    ]
    if target_type:
        filters.append(Comment.target_type == target_type)
    if target_id:
        filters.append(Comment.target_id == target_id)
    if status:
        filters.append(Comment.status == status)
    if parent_id is not None:
        filters.append(Comment.parent_id == parent_id)

    result = await db.execute(
        select(Comment).where(*filters).order_by(Comment.created_at.asc())
    )
    all_comments = list(result.scalars().all())

    visible = [
        c for c in all_comments
        if _comment_visible(
            c,
            role=role,
            viewer_id=viewer.id if viewer else uuid.UUID(int=0),
            is_public_project=is_public_project,
        )
    ]

    if not include_replies:
        visible = [c for c in visible if c.parent_id is None]

    total = len(visible)
    start = (page - 1) * page_size
    items = visible[start:start + page_size]
    return items, total


async def update_comment(
    db: AsyncSession,
    comment: Comment,
    user: User,
    *,
    content: str | None = None,
    position_data: dict | None = None,
) -> Comment:
    """修改评论：仅创建者本人。"""
    if comment.user_id != user.id:
        raise CommentError("只能修改自己创建的评论")
    if comment.deleted_at is not None or comment.status == "deleted":
        raise CommentError("评论已删除，无法修改")
    if content is not None:
        hits = check_sensitive(content)
        if hits:
            raise SensitiveContentError(f"内容包含敏感词: {'、'.join(hits)}")
        comment.content = content
    if position_data is not None:
        comment.position_data = _dumps(position_data)
    await db.flush()
    return comment


async def soft_delete_comment(
    db: AsyncSession,
    comment: Comment,
    user: User,
    *,
    role: str | None,
) -> Comment:
    """删除评论（软删）：仅创建者本人或项目作者（owner）可删除违规内容。"""
    if comment.user_id != user.id and role != "owner":
        raise CommentError("无权删除该评论")
    if comment.deleted_at is not None:
        raise CommentError("评论已删除")
    comment.status = "deleted"
    comment.deleted_at = datetime.utcnow()
    await db.flush()
    return comment


async def hide_comment(
    db: AsyncSession,
    comment: Comment,
    *,
    role: str | None,
) -> Comment:
    """隐藏不当评论：仅项目作者（owner）可隐藏。"""
    if role != "owner":
        raise CommentError("仅作者或项目管理员可隐藏评论")
    if comment.deleted_at is not None:
        raise CommentError("评论已删除")
    comment.status = "hidden"
    await db.flush()
    return comment


async def resolve_comment(
    db: AsyncSession,
    comment: Comment,
    user: User,
    *,
    role: str | None,
) -> Comment:
    """标记已解决：创建者本人或 owner/editor/reviewer 可处理。"""
    if comment.user_id != user.id and role not in ("owner", "editor", "reviewer"):
        raise CommentError("无权处理该评论")
    if comment.status == "resolved":
        return comment
    if comment.status not in ("open", "reopened"):
        raise CommentError(f"当前状态 {comment.status} 不能标记为已解决")
    comment.status = "resolved"
    await db.flush()
    return comment


async def reopen_comment(
    db: AsyncSession,
    comment: Comment,
    user: User,
    *,
    role: str | None,
) -> Comment:
    """重新打开：创建者本人或项目成员。"""
    if comment.user_id != user.id and role is None:
        raise CommentError("无权重新打开该评论")
    if comment.status != "resolved":
        raise CommentError(f"当前状态 {comment.status} 不能重新打开")
    comment.status = "reopened"
    await db.flush()
    return comment
