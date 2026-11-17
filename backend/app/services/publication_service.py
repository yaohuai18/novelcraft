# 发布与阅读模式核心服务（第十三批规格实现）
# 覆盖：slug 生成与唯一化、正文格式转换、发布前检查、章节发布/撤回/归档/恢复、
#       作品发布/发布信息更新、公开阅读接口、作者预览、阅读设置与阅读进度。
import json
import re
import uuid
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chapter_content import ChapterContent
from app.models.outline import Outline
from app.models.project import Project
from app.models.publication import (
    CHAPTER_VISIBILITIES,
    PUBLICATION_STATUSES,
    Publication,
)
from app.models.reader import ReaderPreference, ReadingProgress
from app.models.tag import Tag
from app.models.user import User
from app.schemas.publication import (
    PreviewChapterResponse,
    PreviewProjectResponse,
    ProjectPublishResponse,
    PublicChapterResponse,
    PublicChapterSummary,
    PublicProjectResponse,
    PublicVolumeItem,
    PublicationCreate,
    PublicationResponse,
    PublicationUpdate,
    PublishCheckIssue,
    PublishCheckResponse,
    ReaderPreferenceResponse,
    ReaderPreferenceUpdate,
    ReadingProgressListResponse,
    ReadingProgressResponse,
    ReadingProgressUpdate,
)

from app.services.baseline_service import collect_baseline

# ---------- slug ----------

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")


def slugify(text: str, fallback: str = "item") -> str:
    """将任意标题转成合法 slug：小写、非字母数字转中划线、压缩连续中划线。
    含中文字符时先转拼音（保证公开链接可读且稳定），英文/数字保持原样。"""
    raw = text.strip().lower()
    if re.search(r"[\u4e00-\u9fff]", raw):
        from pypinyin import lazy_pinyin

        raw = "-".join(lazy_pinyin(raw))
    s = re.sub(r"[^a-zA-Z0-9]+", "-", raw)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not s:
        s = fallback
    return s


async def ensure_unique_slug(
    db: AsyncSession,
    model,
    column,
    base: str,
    exclude_id: uuid.UUID | None = None,
) -> str:
    """保证 slug 唯一：冲突时追加 -2/-3/... 序号。"""
    candidate = base
    counter = 1
    while True:
        query = select(model.id).where(column == candidate)
        if exclude_id is not None:
            query = query.where(model.id != exclude_id)
        result = await db.execute(query)
        if result.scalar_one_or_none() is None:
            return candidate
        counter += 1
        candidate = f"{base}-{counter}"


def validate_slug(raw: str) -> str:
    """校验用户自定义 slug 的合法性（仅小写字母/数字/中划线）。"""
    s = raw.strip().lower()
    if not SLUG_RE.match(s) or len(s) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="slug 仅允许小写字母、数字与中划线（不能以中划线开头或结尾）"
        )
    return s


# ---------- 正文格式转换 ----------

PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")
INTERNAL_LINK_RE = re.compile(r"\[\[([^\]]*)\]\]")
INTERNAL_TAG_LINE_RE = re.compile(r"^\s*(?:【[^】]+】|\[(?:备注|内部|AI|编辑|一致性)[：:]\s*.*)\s*$")
DANGEROUS_HTML_RE = re.compile(
    r"<\s*(script|iframe|object|embed|style|link|meta|form)\b|"
    r"javascript\s*:|on[a-z]+\s*=",
    re.IGNORECASE,
)
CONSISTENCY_MARK_RE = re.compile(r"\[一致性待处理\]|【一致性[^】]*待处理[^】]*】")


def _clean_internal_markers(text: str) -> str:
    """清理编辑器内部标记：HTML 注释、内部标签行、占位符、内部引用。"""
    # 删除 HTML 注释（含多行）
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # 删除内部标签整行（【备注】【AI】【内部】等，或 [备注]: xxx 形式）
    lines = []
    for line in text.splitlines():
        if INTERNAL_TAG_LINE_RE.match(line):
            continue
        lines.append(line)
    text = "\n".join(lines)
    # 占位符 {{...}} → 去花括号保留文本（发布前检查会阻止未替换占位符发布）
    text = PLACEHOLDER_RE.sub(lambda m: m.group(0)[2:-2].strip(), text)
    # 内部引用 [[...]] → 去括号保留文本
    text = INTERNAL_LINK_RE.sub(lambda m: m.group(1).strip(), text)
    return text


