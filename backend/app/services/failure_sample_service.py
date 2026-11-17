# 失败样本服务（2026-11-17 批次 4）：改稿差集自动沉淀。
# 作者大幅重写 AI 生成结果时，把「原稿 vs 改稿」差集存为 failure_samples
# （source='edit_diff'），后续生成经 _inject_failure_samples 自动注入教训。
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.failure_sample import FailureSample
from app.models.generation import GenerationRecord

# 相似度阈值：低于此值视为「作者大幅重写」（0~1）
EDIT_DIFF_SIMILARITY_THRESHOLD = 0.6
# 过短的文本不沉淀（噪声）
_MIN_CHARS = 100
# 样本截断长度（防止样本库膨胀）
_MAX_SAMPLE_CHARS = 1500


def content_similarity(a: str, b: str) -> float:
    """两个文本的相似度（0~1）。"""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


async def sink_edit_diff_if_rewritten(
    db: AsyncSession,
    *,
    project_id,
    outline_id,
    new_content: str,
) -> bool:
    """章节保存后调用：与最近一次 AI 生成结果比对，大幅重写则沉淀样本。

    返回 True 表示已落库（调用方负责 commit）。失败/不需要时返回 False。
    """
    if not new_content or len(new_content) < _MIN_CHARS:
        return False
    row = (
        await db.execute(
            select(GenerationRecord.final_content)
            .where(
                GenerationRecord.chapter_id == outline_id,
                # 只取「正文生成类」记录：保存流水线会另落 summarize/extract_state/check
                # 等记录（final_content 为空），按时间倒序取最新会把正文记录挤掉。
                GenerationRecord.action_type.in_(
                    ("draft", "continue", "rewrite", "polish", "expand")
                ),
                GenerationRecord.final_content.is_not(None),
                GenerationRecord.final_content != "",
            )
            .order_by(GenerationRecord.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    original = row or ""
    if len(original) < _MIN_CHARS:
        return False
    if content_similarity(original, new_content) >= EDIT_DIFF_SIMILARITY_THRESHOLD:
        return False  # 没怎么改，不沉淀

    bad_head = original[:_MAX_SAMPLE_CHARS]
    # 去重：同项目最近一条 edit_diff 样本的原稿头部相同 → 跳过（连续自动保存不刷屏）
    existing_head = (
        await db.execute(
            select(FailureSample.bad_example)
            .where(
                FailureSample.project_id == project_id,
                FailureSample.source == "edit_diff",
            )
            .order_by(FailureSample.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_head and (existing_head or "")[:200] == bad_head[:200]:
        return False

    db.add(
        FailureSample(
            project_id=project_id,
            category="style",
            bad_example=bad_head,
            correct_way=new_content[:_MAX_SAMPLE_CHARS],
            source="edit_diff",
            enabled=1,
        )
    )
    return True
