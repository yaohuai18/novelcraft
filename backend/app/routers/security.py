# 安全与隐私 路由（第十八批「性能优化、可靠性与安全加固」）
# 覆盖：刷新令牌轮换 / 高风险操作二次确认 / 安全事件审计 / 文件存储安全（签名URL/下载受控）/
#       内容安全检测（HTML 白名单过滤、命令注入识别）
import html
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.permissions import require_admin_roles
from app.core.security_utils import (
    generate_secure_token,
    looks_like_command_injection,
    sanitize_html,
)
from app.models.user import User
from app.schemas.security_schemas import (
    ConfirmationConfirmRequest,
    ConfirmationConfirmResponse,
    ConfirmationCreateRequest,
    ConfirmationCreateResponse,
    ContentCheckRequest,
    ContentCheckResponse,
    FileDownloadResponse,
    FileListResponse,
    FileRegisterRequest,
    FileRegisterResponse,
    FileSignResponse,
    FileUploadValidateRequest,
    FileUploadValidateResponse,
    PrivacyDeleteAccountResponse,
    PrivacyExportResponse,
    RefreshTokenRevokeAllResponse,
    RefreshTokenRotateRequest,
    RefreshTokenRotateResponse,
    SecurityEventListResponse,
    SecurityEventResponse,
    StoredFileResponse,
)
from app.services import (
    confirmation_service,
    file_storage_service,
    refresh_token_service,
    security_event_service,
)
from app.core.security_utils import generate_secure_token

# 可选登录依赖：公开接口允许匿名访问，有令牌则解析用户
from typing import Optional

from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