def _inline_markdown(text: str) -> str:
    """行内格式：`code`、**粗体**、*斜体*。"""
    # 行内代码优先（避免代码内的 * 被误转）
    code_parts = []
    def _hold_code(m):
        code_parts.append(m.group(0))
        return f"\x00CODE{len(code_parts) - 1}\x00"
    text = re.sub(r"`([^`]+)`", _hold_code, text)
    # 粗体
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    # 斜体
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    # 还原行内代码
    def _restore_code(m):
        idx = int(m.group(1))
        return f"<code>{code_parts[idx][1:-1]}</code>"
    text = re.sub(r"\x00CODE(\d+)\x00", _restore_code, text)
    return text


def _render_block(text: str) -> str:
    """将一块文本渲染为 HTML（标题/引用/分隔线/列表/段落）。"""
    lines = text.split("\n")

    # 标题
    m = re.match(r"^(#{1,6})\s+(.+)$", lines[0])
    if m:
        level = len(m.group(1))
        body = _inline_markdown(m.group(2).strip())
        return f"<h{level}>{body}</h{level}>"

    # 分隔线
    if len(lines) == 1 and re.match(r"^(-{3,}|\*{3,}|_{3,})$", lines[0].strip()):
        return "<hr>"

    # 引用：连续 > 行（内容已 html.escape，> 为 &gt;）
    if all(re.match(r"^\s*&gt;", line) for line in lines if line.strip()):
        inner = "\n".join(
            _inline_markdown(re.sub(r"^\s*&gt;\s?", "", line))
            for line in lines if line.strip()
        )
        return f"<blockquote>{inner}</blockquote>"

    # 无序列表
    if all(re.match(r"^\s*[-*]\s+", line) for line in lines if line.strip()):
        item_tags = []
        for line in lines:
            if not line.strip():
                continue
            item_text = _inline_markdown(re.sub(r"^\s*[-*]\s+", "", line).strip())
            item_tags.append(f"<li>{item_text}</li>")
        return f"<ul>{''.join(item_tags)}</ul>"

    # 有序列表
    if all(re.match(r"^\s*\d+[.、]\s+", line) for line in lines if line.strip()):
        item_tags = []
        for line in lines:
            if not line.strip():
                continue
            item_text = _inline_markdown(re.sub(r"^\s*\d+[.、]\s+", "", line).strip())
            item_tags.append(f"<li>{item_text}</li>")
        return f"<ol>{''.join(item_tags)}</ol>"

    # 普通段落（多行合并）
    body = _inline_markdown(" ".join(line.strip() for line in lines if line.strip()))
    return f"<p>{body}</p>"


def convert_editor_content(text: str) -> tuple[str, str, int]:
    """编辑器内部内容 → 阅读内容。

    步骤：清理内部标记 → 转义危险 HTML（禁止任意 HTML）→ Markdown 转换。
    返回 (html, 纯文本, 字数)。
    """
    cleaned = _clean_internal_markers(text or "")
    # 转义全部 HTML，确保任何原始标签都不会进入阅读页
    import html as html_lib

    escaped = html_lib.escape(cleaned, quote=True)

    # 分块：空行分隔，逐块渲染
    blocks = [b.strip() for b in re.split(r"\n\s*\n", escaped) if b.strip()]
    parts = []
    for block in blocks:
        parts.append(_render_block(block))

    html_out = "\n".join(parts)

    # 纯文本与字数
    plain = re.sub(r"<[^>]+>", "", html_out)
    plain = re.sub(r"\s+", "", plain).strip()
    word_count = len(plain)

    return html_out, plain, word_count


# ---------- 发布前检查 ----------

async def _get_chapter_body(db: AsyncSession, outline_id: uuid.UUID) -> str:
    result = await db.execute(
        select(ChapterContent).where(ChapterContent.outline_id == outline_id)
    )
    content = result.scalar_one_or_none()
    if content is not None:
        return content.content or ""
    # 无 ChapterContent 时回退到大纲节点自身的 content 字段
    outline_result = await db.execute(
        select(Outline.content).where(Outline.id == outline_id)
    )
    return outline_result.scalar_one_or_none() or ""


