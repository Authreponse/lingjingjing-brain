from __future__ import annotations

from lingbrain.memory import extract_memories


def test_question_is_not_stored_as_fact():
    assert extract_memories("你还记得我喜欢什么吗？") == []
    assert extract_memories("我喜欢什么？") == []
    assert extract_memories("我讨厌谁") == []


def test_real_preferences_are_stored():
    items = extract_memories("我喜欢蓝鲸，我也讨厌熬夜")
    contents = {item.content for item in items}
    assert "主人喜欢蓝鲸" in contents
    assert "主人不喜欢熬夜" in contents


def test_quoted_text_is_not_treated_as_fact():
    items = extract_memories('她说"我喜欢这个"')
    assert all("这个" != item.content for item in items)


def test_name_and_preference_together():
    items = extract_memories("我叫阿星，我喜欢蓝鲸")
    by_kind = {item.kind: item.content for item in items}
    assert by_kind["profile"] == "主人希望被称呼为阿星"
    assert by_kind["preference"] == "主人喜欢蓝鲸"
