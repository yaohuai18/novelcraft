# 游标分页工具（第十八批「性能优化、可靠性与安全加固」规格六）
# 游标 = base64(JSON{key: <排序键值>, pk: <主键>})，用于 created_at 倒序列表的
# 高效分页：WHERE (created_at < :c) OR (created_at = :c AND id < :pk)，
# 避免大偏移量扫描。page_size 上限由 settings.page_size_max 约束（100）。
import base64
import json
from datetime import datetime
from uuid import UUID


def encode_cursor(*, sort_value: datetime | str, pk: str) -> str:
    """将排序键与主键编码为不透明游标字符串。"""
    payload = {
        "v": sort_value.isoformat() if isinstance(sort_value, datetime) else str(sort_value),
        "p": str(pk),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> dict | None:
    """解码游标；非法游标返回 None（调用方应回退到首页并置 400）。"""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        return {
            "sort_value": payload["v"],
            "pk": payload["p"],
        }
    except Exception:
        return None


def parse_sort_value(value: str, as_uuid: bool = False):
    """将游标中的排序键还原为可比较对象（datetime / str / uuid.UUID）。"""
    if value.endswith(("Z", "+00:00")) or ("T" in value and "-" in value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if as_uuid:
        return UUID(value)
    return value
