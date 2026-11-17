# 运营后台请求体 Schema（第十七批「统计分析与运营后台」规格十二/十四/十五/十八）
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ReasonRequest(BaseModel):
    """人工操作必须记录原因（规格十四）。"""
    reason: str = Field(..., min_length=1)


class HandleReportRequest(ReasonRequest):
    """处理举报：resolved / rejected。"""
    status: str = "resolved"


class OfficialTagRequest(ReasonRequest):
    tag: str = Field(..., min_length=1, max_length=60)


class RestrictUserRequest(ReasonRequest):
    """限制账号功能：restricted / disabled / active。"""
    status: str = "restricted"


class HandleTicketRequest(ReasonRequest):
    """处理工单（规格十四）：状态流转 + 备注，reason 必填用于审计。"""
    status: str = "resolved"
    note: str | None = None


class FeaturedCreateRequest(ReasonRequest):
    """创建推荐位（规格十五）。"""
    target_type: str = Field(..., description="project/chapter")
    target_id: str
    position: str = Field(..., description="home/category/topic/editor/new")
    sort_order: int = 0
    start_at: datetime | None = None
    end_at: datetime | None = None


class FeaturedUpdateRequest(ReasonRequest):
    sort_order: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    status: str | None = None


class PrivateAccessGrantRequest(BaseModel):
    """私有内容临时授权（明确授权+理由+自动失效，规格十二）。"""
    granted_to: uuid.UUID
    project_id: uuid.UUID
    access_reason: str = Field(..., min_length=1)
    expires_at: datetime


class PrivateAccessRevokeRequest(ReasonRequest):
    pass


class SettingsUpdateRequest(ReasonRequest):
    updates: dict


class ExportQuery(BaseModel):
    """报表导出（规格十八：限时间范围/限数据量/权限检查/脱敏/记录导出）。"""
    report_type: str = Field(..., description="users/content/reading/ai/errors/reports")
    from_date: datetime | None = None
    to_date: datetime | None = None
    fmt: str = "csv"
    page: int = 1
    page_size: int = 500