async def check_publish_ready(
    db: AsyncSession,
    outline: Outline,
) -> PublishCheckResponse:
    """发布前检查：正文/标题/顺序/占位符/危险 HTML/高等级一致性。"""
    issues: list[PublishCheckIssue] = []

    body = await _get_chapter_body(db, outline.id)

    # 1. 正文是否存在
    if not body or not body.strip():
        issues.append(PublishCheckIssue(
            severity="high", message="章节正文为空，无法发布"
        ))
    else:
        # 4. 占位符
        placeholders = PLACEHOLDER_RE.findall(body)
        if placeholders:
            issues.append(PublishCheckIssue(
                severity="high",
                message=f"正文中仍存在占位符：{placeholders[0][:50]}"
            ))
        # 5. 危险 HTML
        danger = DANGEROUS_HTML_RE.search(body)
        if danger:
            issues.append(PublishCheckIssue(
                severity="high",
                message=f"正文包含危险 HTML 标记：{danger.group(0)[:50]}"
            ))
        # 6. 未解决的高等级一致性问题
        if CONSISTENCY_MARK_RE.search(body):
            issues.append(PublishCheckIssue(
                severity="high",
                message="存在未解决的高等级一致性问题（一致性待处理标记）"
            ))
        # 低等级：内部备注行（发布时会清理，不影响阅读）
        if re.search(r"^\s*【[^】]+】", body, flags=re.MULTILINE):
            issues.append(PublishCheckIssue(
                severity="low",
                message="正文包含内部标签行（发布时将清理，不显示在阅读页）"
            ))

    # 2. 标题为空
    if not outline.title or not outline.title.strip():
        issues.append(PublishCheckIssue(
            severity="high", message="章节标题为空，无法发布"
        ))

    # 3. 章节顺序是否有效
    if outline.sort_order is None or outline.sort_order < 1:
        issues.append(PublishCheckIssue(
            severity="medium", message="章节顺序无效（sort_order 需 >= 1）"
        ))

    severity_order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: severity_order.get(i.severity, 9))

    can_publish = all(i.severity != "high" for i in issues)
    return PublishCheckResponse(can_publish=can_publish, issues=issues)


# ---------- 章节发布 / 撤回 / 归档 / 恢复 ----------

async def publish_chapter(
    db: AsyncSession,
    project: Project,
    outline: Outline,
    data: PublicationCreate,
    *,
    commit: bool = True,
) -> PublicationResponse:
    """章节发布：检查 → 转换正文 → 创建/更新发布快照 → 章节状态置 published。

    U1b：`commit=False` 时本函数只 flush（拿到 id / 时间戳等响应序列化字段），
    业务事务由调用方一次性提交，避免服务内提交造成的多事务分裂。
    """
    if project.publication_status == "archived":
        # D-U2A-4：作品归档属状态冲突，统一 409（不再用误导性的 400）
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已归档的作品不能发布章节"
        )

    check = await check_publish_ready(db, outline)
    if not check.can_publish:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "can_publish": False,
                "issues": [i.model_dump() for i in check.issues],
            },
        )

    # 可见性校验
    visibility = data.visibility or outline.visibility or "private"
    if visibility not in CHAPTER_VISIBILITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"非法可见性：{visibility}"
        )

    body = await _get_chapter_body(db, outline.id)
    html_out, plain, word_count = convert_editor_content(body)

    now = datetime.utcnow()

    # 章节 slug（未设置则自动生成并全局唯一化）
    if not outline.chapter_slug:
        base = slugify(outline.title or "chapter", fallback="chapter")
        outline.chapter_slug = await ensure_unique_slug(
            db, Outline, Outline.chapter_slug, base, exclude_id=outline.id
        )

    # 查询已有发布记录
    result = await db.execute(
        select(Publication).where(Publication.chapter_id == outline.id)
    )
    publication = result.scalar_one_or_none()

    if publication is None:
        publication = Publication(
            project_id=project.id,
            chapter_id=outline.id,
            title=data.title or outline.title,
            description=data.description,
            cover_text=data.cover_text,
            content=html_out,
            content_text=plain,
            word_count=word_count,
            status="published",
            visibility=visibility,
            published_at=now,
        )
        db.add(publication)
    else:
        publication.title = data.title or outline.title
        if data.description is not None:
            publication.description = data.description
        if data.cover_text is not None:
            publication.cover_text = data.cover_text
        publication.content = html_out
        publication.content_text = plain
        publication.word_count = word_count
        publication.status = "published"
        publication.visibility = visibility
        if publication.published_at is None:
            publication.published_at = now

    # 章节可见性属于快照通道语义（R-5），随发布记录一起维护。
    # U2a：发布三列（lifecycle_status / status / published_at）不再由本函数写入，
    # 统一由 chapter_lifecycle.mark_published 在同一业务事务内写透。
    outline.visibility = visibility

    # 作品状态：发布章节即公开作品
    if project.publication_status in ("draft", "review"):
        project.publication_status = "published"
        project.published_at = now
    project.last_published_at = now
    if not project.project_slug:
        project.project_slug = await ensure_unique_slug(
            db, Project, Project.project_slug, slugify(project.title or "project")
        )

    # 阶段C「发布基线绑定」：发布时自动收集当前版本基线写入发布记录。
    # 生成失败降级 NULL，不阻断发布（方案：字段可空、旧发布不追溯）。
    try:
        baseline = await collect_baseline(db, project.id, outline.id)
        publication.baseline_meta = json.dumps(baseline.get("meta"), ensure_ascii=False)
        publication.baseline_snapshot = json.dumps(baseline.get("snapshot"), ensure_ascii=False)
    except Exception:  # noqa: BLE001
        publication.baseline_meta = None
        publication.baseline_snapshot = None

    if commit:
        await db.commit()
    else:
        # U1b：只落 SQL，不提交；事务边界交给调用方（同一事务内才保证 status/lifecycle 一致）
        await db.flush()
    await db.refresh(publication)
    return PublicationResponse.model_validate(publication)