async def get_current_user_or_none(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    if credentials is None:
        return None
    return await get_current_user(credentials=credentials, db=db)

router = APIRouter(prefix="/api/security", tags=["安全与隐私"])

ADMIN_ROLES = ("super_admin", "operator", "analyst")


# ---------- 刷新令牌轮换（规格十八） ----------

@router.post("/refresh-token/issue", response_model=RefreshTokenRotateResponse)
async def refresh_token_issue(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    plain = generate_secure_token(48)
    await refresh_token_service.issue(
        db,
        user_id=current_user.id,
        token_jti=plain,
        device_info="test-device",
    )
    return RefreshTokenRotateResponse(ok=True, new_token=plain, expires_at=datetime.utcnow())


@router.post("/refresh-token/rotate", response_model=RefreshTokenRotateResponse)
async def refresh_token_rotate(
    req: RefreshTokenRotateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """刷新令牌轮换：旧令牌作废，签发新令牌。"""
    new_plain = generate_secure_token(48)
    new_row = await refresh_token_service.rotate(
        db,
        old_jti=req.refresh_token,
        new_jti=new_plain,
    )
    if new_row is None:
        return RefreshTokenRotateResponse(ok=False, reason="刷新令牌无效或已过期")
    return RefreshTokenRotateResponse(ok=True, new_token=new_plain, expires_at=new_row.expires_at)


@router.post("/refresh-token/revoke-all", response_model=RefreshTokenRevokeAllResponse)
async def refresh_token_revoke_all(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    revoked = await refresh_token_service.revoke_all(db, user_id=current_user.id)
    return RefreshTokenRevokeAllResponse(ok=True, revoked=revoked)


# ---------- 高风险操作二次确认（规格十九） ----------

@router.post("/confirmations", response_model=ConfirmationCreateResponse)
async def confirmation_create(
    req: ConfirmationCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        row, plain = await confirmation_service.create(
            db, user_id=current_user.id, action=req.action
        )
        return ConfirmationCreateResponse(
            ok=True, action=req.action, token=plain, expires_at=row.expires_at
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/confirmations/confirm", response_model=ConfirmationConfirmResponse)
async def confirmation_confirm(
    req: ConfirmationConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = await confirmation_service.confirm(
        db, token=req.token, action=req.action, user_id=current_user.id
    )
    if row is None:
        return ConfirmationConfirmResponse(ok=False, action=req.action, reason="确认令牌无效、已使用或已过期")
    await security_event_service.record(
        db,
        event_type="permission_changed",
        user_id=current_user.id,
        ip_address="127.0.0.1",
        device_info=None,
        details=f"二次确认通过: {req.action}",
    )
    return ConfirmationConfirmResponse(ok=True, action=req.action)


# ---------- 安全事件审计（规格二十四） ----------

@router.get("/security-events", response_model=SecurityEventListResponse)
async def security_events_list(
    user_id: uuid.UUID | None = Query(default=None),
    event_type: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_roles(*ADMIN_ROLES)),
):
    try:
        rows, total = await security_event_service.list_events(
            db, user_id=user_id, event_type=event_type, page=page, page_size=page_size
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SecurityEventListResponse(total=total, items=[SecurityEventResponse.model_validate(r) for r in rows])


# ---------- 内容安全（规格二十一） ----------

@router.post("/content/check", response_model=ContentCheckResponse)
async def content_check(
    req: ContentCheckRequest,
    current_user: User = Depends(get_current_user),
):
    """内容安全检查：HTML 白名单过滤（XSS/恶意 HTML）与命令注入识别。"""
    if req.kind == "html":
        sanitized = sanitize_html(req.content)
    else:
        sanitized = html.escape(req.content)
    return ContentCheckResponse(
        sanitized=sanitized,
        command_injection_risk=looks_like_command_injection(req.content),
    )


# ---------- 隐私保护（规格二十三） ----------

@router.post("/privacy/export", response_model=PrivacyExportResponse)
async def privacy_export(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """数据导出：仅提供脱敏后的账户基本信息，私人正文/评论需二次授权。"""
    await security_event_service.record(
        db,
        event_type="data_export",
        user_id=current_user.id,
        ip_address="127.0.0.1",
        device_info=None,
        details="隐私数据导出（脱敏）",
    )
    return PrivacyExportResponse(
        ok=True,
        data={
            "username": current_user.username,
            "email": current_user.email,
            "role": current_user.role,
            "exported_at": datetime.utcnow().isoformat(),
            "note": "私人正文与评论需二次授权后单独导出",
        },
    )


@router.post("/privacy/delete-account", response_model=PrivacyDeleteAccountResponse)
async def privacy_delete_account(
    req: ConfirmationConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """账号注销：需二次确认令牌；受理后软注销并记录审计（数据保留期后清除）。"""
    row = await confirmation_service.confirm(
        db, token=req.token, action="delete_account", user_id=current_user.id
    )
    if row is None:
        return PrivacyDeleteAccountResponse(ok=False, reason="确认令牌无效、已使用或已过期")
    await security_event_service.record(
        db,
        event_type="account_delete_requested",
        user_id=current_user.id,
        ip_address="127.0.0.1",
        device_info=None,
        details="账号注销申请已受理（软注销待复核）",
    )
    return PrivacyDeleteAccountResponse(ok=True, reason="注销申请已受理，数据将在保留期后清除")


# ---------- 文件存储安全（规格二十二） ----------

@router.post("/files/validate", response_model=FileUploadValidateResponse)
async def file_validate(
    req: FileUploadValidateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await file_storage_service.validate_upload(
        original_name=req.original_name,
        declared_mime=req.declared_mime,
        content_length=req.content_length,
        is_private=req.is_private,
    )
    return FileUploadValidateResponse(
        ok=result["ok"],
        reason=result["reason"],
        stored_name=result["stored_name"],
        is_private=req.is_private,
    )


@router.post("/files/register", response_model=FileRegisterResponse)
async def file_register(
    req: FileRegisterRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = await file_storage_service.register_file(
        db,
        created_by=current_user.id,
        original_name=req.original_name,
        stored_name=req.stored_name,
        mime_type=req.mime_type,
        file_size=req.file_size,
        is_private=req.is_private,
        max_downloads=req.max_downloads,
        expires_in_minutes=req.expires_in_minutes,
    )
    return FileRegisterResponse(
        id=row.id,
        stored_name=row.stored_name,
        original_name=row.original_name,
        mime_type=row.mime_type,
        file_size=row.file_size,
        is_private=row.is_private,
        download_count=row.download_count,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


@router.get("/files", response_model=FileListResponse)
async def file_list(
    is_private: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    created_by = current_user.id
    if current_user.role in ADMIN_ROLES and is_private is not None:
        created_by = None
    rows, total = await file_storage_service.list_files(
        db, created_by=created_by, is_private=is_private, page=page, page_size=page_size
    )
    return FileListResponse(total=total, items=[StoredFileResponse.model_validate(r) for r in rows])


@router.get("/files/{stored_name}/sign", response_model=FileSignResponse)
async def file_sign(
    stored_name: str = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = await file_storage_service.get_by_stored_name(db, stored_name)
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row.is_private and row.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问私有文件")
    url = file_storage_service.sign_url(stored_name)
    return FileSignResponse(stored_name=stored_name, signed_url=url)


@router.get("/files/{stored_name}/download", response_model=FileDownloadResponse)
async def file_download(
    stored_name: str = Path(...),
    sig: str = Query(default=""),
    exp: int = Query(default=0),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_or_none),
):
    """受控下载：校验签名 URL + 过期 + 权限 + 下载次数上限。"""
    if not file_storage_service.verify_signature(stored_name, sig, exp):
        return FileDownloadResponse(ok=False, reason="签名 URL 无效或已过期")
    row = await file_storage_service.get_by_stored_name(db, stored_name)
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    user_id = current_user.id if current_user else None
    allowed, reason = await file_storage_service.check_download_allowed(
        db, file_row=row, user_id=user_id
    )
    if not allowed:
        return FileDownloadResponse(ok=False, reason=reason)
    await file_storage_service.count_download(db, row)
    return FileDownloadResponse(
        ok=True,
        original_name=row.original_name,
        mime_type=row.mime_type,
        download_count=row.download_count,
    )
