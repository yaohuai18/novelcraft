# 批次 4（2026-11-17）：追读力规则 + 改稿差集相似度 的纯函数测试。
from app.services.failure_sample_service import content_similarity
from app.services.readability import has_hook_ending, thrill_hits


def test_hook_question_ending():
    assert has_hook_ending("陈九抬头：天快亮了。" + "他低声道：“你到底是谁？”") is True


def test_hook_ellipsis_ending():
    assert has_hook_ending("门后的人慢慢转过头来，那张脸……") is True


def test_hook_unclosed_ending():
    assert has_hook_ending("他推开那扇门，看见") is True


def test_no_hook_clean_ending():
    assert has_hook_ending("陈九抬头看天。天快亮了。") is False


def test_thrill_hits_counts_keywords():
    text = "满座皆惊。原来如此，这是一场彻头彻尾的反转，所有人没想到会是这个结果。"
    assert thrill_hits(text) >= 4


def test_thrill_hits_zero():
    assert thrill_hits("他慢慢走过长街，风很轻。") == 0


def test_similarity_identical_is_one():
    text = "这是一段正文。" * 30
    assert content_similarity(text, text) > 0.99


def test_similarity_rewritten_is_low():
    original = "陈九进了京，先去大理寺查旧卷。" * 30
    rewritten = "顾长宁在码头等了三天，最后等来一封血书。" * 30
    assert content_similarity(original, rewritten) < 0.6


def test_similarity_empty_handling():
    assert content_similarity("", "任意文本") == 0.0
    assert content_similarity("任意文本", "") == 0.0
