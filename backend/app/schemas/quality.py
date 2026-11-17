# 质量指标聚合接口模型（阶段B「人工处置与误报闭环」补充9）
from typing import Any

from pydantic import BaseModel, Field


class QualityMetrics(BaseModel):
    """正文质量指标（无数据时为 None，避免假指标）。"""
    generation_truncate_rate: float | None = None
    validation_blocking_rate: float | None = None
    false_positive_rate: float | None = None
    repair_success_rate: float | None = None


class QualityReportResponse(BaseModel):
    """质量面板报告：指标聚合 + 误报反馈闭环摘要。"""
    metrics: QualityMetrics
    disposition_summary: dict[str, int] = Field(
        default_factory=dict,
        description="按 disposition 分组的处置统计（confirmed/false_positive/...）",
    )
    false_positive_feedback: list[dict[str, Any]] = Field(
        default_factory=list, description="最近 false_positive 处置样本（误报回灌）"
    )
    generated_at: str