async def sync_publish_side_effects(
    db: AsyncSession,
    project: Project,
    chapter: Outline | None = None,
    *,
    commit: bool = False,
) -> None:
    """发布/撤回/归档/恢复的索引与统计同步（U2a 唯一同步入口）。

    - 索引与统计各自包裹在既有 savepoint 隔离逻辑内（``index_service._safe_sync``），
      失败只记录、不回滚业务写入；
    - ``commit=False``：同步写入留在业务事务内但**不单独提交**，由调用方唯一
      commit 收口；任何情况下都不把同步视为业务事务的一部分（失败不回滚业务）；
    - 三入口（E1/E2/E3）的成功路径统一经 ``publish_orchestrator`` 调用本函数。
    """
    from app.services import discover_service, index_service

    await index_service._safe_sync(
        db, "sync_project_index", project.id, "project", project.id,
        index_service.sync_project_index(db, project),
    )
    if chapter is not None:
        await index_service._safe_sync(
            db, "update_doc_visibility", project.id, "chapter_content", chapter.id,
            index_service.update_doc_visibility(
                db, project.id, "chapter_content", chapter.id,
                status=chapter.status,
                visibility=chapter.visibility,
            ),
        )
    await index_service._safe_sync(
        db, "refresh_project_stats", project.id, "project", project.id,
        discover_service.refresh_project_stats(db, project.id),
    )
    if commit:
        await db.commit()


async def unpublish_chapter(
    db: AsyncSession,
    project: Project,
    outline: Outline,
) -> PublicationResponse:
    """撤回发布：章节与发布记录置为 draft，链接不可访问、目录隐藏，历史进度保留。"""
    if outline.status not in ("published", "unlisted"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="章节当前未发布，无法撤回"
        )

    result = await db.execute(
        select(Publication).where(Publication.chapter_id == outline.id)
    )
    publication = result.scalar_one_or_none()

    outline.status = "draft"
    if publication is not None:
        publication.status = "draft"

    await db.commit()
    if publication is not None:
        await db.refresh(publication)
        return PublicationResponse.model_validate(publication)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="发布记录不存在"
    )


async def archive_chapter(
    db: AsyncSession,
    outline: Outline,
) -> PublicationResponse:
    """归档章节：不允许普通编辑、目录隐藏、仍可在作者后台查看、可恢复为草稿。"""
    result = await db.execute(
        select(Publication).where(Publication.chapter_id == outline.id)
    )
    publication = result.scalar_one_or_none()

    outline.status = "archived"
    if publication is not None:
        publication.status = "archived"

    await db.commit()
    if publication is not None:
        await db.refresh(publication)
        return PublicationResponse.model_validate(publication)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="发布记录不存在"
    )


