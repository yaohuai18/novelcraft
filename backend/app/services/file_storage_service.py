# 文件存储安全服务（第十八批「性能优化、可靠性与安全加固」规格二十二）
# 限制大小/扩展名 / MIME 校验 / 随机存储名 / 公开私有隔离 / 签名 URL / 下载次数限制。
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security_utils import generate_secure_token, random_stored_name, sha256_hex
from app.models.stored_file import StoredFile

# 扩展名 -> 允许的 MIME（白名单）
ALLOWED_TYPES: dict[str, set[str]] = {
    "jpg": {"image/jpeg", "image/jpg"},
    "jpeg": {"image/jpeg", "image/jpg"},
    "png": {"image/png"},
    "gif": {"image/gif"},
    "webp": {"image/webp"},
    "svg": {"image/svg+xml"},
    "pdf": {"application/pdf"},
    "txt": {"text/plain"},
    "md": {"text/markdown", "text/plain"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "zip": {"application/zip"},
}

# 不可直接执行的扩展名（永不注册可执行文件）
FORBIDDEN_EXT = {
    "exe", "bat", "cmd", "sh", "ps1", "js", "vbs", "msi", "dll", "so", "bin", "py",
}


async def validate_upload(
    *,
    original_name: str,
    declared_mime: str,
    content_length: int,
    is_private: bool = True,
) -> dict:
    """上传前校验：大小 / 扩展名 / MIME 白名单，生成随机存储名。"""
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if not ext:
        return {"ok": False, "reason": "缺少文件扩展名", "stored_name": None}
    if ext in FORBIDDEN_EXT:
        return {"ok": False, "reason": f"禁止上传可执行文件类型: {ext}", "stored_name": None}
    allowed_mimes = ALLOWED_TYPES.get(ext)
    if not allowed_mimes:
        return {"ok": False, "reason": f"不支持的文件扩展名: {ext}", "stored_name": None}
    if content_length > settings.file_max_bytes:
        return {
            "ok": False,
            "reason": f"文件超过大小限制 {settings.file_max_bytes} 字节",
            "stored_name": None,
        }
    if declared_mime and declared_mime.lower() not in allowed_mimes:
        return {
            "ok": False,
            "reason": f"MIME 类型校验失败: {declared_mime}",
            "stored_name": None,
        }
    return {
        "ok": True,
        "reason": None,
        "stored_name": random_stored_name(ext),
        "is_private": is_private,
    }


async def register_file(
    db: AsyncSession,
    *,
    created_by: uuid.UUID | None,
    original_name: str,
    stored_name: str,
    mime_type: str,
    file_size: int,
    is_private: bool = True,
    max_downloads: int | None = None,
    expires_in_minutes: int | None = None,
) -> StoredFile:
    """文件元数据落库（内容实体由存储层按 stored_name 隔离存放）。"""
    row = StoredFile(
        created_by=created_by,
        original_name=original_name[:255],
        stored_name=stored_name,
        mime_type=mime_type,
        file_size=file_size,
        is_private=is_private,
        max_downloads=max_downloads,
        expires_at=(
            datetime.utcnow() + timedelta(minutes=expires_in_minutes)
            if expires_in_minutes
            else None
        ),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_by_stored_name(
    db: AsyncSession,
    stored_name: str,
) -> StoredFile | None:
    return (
        await db.execute(
            select(StoredFile).where(StoredFile.stored_name == stored_name)
        )
    ).scalar_one_or_none()


async def check_download_allowed(
    db: AsyncSession,
    *,
    file_row: StoredFile,
    user_id: uuid.UUID | None,
) -> tuple[bool, str]:
    """下载前校验：过期 / 私有归属 / 下载次数上限。"""
    now = datetime.utcnow()
    if file_row.expires_at is not None and file_row.expires_at <= now:
        return False, "文件已过期"
    if file_row.is_private and (user_id is None or file_row.created_by != user_id):
        return False, "无权访问私有文件"
    if (
        file_row.max_downloads is not None
        and file_row.download_count >= file_row.max_downloads
    ):
        return False, "下载次数已达上限"
    return True, "ok"


async def count_download(db: AsyncSession, file_row: StoredFile) -> None:
    file_row.download_count += 1
    await db.commit()


async def list_files(
    db: AsyncSession,
    *,
    created_by: uuid.UUID | None = None,
    is_private: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[StoredFile], int]:
    filters = []
    if created_by is not None:
        filters.append(StoredFile.created_by == created_by)
    if is_private is not None:
        filters.append(StoredFile.is_private == is_private)
    total = (
        await db.execute(select(func.count(StoredFile.id)).where(*filters))
    ).scalar_one()
    rows = (
        await db.execute(
            select(StoredFile)
            .where(*filters)
            .order_by(StoredFile.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return list(rows), total


def sign_url(stored_name: str, *, ttl_seconds: int = 300) -> str:
    """短期签名 URL：HMAC(SHA-256) 签名，默认 5 分钟有效。"""
    import hmac

    exp = int(datetime.utcnow().timestamp()) + ttl_seconds
    msg = f"{stored_name}:{exp}"
    sig = hmac.new(
        settings.jwt_secret_key.encode("utf-8"),
        msg.encode("utf-8"),
        "sha256",
    ).hexdigest()
    return f"/api/security/files/{stored_name}/download?sig={sig}&exp={exp}"


def verify_signature(stored_name: str, sig: str, exp: int, *, ttl_seconds: int = 300) -> bool:
    """校验签名 URL 真实性 + 未过期。"""
    import hmac

    if abs(int(datetime.utcnow().timestamp()) - int(exp)) > ttl_seconds:
        return False
    msg = f"{stored_name}:{exp}"
    expected = hmac.new(
        settings.jwt_secret_key.encode("utf-8"),
        msg.encode("utf-8"),
        "sha256",
    ).hexdigest()
    return hmac.compare_digest(expected, sig or "")
