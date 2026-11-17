# 用户相关 Pydantic 请求/响应模型
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserRegister(BaseModel):
    """用户注册请求体。"""

    username: str = Field(min_length=3, max_length=50, description="用户名，3-50 字符")
    email: EmailStr = Field(..., description="邮箱")
    password: str = Field(min_length=6, max_length=72, description="密码，6-72 位")


class UserLogin(BaseModel):
    """用户登录请求体。"""

    username: str
    password: str


class UserResponse(BaseModel):
    """用户信息响应模型。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: EmailStr
    role: str
    created_at: datetime  # 保留现有额外字段（规格未含，现有实现已有）


class TokenResponse(BaseModel):
    """登录成功后的令牌响应。"""

    access_token: str
    token_type: str = "bearer"