async def restore_chapter(
    db: AsyncSession,
    outline: Outline,
) -> PublicationResponse:
    """恢复：归档/撤回的章节恢复为草稿（可继续编辑与重新发布）。"""
    result = await db.execute(
        select(Publication).where(Publication.chapter_id == outline.id)
    )
    publication = result.scalar_one_or_none()

    outline.status = "draft"
    if publication is not None:
        publication.status = "draft"

    await db.commit()
    if publication is not None:
        await db.refresh(publication)
        return PublicationResponse.model_validate(publication)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="发布记录不存在"
    )


async def get_publication(
    db: AsyncSession,
    outline: Outline,
) -> PublicationResponse:
    result = await db.execute(
        select(Publication).where(Publication.chapter_id == outline.id)
    )
    publication = result.scalar_one_or_none()
    if publication is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="发布记录不存在"
        )
    return PublicationResponse.model_validate(publication)


# ---------- 作品发布 / 发布信息更新 ----------

async def publish_project(
    db: AsyncSession,
    project: Project,
    force: bool = False,
) -> ProjectPublishResponse:
    """作品发布：至少一章已发布，且无高等级问题（force 仅忽略低/中等级）。"""
    if project.publication_status == "archived":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="已归档的作品不能发布"
        )

    chapter_result = await db.execute(
        select(Outline).where(
            Outline.project_id == project.id,
            Outline.outline_type == "chapter",
        ).order_by(Outline.sort_order.asc())
    )
    chapters = list(chapter_result.scalars().all())

    published_chapters = [c for c in chapters if c.status == "published"]
    if not published_chapters:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="作品下没有已发布章节，请先发布章节"
        )

    # 对全部章节做发布前检查，收集高等级问题
    high_issues: list[dict] = []
    for c in chapters:
        check = await check_publish_ready(db, c)
        for issue in check.issues:
            if issue.severity == "high":
                high_issues.append({
                    "chapter_id": str(c.id),
                    "severity": issue.severity,
                    "message": issue.message,
                })
    if high_issues and not force:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "can_publish": False,
                "issues": high_issues,
            },
        )

    now = datetime.utcnow()
    project.publication_status = "published"
    if project.published_at is None:
        project.published_at = now
    project.last_published_at = now
    if not project.project_slug:
        project.project_slug = await ensure_unique_slug(
            db, Project, Project.project_slug, slugify(project.title or "project")
        )

    await db.commit()
    await db.refresh(project)
    return ProjectPublishResponse(
        project_id=project.id,
        status=project.publication_status,
        project_slug=project.project_slug,
        published_at=project.published_at,
        message="作品已发布",
    )


async def update_publication_info(
    db: AsyncSession,
    project: Project,
    data: PublicationUpdate,
) -> ProjectPublishResponse:
    """更新作品公开信息（名称/副标题/简介/封面文字/发布状态/slug）。"""
    if data.title is not None:
        project.title = data.title
    if data.subtitle is not None:
        project.subtitle = data.subtitle
    if data.description is not None:
        project.description = data.description
    if data.cover_text is not None:
        project.cover_text = data.cover_text
    if data.status is not None:
        if data.status not in PUBLICATION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"非法发布状态：{data.status}"
            )
        project.publication_status = data.status
    if data.project_slug is not None:
        slug = validate_slug(data.project_slug)
        project.project_slug = await ensure_unique_slug(
            db, Project, Project.project_slug, slug, exclude_id=project.id
        )

    await db.commit()
    await db.refresh(project)
    return ProjectPublishResponse(
        project_id=project.id,
        status=project.publication_status,
        project_slug=project.project_slug,
        published_at=project.published_at,
        message="发布信息已更新",
    )


# ---------- 公开阅读 ----------

async def resolve_project_ref(
    db: AsyncSession,
    ref: str,
) -> Project:
    """按 UUID 或 slug 解析作品。"""
    try:
        project_id = uuid.UUID(ref)
        result = await db.execute(select(Project).where(Project.id == project_id))
    except (ValueError, AttributeError):
        result = await db.execute(select(Project).where(Project.project_slug == ref))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在"
        )
    return project


