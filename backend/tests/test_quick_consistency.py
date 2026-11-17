# 批次 2（2026-11-17）：创作页轻量一致性校验 quick_check 的 4 维规则测试。
# 无数据库、零 AI 成本，纯确定性规则；覆盖验收三个矛盾用例 + 误报防护。
from app.services.quick_consistency import quick_check


def _types(result):
    return [i["type"] for i in result["issues"]]


def test_foreshadow_payoff_before_planted():
    plan = {"foreshadowing": [{"item": "砒霜来源", "planted_chapter": 30, "payoff_chapter": 20}]}
    result = quick_check(plan, [], [])
    assert "foreshadow_order" in _types(result)


def test_foreshadow_normal_not_flagged():
    plan = {"foreshadowing": [{"item": "砒霜来源", "planted_chapter": 30, "payoff_chapter": 60}]}
    result = quick_check(plan, [], [])
    assert result["issues"] == []


def test_timeline_reversal_flagged():
    chapters = [
        {"index": 1, "title": "第1章", "content": "崇宁三年三月五日，陈九进了京。"},
        {"index": 2, "title": "第2章", "content": "崇宁三年三月一日，顾长宁在巷口等他。"},
    ]
    result = quick_check({}, chapters, [])
    assert "timeline_reversal" in _types(result)


def test_timeline_arabic_dates():
    chapters = [
        {"index": 1, "title": "第1章", "content": "2026年3月5日，开篇。"},
        {"index": 2, "title": "第2章", "content": "2026年3月1日，倒流。"},
    ]
    result = quick_check({}, chapters, [])
    assert "timeline_reversal" in _types(result)


def test_timeline_different_eras_not_compared():
    chapters = [
        {"index": 1, "title": "第1章", "content": "崇宁三年三月五日。"},
        {"index": 2, "title": "第2章", "content": "天启元年一月一日。"},
    ]
    result = quick_check({}, chapters, [])
    assert "timeline_reversal" not in _types(result)


def test_character_death_then_action_flagged():
    plan = {
        "protagonist": "陈九",
        "supporting_characters": [{"name": "顾长宁", "role": "大理寺录事", "description": ""}],
    }
    chapters = [
        {"index": 1, "title": "第1章", "content": "顾长宁已经死了，尸体停在义庄。"},
        {"index": 2, "title": "第2章", "content": "顾长宁说：“这案子不对。”"},
    ]
    result = quick_check(plan, chapters, [])
    assert "character_state_conflict" in _types(result)


def test_character_alive_not_flagged():
    plan = {"protagonist": "陈九"}
    chapters = [
        {"index": 1, "title": "第1章", "content": "陈九进了京。"},
        {"index": 2, "title": "第2章", "content": "陈九说：“继续查。”"},
    ]
    result = quick_check(plan, chapters, [])
    assert result["issues"] == []


def test_rule_violation_gore_words():
    chapters = [{"index": 1, "title": "第1章", "content": "墙上溅满脑浆和血浆迸出的痕迹。"}]
    result = quick_check({}, chapters, ["内容限制：不出现血腥猎奇描写"])
    assert "rule_violation" in _types(result)


def test_rule_violation_custom_keyword():
    chapters = [{"index": 1, "title": "第1章", "content": "他突然穿越回了十年前。"}]
    result = quick_check({}, chapters, ["内容限制：不出现穿越情节"])
    assert "rule_violation" in _types(result)


def test_clean_content_no_issues():
    result = quick_check({}, [{"index": 1, "title": "第1章", "content": "崇宁三年三月五日，风平浪静。"}], [])
    assert result["issues"] == []
