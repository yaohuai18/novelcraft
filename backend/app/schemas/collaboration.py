# 协作（成员/邀请/活动日志）相关 Pydantic 模型（第十五批规格实现）
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ProjectMemberResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    status: str
    invited_by: uuid.UUID | None
    joined_at: datetime | None
    created_at: datetime
    username: str | None = None

    model_config = {"from_attributes": True}


class MemberRoleUpdate(BaseModel):
    role: str = Field(max_length=20)


class InvitationCreate(BaseModel):
    invitee_email: str = Field(max_length=255)
    role: str = Field(default="viewer", max_length=20)
    expires_in_days: int = Field(default=7, ge=1, le=30)


class InvitationResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    inviter_id: uuid.UUID
    invitee_email: str
    role: str
    status: str
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime
    # 令牌明文仅在创建邀请时返回一次（规格十五：令牌只保存哈希）
    token: str | None = None

    model_config = {"from_attributes": True}


class InvitationAcceptResponse(BaseModel):
    invitation_id: uuid.UUID
    member: ProjectMemberResponse
    role: str


class InvitationTokenRequest(BaseModel):
    token: str = Field(min_length=1, max_length=128)


class ActivityLogResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    metadata: dict | None
    created_at: datetime
    username: str | None = None

    model_config = {"from_attributes": True}


class ActivityLogListResponse(BaseModel):
    items: list[ActivityLogResponse]
    total: int
    page: int
    page_size: int