async def resolve_chapter_ref(
    db: AsyncSession,
    project: Project,
    ref: str,
) -> Outline:
    """按 UUID 或 slug 解析项目内章节。"""
    try:
        chapter_id = uuid.UUID(ref)
        result = await db.execute(
            select(Outline).where(
                Outline.id == chapter_id,
                Outline.project_id == project.id,
                Outline.outline_type == "chapter",
            )
        )
    except (ValueError, AttributeError):
        result = await db.execute(
            select(Outline).where(
                Outline.chapter_slug == ref,
                Outline.project_id == project.id,
                Outline.outline_type == "chapter",
            )
        )
    chapter = result.scalar_one_or_none()
    if chapter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="章节不存在"
        )
    return chapter


def _ensure_public_project(project: Project) -> None:
    if project.publication_status not in ("published", "unlisted"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品未公开"
        )


def _is_chapter_public(chapter: Outline) -> bool:
    return (
        chapter.status in ("published", "unlisted")
        and chapter.visibility in ("public", "link_only")
    )


def _is_chapter_in_toc(chapter: Outline) -> bool:
    return chapter.status == "published" and chapter.visibility == "public"


async def _build_toc(
    db: AsyncSession,
    project: Project,
    visible_only: bool,
) -> list[PublicVolumeItem]:
    """构建目录（按分卷分组）。

    visible_only=True：公开目录（隐藏草稿章节/未发布分卷/非 public 可见性）。
    visible_only=False：作者预览（全部章节与分卷）。
    """
    chapter_result = await db.execute(
        select(Outline)
        .where(
            Outline.project_id == project.id,
            Outline.outline_type == "chapter",
        )
        .order_by(Outline.sort_order.asc())
    )
    chapters = list(chapter_result.scalars().all())

    volume_result = await db.execute(
        select(Outline)
        .where(
            Outline.project_id == project.id,
            Outline.outline_type == "volume",
        )
        .order_by(Outline.sort_order.asc())
    )
    volumes = list(volume_result.scalars().all())

    # 已发布分卷（公开目录只显示已发布分卷）
    published_volumes = {v.id: v for v in volumes if v.status == "published"}

    # 发布快照（word_count / published_at / title）
    pub_result = await db.execute(
        select(Publication).where(Publication.project_id == project.id)
    )
    pubs = {p.chapter_id: p for p in list(pub_result.scalars().all())}

    # 筛选
    items = []
    for c in chapters:
        if visible_only:
            if not _is_chapter_in_toc(c):
                continue
            # 未发布分卷下的章节隐藏
            if c.parent_id is not None and c.parent_id not in published_volumes:
                continue
        items.append(c)

    # 排序：有卷章节按 (卷 sort_order, 章节 sort_order)；无卷章节按章节 sort_order 排在卷组之后
    def volume_sort_key(c):
        vol = published_volumes.get(c.parent_id) if c.parent_id else None
        if vol is not None:
            return (0, vol.sort_order, c.sort_order)
        # 预览模式下，未发布卷的章节也跟随其卷排序
        if not visible_only and c.parent_id is not None:
            parent_map = {v.id: v for v in volumes}
            pv = parent_map.get(c.parent_id)
            if pv is not None:
                return (0, pv.sort_order, c.sort_order)
        return (1, 0, c.sort_order)

    items.sort(key=volume_sort_key)

    # 分组
    groups: list[PublicVolumeItem] = []
    tail_group = PublicVolumeItem(volume_id=None, title=None, chapters=[])
    chapter_number = 0
    for c in items:
        chapter_number += 1
        pub = pubs.get(c.id)
        summary = PublicChapterSummary(
            chapter_id=c.id,
            title=c.title or "",
            chapter_number=chapter_number,
            word_count=pub.word_count if pub else 0,
            published_at=pub.published_at if pub else c.published_at,
            slug=c.chapter_slug,
            visibility=c.visibility,
        )
        vol = None
        if c.parent_id is not None:
            if visible_only:
                vol = published_volumes.get(c.parent_id)
            else:
                parent_map = {v.id: v for v in volumes}
                vol = parent_map.get(c.parent_id)
        if vol is not None:
            group = next(
                (g for g in groups if g.volume_id == vol.id),
                None,
            )
            if group is None:
                group = PublicVolumeItem(
                    volume_id=vol.id,
                    title=vol.title,
                    chapters=[],
                )
                groups.append(group)
            group.chapters.append(summary)
        else:
            tail_group.chapters.append(summary)

    result = groups + ([tail_group] if tail_group.chapters else [])
    return result


