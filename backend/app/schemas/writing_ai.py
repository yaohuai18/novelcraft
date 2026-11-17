# 章节写作 AI 相关 Pydantic 请求/响应模型（第七批规格实现 + 第一期「生成不丢失」扩展）
# continue / rewrite / polish 三个接口统一返回 WritingPreviewResponse（预览不落库）。
# 第一期：AI 返回后先写 generation_record，响应携带 generation_record_id，
# 前端「接受为新版本」时回传该 ID 创建 chapter_version。
import uuid

from pydantic import BaseModel, Field


class ContinueWritingRequest(BaseModel):
    """AI 续写请求体：当前章节正文 + 期望续写长度 + 用户额外要求。"""

    content: str = Field(
        min_length=1,
        max_length=500000
    )

    max_length: int = Field(
        default=1500,
        ge=100,
        le=5000
    )

    instruction: str | None = Field(
        default=None,
        max_length=3000
    )

    # 第六阶段：请求级模型覆盖（随请求发送，密钥仍只存服务端环境变量）
    model_name: str | None = Field(
        default=None,
        max_length=200
    )

    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0
    )

    max_tokens: int | None = Field(
        default=None,
        ge=256,
        le=32768
    )


class RewriteWritingRequest(BaseModel):
    """AI 改写请求体：原文 + 改写要求（增强冲突感等）。"""

    content: str = Field(
        min_length=1,
        max_length=500000
    )

    instruction: str | None = Field(
        default=None,
        max_length=3000
    )

    # 第六阶段：请求级模型覆盖
    model_name: str | None = Field(
        default=None,
        max_length=200
    )

    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0
    )

    max_tokens: int | None = Field(
        default=None,
        ge=256,
        le=32768
    )


class PolishWritingRequest(BaseModel):
    """AI 润色请求体：原文 + 目标风格（语言克制、节奏紧凑等）。"""

    content: str = Field(
        min_length=1,
        max_length=500000
    )

    style: str | None = Field(
        default=None,
        max_length=3000
    )

    # 第六阶段：请求级模型覆盖
    model_name: str | None = Field(
        default=None,
        max_length=200
    )

    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0
    )

    max_tokens: int | None = Field(
        default=None,
        ge=256,
        le=32768
    )


class WritingPreviewResponse(BaseModel):
    """AI 写作预览响应：只返回内容，不覆盖正文、不生成版本。

    第一期「生成不丢失」：每次 AI 调用均先写入 generation_record，
    generation_record_id 供前端「接受为新版本」时回传创建 chapter_version。
    """

    content: str
    generation_record_id: uuid.UUID
