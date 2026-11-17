# -*- coding: utf-8 -*-
"""实体引用校验服务（阶段 E，对应方案补充 7）。

校验 AI 结构化输出中所有实体 id 引用（character_id / location_id / item_id /
secret_id / event_id 等）存在且属于本项目。写路径（state_facts / timeline /
状态变更建议等会落库的 AI 结构化输出）在写入前调用本服务；校验失败 →
丢弃该模型输出并记录（不落库），不阻断正文保存。

防误杀约束：
- 可空引用允许 NULL / 空字符串（不校验、不拒绝）；
- 值非合法 UUID 视为非法引用（strict 开启时拒绝，避免脏 id 落库）；
- 只校验白名单字段（ENTITY_REFERENCE_FIELDS），未知 *_id 字段不校验，
  避免把 chapter_id / project_id 等业务键误当实体引用。

strict 开关：写路径默认 True（由 ai_service.strict_entity_check_enabled()
控制），校验失败即拒绝；关闭时直接放行（开关可关，见 ai_service）。
"""
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.character import Character
from app.models.item import Item
from app.models.location import Location
from app.models.timeline_event import TimelineEvent

logger = logging.getLogger(__name__)

# 字段名 → (实体模型, 实体说明)。secret 本身登记在 items 表（category=secret），
# 因此 secret_id 也按 Item 校验存在性 + 项目归属（不强制 category，防误杀）。
ENTITY_REFERENCE_FIELDS: dict[str, tuple[Any, str]] = {
    "character_id": (Character, "character"),
    "owner_character_id": (Character, "character"),
    "source_character_id": (Character, "character"),
    "target_character_id": (Character, "character"),
    "knower_character_id": (Character, "character"),
    "holder_character_id": (Character, "character"),
    "location_id": (Location, "location"),
    "item_id": (Item, "item"),
    "secret_id": (Item, "secret"),
    "event_id": (TimelineEvent, "timeline_event"),
    "timeline_event_id": (TimelineEvent, "timeline_event"),
}


@dataclass
class EntityViolation:
    """单条引用违规：字段名 / 所在路径 / 值 / 期望实体类型 / 说明。"""

    field: str
    path: str
    value: str
    entity_type: str
    message: str

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "path": self.path,
            "value": self.value,
            "entity_type": self.entity_type,
            "message": self.message,
        }


@dataclass
class EntityValidationResult:
    """校验结果：ok=False 表示存在违规引用，写路径应丢弃模型输出。"""

    ok: bool
    checked: int = 0
    violations: list[EntityViolation] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "violations": [v.as_dict() for v in self.violations],
        }


def _iter_fields(payload: Any, prefix: str = ""):
    """递归遍历 dict/list，yield (path, field, value) 仅限白名单字段。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key in ENTITY_REFERENCE_FIELDS:
                yield path, key, value
            if isinstance(value, (dict, list)):
                yield from _iter_fields(value, path)
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            path = f"{prefix}[{i}]"
            if isinstance(item, (dict, list)):
                yield from _iter_fields(item, path)


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    """把引用值转成 UUID；None / 空串返回 None（可空引用）；非法返回 None 但带标记。"""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return uuid.UUID(text)
        except (ValueError, AttributeError):
            return None
    return None


async def validate_entity_references(
    db: AsyncSession,
    project_id: uuid.UUID,
    payload: Any,
    *,
    strict: bool = True,
) -> EntityValidationResult:
    """校验 payload 中所有实体 id 引用存在且属于本项目。

    - strict=False：开关关闭，不执行校验，直接返回 ok=True（放行）。
    - 可空引用（None / 空字符串）跳过，不误杀。
    - 值非合法 UUID：视为非法引用（strict 开启时拒绝）。
    - 引用 id 不存在于对应表，或不属于本项目：违规。
    """
    if not strict:
        return EntityValidationResult(ok=True)

    result = EntityValidationResult(ok=True)
    entries: list[tuple[str, str, uuid.UUID, str, Any]] = []
    by_model: dict[Any, set[uuid.UUID]] = {}

    for path, field, value in _iter_fields(payload):
        model, entity_type = ENTITY_REFERENCE_FIELDS[field]
        id_val = _coerce_uuid(value)
        if id_val is None:
            if value is not None and not (
                isinstance(value, str) and not value.strip()
            ):
                # 非空但非法 UUID / 非字符串类型 → 违规
                result.ok = False
                result.violations.append(
                    EntityViolation(
                        field=field,
                        path=path,
                        value=str(value),
                        entity_type=entity_type,
                        message=(
                            f"{entity_type} 引用不是合法 UUID 格式"
                        ),
                    )
                )
            continue  # 可空引用（None / 空串）跳过
        entries.append((path, field, id_val, entity_type, model))
        by_model.setdefault(model, set()).add(id_val)

    if not entries:
        return result

    for model, id_set in by_model.items():
        id_list = list(id_set)
        rows = (
            await db.execute(
                select(model.id).where(
                    model.id.in_(id_list),
                    model.project_id == project_id,
                )
            )
        ).scalars().all()
        existing = set(rows)
        for path, field, id_val, entity_type, m in entries:
            if m is not model:
                continue
            result.checked += 1
            if id_val not in existing:
                result.ok = False
                result.violations.append(
                    EntityViolation(
                        field=field,
                        path=path,
                        value=str(id_val),
                        entity_type=entity_type,
                        message=(
                            f"{entity_type} 不存在或不属于本项目"
                        ),
                    )
                )

    if not result.ok:
        logger.warning(
            "entity_reference_validator: rejected payload, %d violation(s): %s",
            len(result.violations),
            [v.as_dict() for v in result.violations],
        )
    return result