async def get_public_project(
    db: AsyncSession,
    project: Project,
) -> PublicProjectResponse:
    """公开作品首页：公开字段 + 目录（隐藏草稿章节/未发布分卷）。"""
    _ensure_public_project(project)

    user_result = await db.execute(select(User).where(User.id == project.user_id))
    user = user_result.scalar_one_or_none()

    tag_result = await db.execute(
        select(Tag.name).where(Tag.project_id == project.id).order_by(Tag.name.asc())
    )
    tags = list(tag_result.scalars().all())

    volumes = await _build_toc(db, project, visible_only=True)
    chapter_count = sum(len(v.chapters) for v in volumes)
    word_count_total = sum(
        sum(c.word_count for c in v.chapters) for v in volumes
    )

    return PublicProjectResponse(
        project_id=project.id,
        title=project.title,
        subtitle=project.subtitle,
        description=project.description,
        cover_text=project.cover_text,
        author_name=user.username if user else "未知作者",
        genre=project.genre,
        tags=tags,
        status=project.publication_status,
        updated_at=project.updated_at,
        slug=project.project_slug,
        volumes=volumes,
        chapter_count=chapter_count,
        word_count_total=word_count_total,
    )


async def _get_navigable_chapters(
    db: AsyncSession,
    project: Project,
) -> list[Outline]:
    """导航范围：通过链接可访问的章节（published/unlisted + public/link_only）。"""
    chapter_result = await db.execute(
        select(Outline)
        .where(
            Outline.project_id == project.id,
            Outline.outline_type == "chapter",
        )
        .order_by(Outline.sort_order.asc())
    )
    chapters = [c for c in list(chapter_result.scalars().all()) if _is_chapter_public(c)]
    chapters.sort(key=lambda c: c.sort_order)
    return chapters


async def get_public_chapter(
    db: AsyncSession,
    project: Project,
    chapter: Outline,
) -> PublicChapterResponse:
    """公开单章正文（含上一章/下一章）。"""
    _ensure_public_project(project)
    if not _is_chapter_public(chapter):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="章节不可访问"
        )

    pub_result = await db.execute(
        select(Publication).where(Publication.chapter_id == chapter.id)
    )
    pub = pub_result.scalar_one_or_none()
    if pub is None or pub.status != "published":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="章节未发布"
        )

    navigable = await _get_navigable_chapters(db, project)
    idx = next((i for i, c in enumerate(navigable) if c.id == chapter.id), None)

    prev_c = navigable[idx - 1] if idx and idx > 0 else None
    next_c = navigable[idx + 1] if idx is not None and idx + 1 < len(navigable) else None

    return PublicChapterResponse(
        project_id=project.id,
        chapter_id=chapter.id,
        title=pub.title,
        chapter_number=(idx + 1) if idx is not None else 0,
        content=pub.content,
        word_count=pub.word_count,
        published_at=pub.published_at,
        updated_at=pub.updated_at,
        slug=chapter.chapter_slug,
        visibility=chapter.visibility,
        prev_chapter_id=prev_c.id if prev_c else None,
        prev_chapter_slug=prev_c.chapter_slug if prev_c else None,
        prev_chapter_title=prev_c.title if prev_c else None,
        next_chapter_id=next_c.id if next_c else None,
        next_chapter_slug=next_c.chapter_slug if next_c else None,
        next_chapter_title=next_c.title if next_c else None,
    )


# ---------- 作者预览 ----------

async def preview_project(
    db: AsyncSession,
    project: Project,
) -> PreviewProjectResponse:
    """作者预览作品首页/目录：不改变发布状态。"""
    volumes = await _build_toc(db, project, visible_only=False)
    return PreviewProjectResponse(
        project_id=project.id,
        title=project.title,
        subtitle=project.subtitle,
        description=project.description,
        cover_text=project.cover_text,
        genre=project.genre,
        publication_status=project.publication_status,
        slug=project.project_slug,
        volumes=volumes,
        preview=True,
    )


async def preview_chapter(
    db: AsyncSession,
    project: Project,
    outline: Outline,
) -> PreviewChapterResponse:
    """作者预览单章正文：基于当前编辑稿转换，不改变发布状态。"""
    body = await _get_chapter_body(db, outline.id)
    html_out, plain, word_count = convert_editor_content(body)
    return PreviewChapterResponse(
        project_id=project.id,
        chapter_id=outline.id,
        title=outline.title or "",
        content=html_out,
        content_text=plain,
        word_count=word_count,
        status=outline.status,
        visibility=outline.visibility,
        preview=True,
    )


# ---------- 阅读设置 ----------

DEFAULT_PREFERENCE = {
    "font_size": 16,
    "line_height": 1.6,
    "content_width": 720,
    "theme": "light",
    "font_family": "default",
}


async def get_preference(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> ReaderPreferenceResponse:
    result = await db.execute(
        select(ReaderPreference).where(ReaderPreference.user_id == user_id)
    )
    pref = result.scalar_one_or_none()
    if pref is None:
        return ReaderPreferenceResponse(
            user_id=user_id,
            updated_at=datetime.utcnow(),
            **DEFAULT_PREFERENCE,
        )
    return ReaderPreferenceResponse.model_validate(pref)


async def upsert_preference(
    db: AsyncSession,
    user_id: uuid.UUID,
    data: ReaderPreferenceUpdate,
) -> ReaderPreferenceResponse:
    result = await db.execute(
        select(ReaderPreference).where(ReaderPreference.user_id == user_id)
    )
    pref = result.scalar_one_or_none()

    updates = data.model_dump(exclude_unset=True)
    if pref is None:
        merged = dict(DEFAULT_PREFERENCE)
        merged.update(updates)
        pref = ReaderPreference(user_id=user_id, **merged)
        db.add(pref)
    else:
        for k, v in updates.items():
            setattr(pref, k, v)

    await db.commit()
    await db.refresh(pref)
    return ReaderPreferenceResponse.model_validate(pref)


# ---------- 阅读进度 ----------

async def _ensure_progress_target(
    db: AsyncSession,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
) -> None:
    project_result = await db.execute(
        select(Project.id).where(Project.id == project_id)
    )
    if project_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="作品不存在"
        )
    chapter_result = await db.execute(
        select(Outline.id).where(
            Outline.id == chapter_id,
            Outline.project_id == project_id,
            Outline.outline_type == "chapter",
        )
    )
    if chapter_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="章节不存在"
        )


async def upsert_progress(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    data: ReadingProgressUpdate,
) -> ReadingProgressResponse:
    await _ensure_progress_target(db, project_id, chapter_id)

    result = await db.execute(
        select(ReadingProgress).where(
            ReadingProgress.user_id == user_id,
            ReadingProgress.project_id == project_id,
            ReadingProgress.chapter_id == chapter_id,
        )
    )
    progress = result.scalar_one_or_none()

    now = datetime.utcnow()
    if progress is None:
        progress = ReadingProgress(
            user_id=user_id,
            project_id=project_id,
            chapter_id=chapter_id,
            position=data.position if data.position is not None else 0,
            last_read_at=now,
            completed=bool(data.completed),
        )
        db.add(progress)
    else:
        if data.position is not None:
            progress.position = data.position
        if data.completed is not None:
            progress.completed = data.completed
        progress.last_read_at = now

    await db.commit()
    await db.refresh(progress)
    return ReadingProgressResponse.model_validate(progress)


async def get_progress(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
) -> ReadingProgressResponse:
    result = await db.execute(
        select(ReadingProgress).where(
            ReadingProgress.user_id == user_id,
            ReadingProgress.project_id == project_id,
            ReadingProgress.chapter_id == chapter_id,
        )
    )
    progress = result.scalar_one_or_none()
    if progress is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="阅读进度不存在"
        )
    return ReadingProgressResponse.model_validate(progress)


async def list_progress(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> ReadingProgressListResponse:
    result = await db.execute(
        select(ReadingProgress)
        .where(
            ReadingProgress.user_id == user_id,
            ReadingProgress.project_id == project_id,
        )
        .order_by(ReadingProgress.last_read_at.desc())
    )
    items = [
        ReadingProgressResponse.model_validate(p)
        for p in list(result.scalars().all())
    ]
    return ReadingProgressListResponse(
        project_id=project_id,
        items=items,
        latest=items[0] if items else None,
    )
